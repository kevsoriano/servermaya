import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from core import auth, config, db
from domain import refunds
from integrations import paymongo
from services import payments

router = APIRouter()


@router.post("/api/orders/{order_id}/refunds")
def submit_refund(order_id: str, body: refunds.RefundIn, user: dict = Depends(auth.current_user)):
    if not config.PAYMONGO_KEY:
        raise HTTPException(503, "Payments are not configured")
    ref = db.orders().document(order_id)
    op_id, now = "op_" + secrets.token_hex(8), datetime.now(UTC)
    saved = db.change(ref, lambda o: refunds.reserve(o, op_id, body, user["email"], now))
    payment_id = saved["payment"]["payment_id"]
    answer = paymongo.paymongo_refund(payment_id, body.amount, body.reason, op_id, order_id)
    return db.change(ref, lambda o: refunds.submitted(o, op_id, answer, datetime.now(UTC)))


@router.post("/api/orders/{order_id}/refunds/manual")
def manual_refund(order_id: str, body: refunds.ManualIn, user: dict = Depends(auth.current_user)):
    op_id, now = "op_" + secrets.token_hex(8), datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: refunds.manual(o, op_id, body, user["email"], now))


@router.post("/api/orders/{order_id}/refunds/check")
def check_refunds(order_id: str, user: dict = Depends(auth.current_user)):
    """Ask PayMongo which refunds exist on the payment. Settles unanswered submissions and finds Dashboard refunds."""
    _, order = db.loaded(order_id)
    payment_id = (order.get("payment") or {}).get("payment_id")
    if not payment_id:
        raise HTTPException(409, "This order has no PayMongo payment")
    return payments.reconcile_refunds_for(payment_id)
