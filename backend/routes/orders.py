import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore

from core import auth, db
from domain import orders as rules
from services import payments

router = APIRouter()


@router.get("/api/orders")
def list_orders(user: dict = Depends(auth.current_user)):
    if user.get("demo"):
        docs = db.orders().where(filter=firestore.FieldFilter("source.owner", "==", user["uid"])).stream()
    else:
        # ponytail: over-fetches to skip other people's demo orders; query by source.type once demo orders outnumber real ones
        docs = db.orders().order_by("created_at", direction=firestore.Query.DESCENDING).limit(100).stream()
    mine = [d for d in docs if auth.visible(d.to_dict(), user)]
    mine.sort(key=lambda d: d.get("created_at") or datetime.max.replace(tzinfo=UTC), reverse=True)
    return [db.present(d) for d in mine[:25]]


@router.get("/api/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(auth.current_user)):
    snap = db.orders().document(order_id).get()
    if not snap.exists:
        raise HTTPException(404, "Order not found")
    return db.present(snap)


def retiring(ref, rule, by: str = "") -> dict:
    """Like change(), and if the rule dropped an unpaid payment request, expire it at PayMongo too."""
    before = (ref.get().to_dict() or {}).get("payment")
    order = db.change(ref, rule, by)
    if before and before["status"] == "pending" and not order.get("payment"):
        payments.expire_session(before["session_id"])
    return order


@router.put("/api/orders/{order_id}")
def edit_order(order_id: str, body: rules.Edit, user: dict = Depends(auth.current_user)):
    return retiring(db.orders().document(order_id), lambda o: rules.edit(o, body), user["email"])


@router.post("/api/orders/{order_id}/send-confirmation")
def send_confirmation(order_id: str, user: dict = Depends(auth.current_user)):
    return db.change(db.orders().document(order_id), lambda o: rules.send(o, secrets.token_urlsafe(32)))


@router.post("/api/orders/{order_id}/assert-confirmation")
def assert_confirmation(order_id: str, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    return db.change(db.orders().document(order_id), lambda o: rules.confirm(o, "seller_asserted", user["email"], now, token))


@router.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: rules.Cancel, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return retiring(db.orders().document(order_id), lambda o: rules.cancel(o, user["email"], body.reason, now))


@router.post("/api/orders/{order_id}/check-payment")
def check_payment(order_id: str, user: dict = Depends(auth.current_user)):
    payment = (db.orders().document(order_id).get().to_dict() or {}).get("payment")
    if not payment:
        raise HTTPException(409, "No payment has been requested for this order")
    return payments.reconcile_session(payment["session_id"])
