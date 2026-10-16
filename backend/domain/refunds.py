"""Refunds after an approved claim. Pure functions over the stored order.

order["refunds"] lists every attempt, never deleted. The amount still refundable counts refunds in progress, so two
submissions cannot both pass. PayMongo is asked first with an operation ID in its metadata; an answer that never came
is reconciled from the payment's refund list before anything is retried.

PayMongo refuses API refunds of QR Ph payments ("Refunds are not allowed for payments with source type qrph", seen in test
mode). Then the seller refunds another way: in the PayMongo Dashboard (picked up when reconciling, so provider-verified),
or outside PayMongo, e.g. a GCash transfer, which is recorded as seller_reported and never shown as verified.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .claims import claim, paid_amount, put
from .orders import Refused

HOLDING = ("submitting", "uncertain", "pending", "succeeded", "seller_reported", "awaiting_reconcile")
STALLED = timedelta(minutes=2)


class RefundIn(BaseModel):
    claim_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=1, description="Centavos")
    reason: Literal["duplicate", "fraudulent", "requested_by_customer", "others"] = "requested_by_customer"


class ManualIn(BaseModel):
    claim_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=1)
    method: Literal["paymongo_dashboard", "outside_paymongo"]
    reference: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=500)


def refundable(order: dict) -> int:
    paid = paid_amount(order) or 0
    return paid - sum(r["amount"] for r in order.get("refunds", []) if r["status"] in HOLDING)


def approved(order: dict, cid: str, amount: int) -> dict:
    c = claim(order, cid)
    decision = c["decision"] or {}
    if decision.get("action") not in ("refund", "partial_refund") or c["status"] not in ("resolution_approved", "executing"):
        raise Refused("Approve a refund on this report first")
    already = sum(r["amount"] for r in order.get("refunds", []) if r.get("claim_id") == cid and r["status"] in HOLDING)
    if amount > decision["amount"] - already:
        raise Refused(f"The approved refund leaves ₱{(decision['amount'] - already) / 100:,.2f} to refund")
    if amount > refundable(order):
        raise Refused(f"Only ₱{refundable(order) / 100:,.2f} of this payment can still be refunded")
    return c


def reserve(order: dict, op_id: str, body: RefundIn, by: str, now: datetime) -> dict:
    """Hold the amount and lock before PayMongo is called. One submission at a time per order."""
    payment = order.get("payment") or {}
    if payment.get("status") != "verified" or not payment.get("payment_id"):
        raise Refused("Only a PayMongo payment can be refunded through PayMongo. Record a refund made another way instead.")
    if any(r["status"] in ("submitting", "uncertain") for r in order.get("refunds", [])):
        raise Refused("Another refund is still being submitted. Check its status first.")
    c = approved(order, body.claim_id, body.amount)
    record = {
        "id": op_id,
        "claim_id": c["id"],
        "method": "paymongo_api",
        "payment_id": payment["payment_id"],
        "amount": body.amount,
        "reason": body.reason,
        "status": "submitting",
        "provider_refund_id": None,
        "by": by,
        "at": now,
    }
    return {"refunds": order.get("refunds", []) + [record], **put(order, {**c, "status": "executing"}, by, now)}


def submitted(order: dict, op_id: str, answer: dict, now: datetime) -> dict:
    """PayMongo's answer: {"refund": {id, status}} accepted, {"refused": detail} refused, or {} when no answer came."""

    def update(**fields):
        return {"refunds": [{**r, **fields, "updated_at": now} if r["id"] == op_id else r for r in order.get("refunds", [])]}

    if "refund" in answer:
        return update(status=answer["refund"]["status"], provider_refund_id=answer["refund"]["id"])
    if "refused" in answer:
        return update(status="api_refused", problem=answer["refused"])  # releases the amount: try the manual route
    return update(status="uncertain")


def manual(order: dict, op_id: str, body: ManualIn, by: str, now: datetime) -> dict:
    """A refund the seller made without the API. Dashboard refunds wait to be found at PayMongo; others stay seller-reported."""
    c = approved(order, body.claim_id, body.amount)
    if body.method == "outside_paymongo" and not body.reference.strip():
        raise Refused("Enter the reference of the transfer you made")
    payment = order.get("payment") or {}
    if body.method == "paymongo_dashboard" and payment.get("status") != "verified":
        raise Refused("This order was not paid through PayMongo")
    record = {
        "id": op_id,
        "claim_id": c["id"],
        "method": body.method,
        "payment_id": payment.get("payment_id"),
        "amount": body.amount,
        "status": "awaiting_reconcile" if body.method == "paymongo_dashboard" else "seller_reported",
        "reference": body.reference.strip() or None,
        "note": body.note.strip() or None,
        "provider_refund_id": None,
        "by": by,
        "at": now,
    }
    return {"refunds": order.get("refunds", []) + [record], **put(order, {**c, "status": "executing"}, by, now)}


def reconcile(order: dict, provider: list[dict], now: datetime) -> dict:
    """Apply the refunds PayMongo lists on the payment: [{id, amount, status, metadata}]. Safe to repeat.

    Matched by our operation ID first, then a Dashboard refund waiting with the same amount. A refund we did not start
    is still recorded, so the balance stays right.
    """
    refunds = [dict(r) for r in order.get("refunds", [])]
    for p in provider:
        mine = next((r for r in refunds if r["provider_refund_id"] == p["id"]), None)
        mine = mine or next((r for r in refunds if r["id"] == (p.get("metadata") or {}).get("operation_id")), None)
        mine = mine or next((r for r in refunds if r["status"] == "awaiting_reconcile" and r["amount"] == p["amount"]), None)
        if mine:
            mine.update(status=p["status"], provider_refund_id=p["id"], updated_at=now)
        else:
            refunds.append(
                {
                    "id": p["id"],
                    "claim_id": None,
                    "method": "paymongo_dashboard",
                    "payment_id": (order.get("payment") or {}).get("payment_id"),
                    "amount": p["amount"],
                    "status": p["status"],
                    "provider_refund_id": p["id"],
                    "by": "PayMongo",
                    "at": now,
                    "note": "Found at PayMongo; not started in this app",
                }
            )
    for r in refunds:
        # A "submitting" refund may still be in flight, so its absence proves nothing until it is old enough to have stalled.
        lost = r["status"] == "uncertain" or (r["status"] == "submitting" and now - r["at"] > STALLED)
        if lost and not any(p["id"] == r["provider_refund_id"] for p in provider):
            r.update(status="not_found", updated_at=now)  # PayMongo has no such refund: the attempt never landed
    return {"refunds": refunds} if refunds != order.get("refunds", []) else {}
