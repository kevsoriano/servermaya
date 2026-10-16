from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from core import config, db
from domain import delivery
from domain import orders as rules
from integrations import paymongo
from services import customer_link, payments

router = APIRouter()


@router.get("/api/public/orders/{token}")
def public_order(token: str):
    return customer_link.customer_view(customer_link.by_token(token).to_dict())


@router.post("/api/public/orders/{token}/confirm")
def public_confirm(token: str):
    now = datetime.now(UTC)

    def rule(order: dict) -> dict:
        if order.get("confirm_token") != token:  # edited between lookup and transaction
            raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
        return rules.confirm(order, "customer", "confirmation_link", now)

    return customer_link.customer_view(db.change(customer_link.by_token(token).reference, rule))


@router.post("/api/public/orders/{token}/pay")
def public_pay(token: str):
    snap = customer_link.by_token(token)
    order = snap.to_dict()
    try:
        existing = rules.reusable_payment(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if existing:
        return {"checkout_url": existing["checkout_url"]}
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
            # The redirect only brings the customer back. It never marks anything paid.
            "success_url": f"{config.PUBLIC_URL}/c/{token}?returned=1",
            "cancel_url": f"{config.PUBLIC_URL}/c/{token}",
        },
    )
    a = session["attributes"]
    now = datetime.now(UTC)
    saved = db.change(snap.reference, lambda o: rules.request_payment(o, session["id"], a["checkout_url"], a["livemode"], now))
    if saved["payment"]["session_id"] != session["id"]:
        payments.expire_session(session["id"])
    return {"checkout_url": saved["payment"]["checkout_url"]}


@router.post("/api/public/orders/{token}/check-payment")
def public_check_payment(token: str):
    payment = customer_link.by_token(token).to_dict().get("payment")
    return customer_link.customer_view(payments.reconcile_session(payment["session_id"]) if payment else customer_link.by_token(token).to_dict())


@router.post("/api/public/orders/{token}/received")
def public_received(token: str):
    now = datetime.now(UTC)
    return customer_link.customer_view(db.change(customer_link.by_token(token).reference, lambda o: delivery.acknowledge(o, "customer_link", now)))
