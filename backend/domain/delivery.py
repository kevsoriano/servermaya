"""Dispatch and delivery. Pure functions: each takes the stored order and returns the fields to update.

order["fulfillment"] is the current shipment; replaced or cancelled ones move to order["fulfillment_history"].
Every status change is an event labelled with its source: provider (Lalamove), seller_reported, customer_reported or demo
(the delivery simulator). Events are appended, never edited. Delivered (a courier fact) and received (the customer's word)
are separate records, and neither proves the parcel held the right items.
"""

import hashlib
import re
from datetime import datetime, timedelta

from .orders import Refused
from .packing import paid_by

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
    ended = order.get("fulfillment") if (order.get("fulfillment") or {}).get("status") in ENDED else None
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
        "driver": None,
        "pop": None,
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
    # A failed or cancelled shipment stays on record when the next one starts, as a cancelled one does.
    history = {"fulfillment_history": order.get("fulfillment_history", []) + [ended]} if ended else {}
    return {"fulfillment": shipment, **history}


def event(eid: str, source: str, raw: str, status: str, at: datetime, received_at: datetime, **detail) -> dict:
    return {"id": eid, "source": source, "raw_status": raw, "status": status, "at": at, "received_at": received_at, **detail}


def begin_booking(order: dict, now: datetime, sandbox_outcome: str | None = None) -> dict:
    """Lock the quote before calling Lalamove, so a double click or a retry cannot book twice."""
    can_dispatch(order)
    f = active(order)
    if not f or f["courier"] != "lalamove":
        raise Refused("Get a Lalamove quote first")
    if f["status"] != "quoted":
        raise Refused("This shipment is already booked" if f["status"] != "booking" else "Booking is in progress. Wait for Lalamove's answer.")
    if f["quotation"]["expires_at"] <= now:
        raise Refused("The quote has expired. Get a new one.")
    booking = {**f, "status": "booking", "booking_started_at": now}
    if sandbox_outcome and f["quotation"].get("livemode") is False:  # never on a live booking
        booking["sandbox_outcome"] = sandbox_outcome
    return {"fulfillment": booking}


def booked(order: dict, provider_order_id: str, share_link: str | None, now: datetime) -> dict:
    f = order.get("fulfillment") or {}
    if f.get("provider_order_id") == provider_order_id:
        return {}  # the webhook or the API answer got here first
    if f.get("status") not in ("booking", "quoted"):
        raise Refused("No booking in progress")
    ev = event(f"lalamove:{provider_order_id}:created", "provider", "ORDER_CREATED", "booked", now, now)
    return {
        "fulfillment": {
            **f,
            "status": "booked",
            "provider_order_id": provider_order_id,
            "share_link": share_link,
            "booking_uncertain": False,
            "events": f["events"] + [ev],
        }
    }


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


def record(f: dict, ev: dict, driver: dict | None = None, share_link: str | None = None, pod: dict | None = None, pop: dict | None = None):
    """One status event on a shipment (an order's delivery or a return's). The updated shipment, or None for a duplicate.
    A late or out-of-order event is kept but never moves status back."""
    if any(e["id"] == ev["id"] for e in f["events"]):
        return None
    status = f["status"]
    if ev["status"] in ENDED:
        if status != "delivered":
            status = ev["status"]
    elif status not in ENDED and RANK.get(ev["status"], -1) > RANK.get(status, -1):
        status = ev["status"]
    updated = {**f, "status": status, "events": f["events"] + [ev]}
    if driver:
        updated = with_driver(updated, driver)
    if share_link:
        updated["share_link"] = share_link
    if pod:
        updated["pod"] = pod
    if pop:
        updated["pop"] = pop
    return updated


def apply_event(
    order: dict, ev: dict, driver_id: str | None = None, share_link: str | None = None, pod: dict | None = None, pop: dict | None = None, driver=None
) -> dict:
    """Record one status event on the order's shipment. Duplicates change nothing."""
    f = order.get("fulfillment")
    if not f:
        return {}
    if driver_id and not driver:
        driver = driver_record("provider", driver_id, at=ev["at"])
    updated = record(f, ev, driver, share_link, pod, pop)
    return {"fulfillment": updated} if updated else {}


