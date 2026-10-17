from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from core import config, db
from domain import delivery
from domain import orders as rules
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
    # The redirect only brings the customer back. It never marks anything paid.
    return {"checkout_url": payments.checkout(customer_link.by_token(token).reference, f"{config.PUBLIC_URL}/c/{token}")}


@router.post("/api/public/orders/{token}/check-payment")
def public_check_payment(token: str):
    payment = customer_link.by_token(token).to_dict().get("payment")
    return customer_link.customer_view(payments.reconcile_session(payment["session_id"]) if payment else customer_link.by_token(token).to_dict())


@router.post("/api/public/orders/{token}/received")
def public_received(token: str):
    now = datetime.now(UTC)
    return customer_link.customer_view(db.change(customer_link.by_token(token).reference, lambda o: delivery.acknowledge(o, "customer_link", now)))
