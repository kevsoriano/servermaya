import re
from datetime import UTC, datetime

from fastapi import HTTPException
from google.cloud import firestore

from core import db
from domain import orders as rules
from domain import refunds
from integrations import paymongo


def expire_session(session_id: str) -> None:
    """Best effort. If it fails and the customer pays anyway, reconcile() routes that payment to review."""
    try:
        paymongo.paymongo("POST", f"checkout_sessions/{session_id}/expire")
    except HTTPException:
        pass


def reconcile_session(session_id: str) -> dict:
    """Fetch the session from PayMongo and apply it to its order. Used by the webhook and by every manual recheck."""
    if not re.fullmatch(r"cs_\w+", session_id):
        raise HTTPException(400, "Invalid session id")
    session = paymongo.paymongo("GET", f"checkout_sessions/{session_id}")
    a = session["attributes"]
    order_id = str((a.get("metadata") or {}).get("order_id", ""))
    if not re.fullmatch(r"\w{1,64}", order_id):
        raise HTTPException(404, "Session does not belong to an order")
    paid = [
        {"id": p["id"], "amount": p["attributes"]["amount"], "currency": p["attributes"]["currency"], "paid_at": p["attributes"].get("paid_at")}
        for p in a.get("payments", [])
        if p["attributes"].get("status") == "paid"
    ]
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: rules.reconcile(o, session_id, a["status"], paid, now))


def reconcile_refunds_for(payment_id: str) -> dict | None:
    found = list(db.orders().where(filter=firestore.FieldFilter("payment.payment_id", "==", payment_id)).limit(1).stream())
    if not found:
        return None
    listed = paymongo.provider_refunds(payment_id)
    return db.change(found[0].reference, lambda o: refunds.reconcile(o, listed, datetime.now(UTC)))
