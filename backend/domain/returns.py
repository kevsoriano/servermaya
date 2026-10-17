"""Returns: the customer sends the item back on an approved claim, the seller inspects what came back, and a refund on that
claim waits for the item. Pure functions over the stored order, like orders.py.

order["returns"] holds one return per claim. The customer sends the item back themselves, with a courier of their choice;
the seller books nothing. The seller records the courier and tracking number the customer gives (customer-reported, never
checked), marks the item received once it is in hand, then records what came back. Code, not AI, holds a refund on the
claim until the item is received; what the inspection finds never blocks it, the seller decides.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .claims import CLOSED, claim
from .orders import Refused

RETURNABLE = ("refund", "partial_refund", "replacement")  # outcomes where the item can come back
WAITING = ("requested", "shipping")  # the item is not back yet
MAX_PHOTOS = 8
RESULTS = {
    "same_item": "The item we sent",
    "different_item": "A different item",
    "damaged_as_reported": "Damaged as reported",
    "unclear": "Unclear",
}


class InspectionIn(BaseModel):
    result: Literal["same_item", "different_item", "damaged_as_reported", "unclear"]
    condition: str = Field(min_length=1, max_length=1000)
    weight_g: int | None = Field(default=None, ge=1, le=1_000_000)
    seal_id: str = Field(default="", max_length=60)


def for_claim(order: dict, cid: str) -> dict | None:
    return next((r for r in order.get("returns", []) if r["claim_id"] == cid), None)


def find(order: dict, rid: str) -> dict:
    found = next((r for r in order.get("returns", []) if r["id"] == rid), None)
    if not found:
        raise Refused("No such return")
    return found


def put(order: dict, updated: dict) -> dict:
    return {"returns": [updated if r["id"] == updated["id"] else r for r in order.get("returns", [])]}


def request(order: dict, rid: str, cid: str, by: str, now: datetime) -> dict:
    """Ask for the item back on an approved refund or replacement. The chat message itself is a separate seller send."""
    c = claim(order, cid)
    if c["status"] in CLOSED:
        raise Refused("This report is closed")
    if (c["decision"] or {}).get("action") not in RETURNABLE:
        raise Refused("Approve a refund or replacement before asking for the item back")
    if for_claim(order, cid):
        raise Refused("A return is already open for this report")
    new = {
        "id": rid,
        "claim_id": cid,
        "status": "requested",
        "requested_by": by,
        "requested_at": now,
        "sent": None,
        "sent_history": [],
        "received": None,
        "photos": [],
        "check": None,
        "inspection": None,
        "inspection_history": [],
    }
    return {"returns": order.get("returns", []) + [new]}


def holds_refund(order: dict, cid: str) -> str | None:
    """Why a refund on this claim must wait, or None. Only a return that was asked for and has not arrived holds it."""
    r = for_claim(order, cid)
    if r and r["status"] in WAITING:
        return "The refund waits for the returned item: mark it received first"
    return None


def sent(order: dict, rid: str, courier: str, tracking: str, by: str, now: datetime) -> dict:
    """The customer says they sent it: the courier and tracking number they gave, as the customer reported them. A correction
    replaces the details shown and keeps the earlier ones."""
    r = find(order, rid)
    if r["status"] not in WAITING:
        raise Refused("The item is already back")
    if not courier.strip():
        raise Refused("Enter the courier the customer used")
    record = {"courier": courier.strip(), "tracking": tracking.strip() or None, "source": "customer_reported", "by": by, "at": now}
    history = r["sent_history"] + ([r["sent"]] if r["sent"] else [])
    return put(order, {**r, "status": "shipping", "sent": record, "sent_history": history})


def receive(order: dict, rid: str, by: str, now: datetime) -> dict:
    """The seller has the item in hand, by courier or by hand. This, not the customer's word that it was sent, is what releases a
    held refund."""
    r = find(order, rid)
    if r["status"] not in WAITING:
        return {}
    return put(order, {**r, "status": "received", "received": {"by": by, "at": now}})


def add_photo(order: dict, rid: str, photo: dict) -> dict:
    r = find(order, rid)
    if r["status"] in WAITING:
        raise Refused("Mark the item received before photographing it")
    if len(r["photos"]) >= MAX_PHOTOS:
        raise Refused(f"Up to {MAX_PHOTOS} photos per return")
    return put(order, {**r, "photos": r["photos"] + [photo]})


def record_check(order: dict, rid: str, rows: list[dict], model: str, now: datetime) -> dict:
    """The photo check of the returned item against the order's lines, as the packing check reads them."""
    r = find(order, rid)
    return put(order, {**r, "check": {"rows": rows, "model": model, "at": now}})


def inspect(order: dict, rid: str, body: InspectionIn, by: str, now: datetime) -> dict:
    """What came back, in the seller's judgment. A different item flags a possible swap; it decides nothing by itself."""
    r = find(order, rid)
    if r["status"] in WAITING:
        raise Refused("Mark the item received first")
    if not r["photos"]:
        raise Refused("Add at least one photo of the returned item")
    inspection = {**body.model_dump(), "condition": body.condition.strip(), "seal_id": body.seal_id.strip() or None, "by": by, "at": now}
    history = r["inspection_history"] + ([r["inspection"]] if r["inspection"] else [])
    return put(order, {**r, "status": "inspected", "inspection": inspection, "inspection_history": history})
