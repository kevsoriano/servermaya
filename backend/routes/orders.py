import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore

from core import auth, config, db
from domain import orders as rules
from services import chats, deliveries, payments

router = APIRouter()


@router.get("/api/orders")
def list_orders(user: dict = Depends(auth.current_user)):
    """This visitor's demo orders and the shop's own (its Messenger orders), newest first."""
    own = db.orders().where(filter=firestore.FieldFilter("source.owner", "==", user["uid"])).stream()
    shop = db.orders().where(filter=firestore.FieldFilter("source.type", "==", "messenger")).stream()
    mine = [d for d in [*own, *shop] if auth.visible(d.to_dict(), user)]
    mine.sort(key=lambda d: d.get("created_at") or datetime.max.replace(tzinfo=UTC), reverse=True)
    return [db.present(d) for d in mine[:25]]


@router.get("/api/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(auth.current_user)):
    snap = db.orders().document(order_id).get()
    if not snap.exists:
        raise HTTPException(404, "Order not found")
    order = snap.to_dict()
    if rules.waiting_for(order):  # the customer may have tapped Confirm or Received in the chat since
        chats.apply_answers(order["source"]["conversation_id"])
        snap = snap.reference.get()
    if deliveries.advance_sandbox(order_id, snap.to_dict()):  # a sandbox booking's simulated steps now due
        snap = snap.reference.get()
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


# Asking in the chat. Each is one seller click that sends one message; the customer answers with the button under it. The
# order is checked first and the ask recorded only after the message went out, so a refused send leaves nothing waiting.
def chat_of(order_id: str) -> tuple:
    ref, order = db.loaded(order_id)
    if (order.get("source") or {}).get("type") not in ("messenger", "demo"):
        raise HTTPException(409, "This order did not come from a chat")
    return ref, order, order["source"]["conversation_id"]


def sent_to(updates: dict, kind: str, sent: dict) -> dict:
    """Keep the PSID the button went to: Messenger's webhook counts a tap only from that customer."""
    if sent.get("psid") and updates.get("asked"):
        updates["asked"][kind]["psid"] = sent["psid"]
    return updates


def checked(rule, order: dict) -> dict:
    try:
        return rule(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))


@router.post("/api/orders/{order_id}/ask-confirmation")
def ask_confirmation(order_id: str, user: dict = Depends(auth.current_user)):
    """Send the order summary with a Confirm button. The customer page's link is made too, as a fallback."""
    ref, order, cid = chat_of(order_id)
    now, token = datetime.now(UTC), secrets.token_urlsafe(32)
    rule = lambda o: (u := rules.send(o, token)) | rules.ask({**o, **u}, "confirm", order_id, now)
    ask = checked(rule, order)["asked"]["confirm"]
    sent = chats.send(cid, rules.summary(order), user, order_id, ask["text"], f"confirm:{order_id}:{ask['version']}")
    return db.change(ref, lambda o: sent_to(rule(o), "confirm", sent))


@router.post("/api/orders/{order_id}/payment-link")
def send_payment_link(order_id: str, user: dict = Depends(auth.current_user)):
    """Send the PayMongo QR Ph link in the chat. Only PayMongo's webhook, or a recheck with PayMongo, marks it paid."""
    ref, order, cid = chat_of(order_id)
    url = payments.checkout(ref, f"{config.PUBLIC_URL}/paid")
    chats.send(cid, f"Thank you for confirming! Pay {rules.peso(order['draft']['total'])} with QR Ph here: {url}", user, order_id)
    return db.present(ref.get())


@router.post("/api/orders/{order_id}/ask-pin")
def ask_pin(order_id: str, user: dict = Depends(auth.current_user)):
    """Ask the customer for their exact drop-off spot, as a Google Maps link the delivery panel can read."""
    ref, order, cid = chat_of(order_id)
    text = (
        "To make sure the rider finds you: open Google Maps, press and hold on your exact drop-off spot (your gate or building), "
        "tap Share, and paste the link here."
    )
    chats.send(cid, text, user, order_id)
    return db.present(ref.get())


@router.post("/api/orders/{order_id}/ask-received")
def ask_received(order_id: str, user: dict = Depends(auth.current_user)):
    """Send a Received button once the order is on its way. The courier's "delivered" never stands in for it."""
    ref, order, cid = chat_of(order_id)
    now = datetime.now(UTC)

    def rule(o: dict) -> dict:
        if (o.get("fulfillment") or {}).get("status") not in ("picked_up", "delivered"):
            raise rules.Refused("The order has not been sent yet")
        return rules.ask(o, "received", order_id, now)

    ask = checked(rule, order)["asked"]["received"]
    text = (
        "Your order is on its way! Before you open the parcel, start a video showing its label, and keep filming as you open it. "
        "Once it's in your hands, tap Received below. If anything is wrong, tell us here and send the video."
    )
    sent = chats.send(cid, text, user, order_id, ask["text"], f"received:{order_id}")
    return db.change(ref, lambda o: sent_to(rule(o), "received", sent))
