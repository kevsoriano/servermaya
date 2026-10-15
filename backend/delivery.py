"""Dispatch and delivery. Pure functions: each takes the stored order and returns the fields to update.

order["fulfillment"] is the current shipment; replaced or cancelled ones move to order["fulfillment_history"].
Every status change is an event labelled with its source: provider (Lalamove), seller_reported, customer_reported or demo
(the delivery simulator). Events are appended, never edited. Delivered (a courier fact) and received (the customer's word)
are separate records, and neither proves the parcel held the right items.
"""

import re
from datetime import datetime

from orders import Refused
from packing import paid_by

# Normalized statuses in the order a shipment moves through them. Booking or a driver match is not a pickup.
RANK = {"quoted": 0, "booking": 1, "booked": 2, "driver_assigned": 3, "picked_up": 4, "delivered": 5}
ENDED = ("cancelled", "failed")
LALAMOVE = {  # raw Lalamove v3 status -> normalized; the raw value is always kept beside it
    "ASSIGNING_DRIVER": "booked",
    "ON_GOING": "driver_assigned",
    "PICKED_UP": "picked_up",
    "COMPLETED": "delivered",
    "CANCELED": "cancelled",
    "REJECTED": "failed",
    "EXPIRED": "failed",
}
SIMULATED_STEPS = ("driver_assigned", "picked_up", "delivered")
SELLER_STEPS = ("picked_up", "delivered", "failed")


def can_dispatch(order: dict) -> dict:
    """The dispatch gate: this version confirmed, paid for, and packed. Returns the completed packing record."""
    if order["status"] != "confirmed":
        raise Refused("Dispatch needs a confirmed order")
    if not paid_by(order):
        raise Refused("Dispatch needs a payment verified by PayMongo, or a GCash payment you confirmed, for this version")
    packing = order.get("packing") or {}
    if packing.get("version") != order.get("version") or not packing.get("completed"):
        raise Refused("Dispatch needs packing marked complete for this version")
    return packing


def active(order: dict) -> dict | None:
    f = order.get("fulfillment")
    return f if f and f["status"] not in ENDED else None


def start(order: dict, fid: str, courier: str, by: str, now: datetime, quotation: dict | None = None, tracking: str | None = None) -> dict:
    """Open a shipment. Lalamove starts as a quote the seller still has to approve; the simulator and other couriers start booked."""
    can_dispatch(order)
    current = active(order)
    if current and current["status"] != "quoted":
        raise Refused("This order already has a shipment. Cancel it before starting another.")
    shipment = {
        "id": fid,
        "version": order["version"],
        "courier": courier,
        "status": "quoted" if courier == "lalamove" else "booked",
        "quotation": quotation,
        "provider_order_id": None,
        "share_link": None,
        "tracking": tracking,
        "driver_id": None,
        "pod": None,
        "events": [],
        "created_by": by,
        "created_at": now,
        "replaces": (order.get("replacement") or {}).get("fulfillment_id"),
        "claim_id": (order.get("replacement") or {}).get("claim_id"),
    }
    if courier != "lalamove":
        source = "demo" if courier == "simulator" else "seller_reported"
        shipment["events"] = [event(f"{fid}:booked", source, "booked", "booked", now, now)]
    return {"fulfillment": shipment}


def event(eid: str, source: str, raw: str, status: str, at: datetime, received_at: datetime, **detail) -> dict:
    return {"id": eid, "source": source, "raw_status": raw, "status": status, "at": at, "received_at": received_at, **detail}


def begin_booking(order: dict, now: datetime) -> dict:
    """Lock the quote before calling Lalamove, so a double click or a retry cannot book twice."""
    can_dispatch(order)
    f = active(order)
    if not f or f["courier"] != "lalamove":
        raise Refused("Get a Lalamove quote first")
    if f["status"] != "quoted":
        raise Refused("This shipment is already booked" if f["status"] != "booking" else "Booking is in progress. Wait for Lalamove's answer.")
    if f["quotation"]["expires_at"] <= now:
        raise Refused("The quote has expired. Get a new one.")
    return {"fulfillment": {**f, "status": "booking", "booking_started_at": now}}


def booked(order: dict, provider_order_id: str, share_link: str | None, now: datetime) -> dict:
    f = order.get("fulfillment") or {}
    if f.get("provider_order_id") == provider_order_id:
        return {}  # the webhook or the API answer got here first
    if f.get("status") not in ("booking", "quoted"):
        raise Refused("No booking in progress")
    ev = event(f"lalamove:{provider_order_id}:created", "provider", "ORDER_CREATED", "booked", now, now)
    return {"fulfillment": {**f, "status": "booked", "provider_order_id": provider_order_id, "share_link": share_link,
                            "booking_uncertain": False, "events": f["events"] + [ev]}}


def booking_failed(order: dict, problem: str) -> dict:
    """Lalamove refused the booking, so nothing was created: unlock the quote."""
    f = order.get("fulfillment") or {}
    return {"fulfillment": {**f, "status": "quoted", "problem": problem}} if f.get("status") == "booking" else {}


