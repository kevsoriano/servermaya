import secrets
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import delivery
from domain import orders as rules
from integrations import lalamove
from services import deliveries

router = APIRouter()


# Dispatch and delivery. Lalamove is live in its sandbox; the simulator and other couriers are labelled as such.
class DispatchSettings(BaseModel):
    address: str = Field(min_length=5, max_length=300)
    pin: str = Field(min_length=3, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=7, max_length=30)


@router.get("/api/settings/dispatch")
def get_dispatch_settings(user: dict = Depends(auth.current_user)):
    if user.get("demo"):
        return config.DEMO_PICKUP
    return (db.store_doc().get().to_dict() or {}).get("dispatch")


@router.put("/api/settings/dispatch")
def put_dispatch_settings(body: DispatchSettings, user: dict = Depends(auth.current_user)):
    """Where couriers pick up from. Lalamove needs a map pin; the seller pastes it from Google Maps."""
    try:
        lat, lng = delivery.parse_pin(body.pin)
        phone = delivery.e164(body.phone)
    except rules.Refused as e:
        raise HTTPException(422, str(e))
    saved = {
        "address": body.address.strip(),
        "lat": lat,
        "lng": lng,
        "name": body.name.strip(),
        "phone": phone,
        "updated_by": user["email"],
        "updated_at": datetime.now(UTC),
    }
    db.store_doc().set({"dispatch": saved}, merge=True)
    return saved


class QuoteIn(BaseModel):
    pin: str = Field(min_length=3, max_length=500)
    service: Literal["MOTORCYCLE", "SEDAN", "MPV", "VAN"] = "MOTORCYCLE"


def centavos_of(text: str) -> int:
    return int(round(float(text) * 100))


@router.post("/api/orders/{order_id}/dispatch/quote")
def quote_delivery(order_id: str, body: QuoteIn, user: dict = Depends(auth.current_user)):
    """A Lalamove quotation for the seller to approve. Nothing is booked yet."""
    pickup = config.DEMO_PICKUP if db.demo_order(order_id) else get_dispatch_settings(user)
    if not pickup:
        raise HTTPException(409, "Add your pickup address and pin in Settings first")
    ref, order = db.loaded(order_id)
    try:
        delivery.can_dispatch(order)
        lat, lng = delivery.parse_pin(body.pin)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    stops = [
        {"coordinates": {"lat": str(pickup["lat"]), "lng": str(pickup["lng"])}, "address": pickup["address"]},
        {"coordinates": {"lat": str(lat), "lng": str(lng)}, "address": order["draft"]["address"]["value"]},
    ]
    try:
        q = lalamove.lalamove("POST", "/v3/quotations", {"data": {"serviceType": body.service, "language": "en_PH", "stops": stops}})
    except lalamove.LalamoveNoAnswer:
        raise HTTPException(502, "Lalamove did not answer. Try again.")
    price = q.get("priceBreakdown") or {}
    quotation = {
        "id": q["quotationId"],
        "service": body.service,
        "price": centavos_of(price.get("total", "0")),
        "currency": price.get("currency", "PHP"),
        "expires_at": lalamove.lalamove_time(q.get("expiresAt")),
        "stop_ids": [s["stopId"] for s in q.get("stops", [])],
        "dropoff": [lat, lng],
        "livemode": "sandbox" not in config.LALAMOVE_URL,
    }
    now = datetime.now(UTC)
    fid = ((order.get("fulfillment") or {}).get("id")) or secrets.token_hex(8)
    return db.change(ref, lambda o: delivery.start(o, fid, "lalamove", user["email"], now, quotation=quotation))


