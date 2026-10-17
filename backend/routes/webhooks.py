import hmac
import json
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from google.cloud import firestore

from core import config, db
from integrations import lalamove, messenger, paymongo
from services import chats, deliveries, payments

router = APIRouter()


@router.post("/api/webhooks/paymongo")
async def paymongo_webhook(request: Request):
    raw = await request.body()
    if not config.PAYMONGO_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook is not configured")
    if not paymongo.valid_signature(request.headers.get("Paymongo-Signature", ""), raw, config.PAYMONGO_WEBHOOK_SECRET):
        raise HTTPException(401, "Bad signature")
    try:
        event = json.loads(raw)["data"]["attributes"]
        subject = event["data"]
        subject_id = subject["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Unrecognised event")
    # Each event only tells us where to look; what we fetch from PayMongo is what counts. Duplicates are harmless.
    if event.get("type") == "checkout_session.payment.paid":
        await run_in_threadpool(payments.reconcile_session, subject_id)
    elif event.get("type") in ("payment.refunded", "payment.refund.updated"):
        payment_id = (subject.get("attributes") or {}).get("payment_id") or (subject_id if subject.get("type") == "payment" else None)
        if payment_id:
            await run_in_threadpool(payments.reconcile_refunds_for, payment_id)
    return {"received": True}


@router.get("/api/webhooks/messenger")
def messenger_verify(request: Request):
    """Meta's one-time check when the webhook is set up: echo the challenge if the verify token is ours."""
    q = request.query_params
    token = config.MESSENGER_VERIFY_TOKEN
    if not token or q.get("hub.mode") != "subscribe" or not hmac.compare_digest(q.get("hub.verify_token", ""), token):
        raise HTTPException(403, "Verification failed")
    return PlainTextResponse(q.get("hub.challenge", ""))


@router.post("/api/webhooks/messenger")
async def messenger_webhook(request: Request):
    """Messages pushed by Meta. Only a tap on a Confirm or Received button is acted on, at once; everything else in the inbox
    is still read from Messenger when a seller opens it. Retries are harmless: a confirmed order is not confirmed again."""
    raw = await request.body()
    if not config.MESSENGER_APP_SECRET:
        raise HTTPException(503, "Webhook is not configured")
    if not messenger.valid_signature(request.headers.get("X-Hub-Signature-256", ""), raw, config.MESSENGER_APP_SECRET):
        raise HTTPException(401, "Bad signature")
    try:
        events = [e for entry in json.loads(raw).get("entry", []) for e in entry.get("messaging", [])]
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Unrecognised event")
    for event in events:
        await run_in_threadpool(chats.apply_tap, event)
    return {"received": True}


def shipment_ref(provider_order_id: str | None, quotation_id: str | None = None):
    for field, value in (("fulfillment.provider_order_id", provider_order_id), ("fulfillment.quotation.id", quotation_id)):
        if value:
            found = list(db.orders().where(filter=firestore.FieldFilter(field, "==", value)).limit(1).stream())
            if found:
                return found[0].reference
    return None


@router.post("/api/webhooks/lalamove")
async def lalamove_webhook(request: Request):
    """Lalamove order events. Always answers 200, because Lalamove pings the URL when it is registered and retries otherwise.

    Only events with our API key and a valid signature are stored and acted on. The raw event is kept; the event ID dedupes.
    """
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        return {"received": True}
    if not (config.LALAMOVE_KEY and isinstance(event, dict) and event.get("apiKey") == config.LALAMOVE_KEY and len(raw) < 65536):
        return {"received": True}
    data = event.get("data")
    bodies = {json.dumps(data, separators=(",", ":")), json.dumps(data, separators=(",", ":"), ensure_ascii=False)}
    expected = {lalamove.lalamove_signature(str(event.get("timestamp")), "POST", request.url.path, b) for b in bodies}
    if not any(hmac.compare_digest(e, str(event.get("signature", ""))) for e in expected):
        return {"received": True}
    doc = {"provider": "lalamove", "raw": raw.decode(), "path": request.url.path, "verified": True, "received_at": firestore.SERVER_TIMESTAMP}
    ref = db.clients()[1].collection("webhookEvents").document(f"lalamove_{event.get('eventId') or secrets.token_hex(8)}")
    try:
        await run_in_threadpool(ref.create, doc)
    except Exception:
        return {"received": True}  # duplicate delivery of an event we already have
    o = (data or {}).get("order") or {}
    kind = event.get("eventType")
    if o.get("orderId") and kind in ("ORDER_CREATED", "ORDER_STATUS_CHANGED", "DRIVER_ASSIGNED"):
        target = await run_in_threadpool(shipment_ref, o["orderId"], o.get("quotationId"))
        raw_status = None if kind == "ORDER_CREATED" else (o.get("status") or kind)
        if target:
            rule = deliveries.lalamove_rule(
                o["orderId"], raw_status, f"lalamove:{event['eventId']}", lalamove.lalamove_time(data.get("updatedAt")), o
            )
            try:
                await run_in_threadpool(db.change, target, rule)
            except HTTPException:
                pass  # the event is stored; a refresh reconciles from Lalamove
    return {"received": True}
