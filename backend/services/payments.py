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


def checkout(ref, back_url: str) -> str:
    """The QR Ph checkout link for this order's version: the pending one, or a new PayMongo session. back_url is where
    PayMongo sends the customer after paying (with ?returned=1) or cancelling."""
    snap = ref.get()
    order = snap.to_dict()
    try:
        existing = rules.reusable_payment(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if existing:
        return existing["checkout_url"]
    d = order["draft"]
    session = paymongo.paymongo(
        "POST",
        "checkout_sessions",
        {
            "line_items": [
                {"name": " ".join(filter(None, [i["name"], i["variant"]])), "amount": i["unit_price"], "currency": "PHP", "quantity": i["quantity"]}
                for i in d["items"]
            ],
            "payment_method_types": ["qrph"],
            "reference_number": f"{snap.id}-v{order['version']}",
            "description": f"Order {snap.id} version {order['version']}",
            "metadata": {"order_id": snap.id, "order_version": str(order["version"])},
            "billing": {"name": d["recipient_name"]["value"], "phone": d["phone"]["value"]},
            "success_url": f"{back_url}?returned=1",
            "cancel_url": back_url,
        },
    )
    a = session["attributes"]
    now = datetime.now(UTC)
    saved = db.change(ref, lambda o: rules.request_payment(o, session["id"], a["checkout_url"], a["livemode"], now))
    if saved["payment"]["session_id"] != session["id"]:
        expire_session(session["id"])
    return saved["payment"]["checkout_url"]


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