def booking_uncertain(order: dict) -> dict:
    """Lalamove did not answer. The quote stays locked: Lalamove's ORDER_CREATED event (matched by quotation) settles it."""
    f = order.get("fulfillment") or {}
    return {"fulfillment": {**f, "booking_uncertain": True}} if f.get("status") == "booking" else {}


def release_booking(order: dict, now: datetime) -> dict:
    """Give up on an unanswered booking. Only once its quote has expired, so Lalamove can no longer create an order from it."""
    f = order.get("fulfillment") or {}
    if f.get("status") != "booking":
        return {}
    if f["quotation"]["expires_at"] > now:
        raise Refused("Wait until the quote expires; Lalamove may still be creating the order")
    return {"fulfillment": {**f, "status": "quoted", "booking_uncertain": False, "released_at": now}}


def apply_event(order: dict, ev: dict, driver_id: str | None = None, share_link: str | None = None, pod: dict | None = None) -> dict:
    """Record one status event. Duplicates change nothing; a late or out-of-order event is kept but never moves status back."""
    f = order.get("fulfillment")
    if not f or any(e["id"] == ev["id"] for e in f["events"]):
        return {}
    status = f["status"]
    if ev["status"] in ENDED:
        if status != "delivered":
            status = ev["status"]
    elif status not in ENDED and RANK.get(ev["status"], -1) > RANK.get(status, -1):
        status = ev["status"]
    updated = {**f, "status": status, "events": f["events"] + [ev]}
    if driver_id and driver_id != f["driver_id"]:
        # Reassignment is recorded, not overwritten: who was assigned when matters for delivery patterns.
        updated["driver_id"] = driver_id
        updated["drivers"] = f.get("drivers", []) + [{"driver_id": driver_id, "at": ev["at"]}]
    if share_link:
        updated["share_link"] = share_link
    if pod:
        updated["pod"] = pod
    return {"fulfillment": updated}


def step(order: dict, status: str, source: str, by: str, now: datetime, note: str = "") -> dict:
    """The next status from the delivery simulator (labelled demo) or from the seller for another courier (seller_reported)."""
    f = active(order)
    if not f:
        raise Refused("No shipment to update")
    allowed = SIMULATED_STEPS if f["courier"] == "simulator" else SELLER_STEPS if f["courier"] == "other" else ()
    if status not in allowed or (f["courier"] == "simulator") != (source == "demo"):
        raise Refused("This shipment's status comes from its courier")
    if status not in ENDED and RANK[status] <= RANK.get(f["status"], -1):
        raise Refused(f"The shipment is already {f['status'].replace('_', ' ')}")
    ev = event(f"{f['id']}:{status}:{len(f['events'])}", source, status, status, now, now, by=by, note=note.strip() or None)
    return apply_event(order, ev)


def cancel(order: dict, by: str, reason: str, now: datetime) -> dict:
    """Seller-side cancel of a shipment not yet picked up. A booked Lalamove order must also be cancelled with Lalamove."""
    f = active(order)
    if not f:
        return {}
    if RANK.get(f["status"], 0) >= RANK["picked_up"]:
        raise Refused("The parcel is already picked up")
    ev = event(f"{f['id']}:cancelled", "seller_reported", "cancelled", "cancelled", now, now, by=by, note=reason.strip() or None)
    ended = {**f, "status": "cancelled", "events": f["events"] + [ev]}
    return {"fulfillment": None, "fulfillment_history": order.get("fulfillment_history", []) + [ended]}


def acknowledge(order: dict, source: str, now: datetime) -> dict:
    """The customer says they received it. Kept apart from the courier's delivered status."""
    f = order.get("fulfillment")
    if not f or RANK.get(f["status"], -1) < RANK["picked_up"]:
        raise Refused("The order has not been sent yet")
    if (order.get("receipt") or {}).get("fulfillment_id") == f["id"]:
        return {}
    return {"receipt": {"status": "customer_confirmed", "fulfillment_id": f["id"], "source": source, "at": now}}


def parse_pin(text: str) -> tuple[float, float]:
    """A drop-off pin as "14.6091, 121.0223" or a Google Maps link containing it. Limited to the Philippines."""
    found = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{2,3}\.\d+)", text or "")
    if not found:
        raise Refused("Paste the drop-off pin as latitude, longitude (for example 14.6091, 121.0223) or a Google Maps link")
    lat, lng = float(found[1]), float(found[2])
    if not (4 <= lat <= 22 and 116 <= lng <= 127):
        raise Refused("That pin is outside the Philippines")
    return lat, lng


def e164(phone: str) -> str:
    """Lalamove wants +63 numbers: 0917 123 4567 and 639171234567 become +639171234567."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = "63" + digits[1:]
    if not re.fullmatch(r"639\d{9}", digits):
        raise Refused("Use a Philippine mobile number such as 0917 123 4567")
    return "+" + digits