# Drivers. Every record says where it came from: provider (Lalamove), simulated (the sandbox demo), or seller_reported (a
# rider the seller names, such as a Favourite Driver booked in Lalamove's own app or one hired outside Lalamove). Lalamove's
# driver is never replaced by another source; a different driver is added to the history, because who carried a parcel
# matters for delivery patterns.


def driver_record(source: str, driver_id: str, name: str | None = None, plate: str | None = None, at: datetime | None = None) -> dict:
    return {"source": source, "driver_id": driver_id, "name": name, "plate": plate, "at": at}


def with_driver(f: dict, d: dict) -> dict:
    current = f.get("driver") or (driver_record("provider", f["driver_id"]) if f.get("driver_id") else None)
    if current and current["source"] == "provider" and d["source"] != "provider":
        return f
    if current and (current["source"], current["driver_id"]) == (d["source"], d["driver_id"]):
        return {**f, "driver": {**current, **{k: v for k, v in d.items() if v is not None and k != "at"}}}  # details fetched later
    history = f.get("drivers", []) + [{"driver_id": d["driver_id"], "source": d["source"], "at": d["at"]}]
    return {**f, "driver": d, "driver_id": d["driver_id"], "drivers": history}


def seller_driver_id(name: str, plate: str) -> str:
    """A seller-reported rider has no courier ID: grouped on what the seller typed, which is all there is."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return f"seller:{norm(name)}|{norm(plate)}"


def shipments(order: dict) -> list[dict]:
    return [f for f in [order.get("fulfillment")] + order.get("fulfillment_history", []) if f]


def replace_shipment(order: dict, updated: dict) -> dict:
    """The update that stores one changed shipment where it already is: current, or in the history."""
    if (order.get("fulfillment") or {}).get("id") == updated["id"]:
        return {"fulfillment": updated}
    return {"fulfillment_history": [updated if f["id"] == updated["id"] else f for f in order.get("fulfillment_history", [])]}


def find_shipment(order: dict, fid: str) -> dict:
    found = next((f for f in shipments(order) if f["id"] == fid), None)
    if not found:
        raise Refused("No such shipment")
    return found


def named(f: dict, name: str, plate: str, by: str, now: datetime) -> dict:
    """A shipment with the rider the seller names: for another courier, a rider they arranged, or a Lalamove booking that came
    without driver details (Lalamove's ID is kept, with the seller's details)."""
    current = f.get("driver") or {}
    if current.get("source") == "provider" and (current.get("name") or current.get("plate")):
        raise Refused("Lalamove already gave this shipment's driver")
    if not name.strip():
        raise Refused("Enter the rider's name")
    if current.get("source") == "provider":
        return {**f, "driver": {**current, "name": name.strip(), "plate": plate.strip() or None, "named_by": by}}
    d = {**driver_record("seller_reported", seller_driver_id(name, plate), name.strip(), plate.strip() or None, now), "by": by}
    return with_driver(f, d)


def name_driver(order: dict, fid: str, name: str, plate: str, by: str, now: datetime) -> dict:
    return replace_shipment(order, named(find_shipment(order, fid), name, plate, by, now))


# Why a shipment failed or was cancelled. Lalamove gives no reason, so the seller records one.
END_WHO = ("driver", "customer", "seller", "courier", "unknown")
END_WHY = ("customer_unreachable", "wrong_pin", "refused", "driver_no_show", "damaged", "other")


def end_reason(order: dict, fid: str, who: str, why: str, note: str, by: str, now: datetime) -> dict:
    f = find_shipment(order, fid)
    if f["status"] not in ENDED:
        raise Refused("Only a failed or cancelled shipment has an end reason")
    if who not in END_WHO or why not in END_WHY:
        raise Refused("Pick who and why")
    if why == "other" and not note.strip():
        raise Refused("Say what happened")
    return replace_shipment(order, {**f, "end_reason": {"who": who, "why": why, "note": note.strip() or None, "by": by, "at": now}})


# Lalamove's sandbox takes a booking but rarely assigns a driver (none in 20 minutes, in two tries on 2026-10-17), and its API
# cannot move an order on. For the demo, a sandbox booking moves on by itself, one stage a second, each step labelled as
# simulated, with a simulated driver and sample pickup and drop-off photos. Never for a live booking.
SANDBOX_STEP = timedelta(seconds=1)
SANDBOX_NOTE = "Lalamove's sandbox rarely assigns a driver: this step is simulated for the demo"
# The outcome the seller picks when booking in the sandbox, and the steps that play it out
SANDBOX_OUTCOMES = {
    "delivered": ("driver_assigned", "picked_up", "delivered"),
    "damaged": ("driver_assigned", "picked_up", "delivered"),  # handed over with a crushed corner
    "failed": ("driver_assigned", "picked_up", "failed"),  # the rider could not hand it over
    "cancelled": ("driver_assigned", "cancelled"),  # the driver cancelled before pickup
}
SANDBOX_NOTES = {
    "failed": "The rider could not deliver it (simulated in Lalamove's sandbox)",
    "cancelled": "The driver cancelled before pickup (simulated in Lalamove's sandbox)",
}
# Made-up riders, so a sandbox shipment shows a driver as a live one would. The shipment's ID picks one, so it never changes.
SANDBOX_DRIVERS = [
    ("sim-1", "Sample Rider 1", "ABC ***1"),
    ("sim-2", "Sample Rider 2", "DEF ***2"),
    ("sim-3", "Sample Rider 3", "GHI ***3"),
]
# Sample courier photos (seed/images), stamped as AI-generated samples: the parcel at pickup, and at drop-off by outcome.
SANDBOX_PICKUP = "courier-pickup-intact"
SANDBOX_DROPOFF = {"delivered": "courier-dropoff-intact", "damaged": "courier-dropoff-crushed"}


def sandbox_driver(fid: str, at: datetime) -> dict:
    driver_id, name, plate = SANDBOX_DRIVERS[int(hashlib.sha256(fid.encode()).hexdigest(), 16) % len(SANDBOX_DRIVERS)]
    return driver_record("simulated", driver_id, name, plate, at)


def proof(status: str, at: datetime, source: str, image: dict | None = None, image_url: str | None = None) -> dict:
    """A courier's pickup or drop-off proof. image: the copy kept in Storage, or {"sample": name} for a demo photo."""
    return {"status": status, "at": at, "source": source, "image": image, "image_url": image_url}


