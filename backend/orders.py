"""Order versioning, confirmation and payment rules. Pure functions: each takes the stored order and returns the fields to update."""

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


class Cancel(BaseModel):
    reason: str = Field(default="", max_length=500)


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


CARRIED = ("recipient_name", "phone", "address")  # who and where; delivery instructions are for one delivery


def finished_at(order: dict) -> datetime | None:
    """When the order was cancelled or the customer said they received it; None while it is still going."""
    if order["status"] == "cancelled":
        return (order.get("cancelled") or {}).get("at")
    receipt = order.get("receipt") or {}
    return receipt.get("at") if receipt.get("status") == "customer_confirmed" else None


def covers_until(order: dict) -> datetime | None:
    """The end of the stretch of chat this order is about: its last drafted-from message, or later if follow-ups such as
    "already paid" or "received, thanks" came before it was finished."""
    ends = [datetime.fromisoformat(order["messages"][-1]["created_time"])] if order.get("messages") else []
    return max(ends + [t for t in [finished_at(order)] if t], default=None)


def since_last_order(messages: list[dict], previous: list[dict]) -> list[dict]:
    """The messages a new order is drafted from: those after every earlier order of this chat. A returning customer's old
    items, changes and follow-ups stay with the old order."""
    ends = [t for t in map(covers_until, previous) if t]
    if not ends:
        return messages
    return [m for m in messages if datetime.fromisoformat(m["created_time"]) > max(ends)]


def carry_over(draft: dict, previous: list[tuple[str, dict]]) -> dict:
    """Contact details this part of the chat does not give, from the newest earlier order the customer confirmed as it stands.

    previous: (order id, order), newest first. A carried value cites no message, only the order it came from, so it shows as
    copied and the seller checks it is still right.
    """
    for order_id, o in previous:
        if any(c["version"] == o["version"] for c in o.get("confirmations", [])):
            old = o["draft"]
            filled = {
                f: {"value": old[f]["value"], "sources": [], "from_order": order_id} for f in CARRIED if not draft[f]["value"] and old[f]["value"]
            }
            # "Welcome back, Leah!" gives only the start of the name she confirmed as "Leah Cruz": keep the full one.
            new, full = draft["recipient_name"]["value"], old["recipient_name"]["value"]
            if new and full and new.lower().split() != full.lower().split() and full.lower().split()[: len(new.split())] == new.lower().split():
                filled["recipient_name"] = {**draft["recipient_name"], "value": full, "from_order": order_id}
            return {**draft, **filled}
    return draft


def done(order: dict) -> bool:
    """Cancelled, or the customer said they received it. A new order from the same chat is then the expected next step."""
    return order["status"] == "cancelled" or (order.get("receipt") or {}).get("status") == "customer_confirmed"


def edit(order: dict, body: Edit) -> dict:
    """A seller edit always makes a new version and voids the confirmation link for the old one."""
    if order["status"] == "cancelled":
        raise Refused("This order is cancelled")
    if order.get("fulfillment"):
        raise Refused("This order has a shipment. Cancel the shipment before changing the order.")
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
    updates = {"draft": draft, "version": order["version"] + 1, "status": "draft", "confirm_token": None}
    if order.get("packing"):
        # Packing checks were for the old version. Keep them on record and start again.
        updates["packing"] = None
        updates["packing_history"] = order.get("packing_history", []) + [order["packing"]]
    if (order.get("payment") or {}).get("status") == "pending":
        updates["payment"] = None  # the unpaid request was for the old version; the caller expires it at PayMongo
    return updates


def send(order: dict, token: str) -> dict:
    if order["status"] == "awaiting_confirmation":
        return {}  # link already issued for this version
    if order["status"] != "draft":
        raise Refused(f"This order is already {order['status']}")
    if gaps := missing(order["draft"]):
        raise Refused("Fill in first: " + ", ".join(gaps))
    return {"status": "awaiting_confirmation", "confirm_token": token}


def confirm(order: dict, kind: str, by: str, now: datetime, token: str | None = None) -> dict:
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
    updates = {"status": "confirmed", "confirmations": order.get("confirmations", []) + [record]}
    if not order.get("confirm_token") and token:
        updates["confirm_token"] = token  # the customer still needs a link to pay
    return updates


def cancel(order: dict, by: str, reason: str, now: datetime) -> dict:
    """Cancelling keeps the whole record, including any received payment, and kills the customer link."""
    if order["status"] == "cancelled":
        return {}
    if order.get("fulfillment"):
        raise Refused("This order has a shipment. Cancel the shipment first.")
    cancelled = {"by": by, "at": now, "reason": reason.strip() or None, "previous_status": order["status"]}
    updates = {"status": "cancelled", "confirm_token": None, "cancelled": cancelled}
    if (order.get("payment") or {}).get("status") == "pending":
        updates["payment"] = None
    return updates


# Payment. order["payment"] is the current request; order["payment_review"] collects money that arrived unexpectedly.
# ponytail: one payment request on the order doc, split into a payments collection when partial payments or refunds land


def reusable_payment(order: dict) -> dict | None:
    """The existing request to send the customer back to, or None if a new one is needed. Refuses when neither applies."""
    if order["status"] != "confirmed":
        raise Refused("The order must be confirmed before payment")
    payment = order.get("payment")
    if payment and payment["status"] == "verified":
        raise Refused("This order is already paid")
    if payment and payment["status"] == "mismatched":
        raise Refused("A payment for this order needs the seller's review")
    manual = order.get("manual_payment") or {}
    if manual.get("status") == "seller_confirmed" and manual.get("version") == order["version"]:
        raise Refused("The seller already confirmed a direct GCash payment for this order")
    return payment if payment and payment["status"] == "pending" else None


def request_payment(order: dict, session_id: str, checkout_url: str, livemode: bool, now: datetime) -> dict:
    if reusable_payment(order):
        return {}  # another request won the race; the caller expires the spare session
    return {
        "payment": {
            "status": "pending",
            "session_id": session_id,
            "checkout_url": checkout_url,
            "amount": order["draft"]["total"],
            "version": order["version"],
            "livemode": livemode,
            "requested_at": now,
        }
    }


def reconcile(order: dict, session_id: str, session_status: str, paid: list[dict], now: datetime) -> dict:
    """Apply what PayMongo reports for one checkout session. `paid` are its successful payments: id, amount, currency, paid_at.

    Safe to call any number of times and in any order: a payment already recorded changes nothing,
    and a verified payment is never downgraded.
    """
    payment = order.get("payment")
    review = list(order.get("payment_review", []))
    current = bool(payment) and payment["session_id"] == session_id
    seen = {r["payment_id"] for r in review} | ({payment.get("payment_id")} if payment else set())
    updates = {}
    for p in paid:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        received = {"payment_id": p["id"], "paid_amount": p["amount"], "currency": p["currency"], "paid_at": p["paid_at"]}
        if not current or payment["status"] != "pending":
            reason = "Payment received on a request that was replaced or cancelled" if not current else "Second payment on the same request"
            review.append({**received, "session_id": session_id, "reason": reason, "recorded_at": now})
            updates["payment_review"] = review
        elif p["amount"] == payment["amount"] and p["currency"] == "PHP":
            payment = {**payment, **received, "status": "verified"}
        else:
            payment = {**payment, **received, "status": "mismatched", "problem": "Paid amount differs from the order total"}
    if current:
        if payment["status"] == "pending" and session_status == "expired":
            payment = {**payment, "status": "expired"}
        updates["payment"] = {**payment, "checked_at": now}  # freshness of the last successful check
    return updates
