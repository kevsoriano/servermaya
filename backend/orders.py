"""Order versioning and confirmation rules. Pure functions: each takes the stored order and returns the fields to update."""

from datetime import datetime

from pydantic import BaseModel, Field

FIELDS = ("recipient_name", "phone", "address", "delivery_instructions")
REQUIRED = {"recipient_name": "Recipient name", "phone": "Phone number", "address": "Delivery address"}


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    variant: str | None = Field(default=None, max_length=200)
    quantity: int = Field(ge=1, le=9999)
    unit_price: int | None = Field(default=None, ge=0, le=100_000_000, description="Centavos")


class Edit(BaseModel):
    items: list[ItemIn] = Field(max_length=50)
    recipient_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    delivery_instructions: str | None = Field(default=None, max_length=500)


class Refused(Exception):
    """The action is not allowed in the order's current state. The message is shown to the seller."""


def total(items: list[dict]) -> int | None:
    if not items or any(i["unit_price"] is None for i in items):
        return None
    return sum(i["quantity"] * i["unit_price"] for i in items)


def missing(draft: dict) -> list[str]:
    """What still has to be filled in before the customer can be asked to confirm."""
    out = []
    if not draft["items"]:
        out.append("At least one item")
    elif draft["total"] is None:
        out.append("A price for every item")
    return out + [label for key, label in REQUIRED.items() if not draft[key]["value"]]


def edit(order: dict, body: Edit) -> dict:
    """A seller edit always makes a new version and voids the confirmation link for the old one."""
    if order["status"] == "cancelled":
        raise Refused("This order is cancelled")
    old = order["draft"]
    key = lambda i: (i["name"], i["variant"], i["quantity"], i["unit_price"])
    cited = {key(i): i["sources"] for i in old["items"]}
    # An unchanged item keeps its source messages. Anything the seller typed has none and shows as seller-entered.
    items = [{**i, "sources": cited.get(key(i), [])} for i in (i.model_dump() for i in body.items)]
    fields = {}
    for f in FIELDS:
        value = (getattr(body, f) or "").strip() or None
        fields[f] = old[f] if value == old[f]["value"] else {"value": value, "sources": []}
    draft = {**old, "items": items, "total": total(items), **fields}
    if draft == old:
        return {}
    return {"draft": draft, "version": order["version"] + 1, "status": "draft", "confirm_token": None}


def send(order: dict, token: str) -> dict:
    if order["status"] == "awaiting_confirmation":
        return {}  # link already issued for this version
    if order["status"] != "draft":
        raise Refused(f"This order is already {order['status']}")
    if gaps := missing(order["draft"]):
        raise Refused("Fill in first: " + ", ".join(gaps))
    return {"status": "awaiting_confirmation", "confirm_token": token}


def confirm(order: dict, kind: str, by: str, now: datetime) -> dict:
    """kind is 'customer' (they pressed Confirm on the link) or 'seller_asserted' (seller says they agreed)."""
    if order["status"] == "confirmed":
        return {}
    allowed = ("awaiting_confirmation",) if kind == "customer" else ("draft", "awaiting_confirmation")
    if order["status"] not in allowed:
        raise Refused("This order cannot be confirmed right now")
    if gaps := missing(order["draft"]):
        raise Refused("Fill in first: " + ", ".join(gaps))
    record = {"version": order["version"], "kind": kind, "by": by, "at": now}
    # Appended, never replaced: earlier agreements stay on record.
    return {"status": "confirmed", "confirmations": order.get("confirmations", []) + [record]}