@router.post("/api/orders/{order_id}/dispatch/book")
def book_delivery(order_id: str, user: dict = Depends(auth.current_user)):
    """Book the approved quote. The quote is locked first, so a retry cannot book twice."""
    pickup = config.DEMO_PICKUP if db.demo_order(order_id) else get_dispatch_settings(user)
    ref, order = db.loaded(order_id)
    f, d = order.get("fulfillment") or {}, order["draft"]
    try:
        phone = delivery.e164(d["phone"]["value"])
    except rules.Refused as e:
        raise HTTPException(409, f"Recipient phone: {e}")
    if not pickup or len((f.get("quotation") or {}).get("stop_ids", [])) != 2:
        raise HTTPException(409, "Get a Lalamove quote first")
    now = datetime.now(UTC)
    db.change(ref, lambda o: delivery.begin_booking(o, now))
    q = f["quotation"]
    payload = {
        "data": {
            "quotationId": q["id"],
            "sender": {"stopId": q["stop_ids"][0], "name": pickup["name"], "phone": pickup["phone"]},
            "recipients": [
                {
                    "stopId": q["stop_ids"][1],
                    "name": d["recipient_name"]["value"],
                    "phone": phone,
                    "remarks": d["delivery_instructions"]["value"] or "",
                }
            ],
            "isPODEnabled": True,
            "metadata": {"orderId": order_id, "fulfillmentId": f["id"]},
        }
    }
    try:
        placed = lalamove.lalamove("POST", "/v3/orders", payload)
    except lalamove.LalamoveNoAnswer:
        return db.change(ref, delivery.booking_uncertain)  # Lalamove's ORDER_CREATED event, matched by quotation, settles it
    except HTTPException as e:
        problem = e.detail
        db.change(ref, lambda o: delivery.booking_failed(o, problem))
        raise
    return db.change(ref, lambda o: delivery.booked(o, placed["orderId"], placed.get("shareLink"), datetime.now(UTC)))


@router.post("/api/orders/{order_id}/dispatch/release")
def release_delivery(order_id: str, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: delivery.release_booking(o, now))


@router.post("/api/orders/{order_id}/dispatch/refresh")
def refresh_delivery(order_id: str, user: dict = Depends(auth.current_user)):
    """Ask Lalamove for the order's current status, for when a webhook is late or missed."""
    ref, order = db.loaded(order_id)
    f = order.get("fulfillment") or {}
    if f.get("courier") != "lalamove" or not f.get("provider_order_id"):
        raise HTTPException(409, "No booked Lalamove order to refresh")
    try:
        o = lalamove.lalamove("GET", f"/v3/orders/{f['provider_order_id']}")
    except lalamove.LalamoveNoAnswer:
        raise HTTPException(502, "Lalamove did not answer. Status unchanged.")
    raw = o.get("status")
    eid = f"lalamove:{f['provider_order_id']}:poll:{raw}"  # one event per status seen by polling
    return db.change(ref, deliveries.lalamove_rule(f["provider_order_id"], raw, eid, datetime.now(UTC), o))


class StartIn(BaseModel):
    courier: Literal["simulator", "other"]
    tracking: str = Field(default="", max_length=200)


@router.post("/api/orders/{order_id}/dispatch/start")
def start_delivery(order_id: str, body: StartIn, user: dict = Depends(auth.current_user)):
    """The delivery simulator (every event labelled demo) or another courier whose updates the seller reports."""
    if body.courier == "other" and not body.tracking.strip():
        raise HTTPException(422, "Enter the courier and tracking number")
    now = datetime.now(UTC)
    fid = secrets.token_hex(8)
    return db.change(
        db.orders().document(order_id), lambda o: delivery.start(o, fid, body.courier, user["email"], now, tracking=body.tracking.strip() or None)
    )


class StepIn(BaseModel):
    status: Literal["driver_assigned", "picked_up", "delivered", "failed"]
    note: str = Field(default="", max_length=500)


@router.post("/api/orders/{order_id}/dispatch/step")
def step_delivery(order_id: str, body: StepIn, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)

    def rule(o: dict) -> dict:
        source = "demo" if (o.get("fulfillment") or {}).get("courier") == "simulator" else "seller_reported"
        return delivery.step(o, body.status, source, user["email"], now, body.note)

    return db.change(db.orders().document(order_id), rule)


@router.post("/api/orders/{order_id}/dispatch/cancel")
def cancel_delivery(order_id: str, body: rules.Cancel, user: dict = Depends(auth.current_user)):
    ref, order = db.loaded(order_id)
    f = order.get("fulfillment") or {}
    if f.get("status") == "booking":
        raise HTTPException(409, "A booking is waiting for Lalamove's answer. Release it once the quote expires.")
    if f.get("courier") == "lalamove" and f.get("provider_order_id"):
        try:
            lalamove.lalamove("DELETE", f"/v3/orders/{f['provider_order_id']}")
        except lalamove.LalamoveNoAnswer:
            raise HTTPException(502, "Lalamove did not answer. The booking may still be active; try again.")
    now = datetime.now(UTC)
    return db.change(ref, lambda o: delivery.cancel(o, user["email"], body.reason, now))