def sandbox_steps(f: dict, now: datetime) -> dict | None:
    """The simulated steps now due on a Lalamove sandbox shipment, one second apart from the booking, playing out the outcome
    the seller picked (delivered unless said otherwise). Each step has a fixed ID, so applying this again adds nothing, and
    a real Lalamove update that is further along is never moved back. The updated shipment, or None."""
    if f.get("courier") != "lalamove" or (f.get("quotation") or {}).get("livemode") is not False or f.get("status") in ENDED:
        return None
    booked_at = next((e["at"] for e in f.get("events", []) if e["status"] == "booked"), None)
    if not booked_at:
        return None
    outcome = f.get("sandbox_outcome") or "delivered"
    current, changed = f, False
    for n, status in enumerate(SANDBOX_OUTCOMES.get(outcome, SIMULATED_STEPS), 1):
        at = booked_at + n * SANDBOX_STEP
        if at > now:
            break
        note = SANDBOX_NOTES.get(status, SANDBOX_NOTE)
        ev = event(f"{f['id']}:sandbox:{status}", "demo", status, status, at, now, by="Lalamove sandbox", note=note)
        driver = sandbox_driver(f["id"], at) if status == "driver_assigned" else None
        pop = proof("PICKED_UP", at, "demo", {"sample": SANDBOX_PICKUP}) if status == "picked_up" else None
        pod = None
        if status == "delivered":
            pod = proof("DELIVERED", at, "demo", {"sample": SANDBOX_DROPOFF.get(outcome, SANDBOX_DROPOFF["delivered"])})
        elif status == "failed":
            pod = proof("FAILED", at, "demo")
        if done := record(current, ev, driver, pod=pod, pop=pop):
            current, changed = done, True
    return current if changed else None


def sandbox_progress(order: dict, now: datetime) -> dict:
    """The order's shipment's simulated sandbox steps now due, as an update."""
    f = order.get("fulfillment") or {}
    updated = sandbox_steps(f, now) if f else None
    return {"fulfillment": updated} if updated else {}


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
