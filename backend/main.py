import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google import genai
from google.cloud import firestore
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

import extraction
import orders as rules

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")
# Comma-separated. Empty means nobody gets in.
ALLOWLIST = {e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAIL", "").split(",") if e.strip()}
PAGE_ID = os.environ.get("MESSENGER_PAGE_ID", "")
PAGE_TOKEN = os.environ.get("MESSENGER_PAGE_TOKEN", "")
GRAPH = "https://graph.facebook.com"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# ponytail: one store, add real store ids when a second seller onboards
STORE = "main"
PAYMONGO_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")
PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")
LALAMOVE_KEY = os.environ.get("LALAMOVE_API_KEY", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://servermaya-ai-builder-cup.web.app")

app = FastAPI()
_http = google_requests.Request()


def current_user(authorization: str = Header(default="")) -> dict:
    """Verify the Firebase ID token and the email allowlist. Every protected route depends on this."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = id_token.verify_firebase_token(token, _http, audience=PROJECT_ID)
    except Exception:
        raise HTTPException(401, "Invalid token")
    if claims.get("iss") != f"https://securetoken.google.com/{PROJECT_ID}":
        raise HTTPException(401, "Invalid token")
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in ALLOWLIST:
        raise HTTPException(403, "This account is not allowed")
    return {"uid": claims["sub"], "email": email, "name": claims.get("name")}


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


def graph(path: str, **params) -> dict:
    """GET from the Graph API as the Page. Token goes in a header so it never lands in a URL or log."""
    if not PAGE_ID or not PAGE_TOKEN:
        raise HTTPException(503, "Messenger is not configured")
    try:
        r = requests.get(f"{GRAPH}/{path}", params=params, headers={"Authorization": f"Bearer {PAGE_TOKEN}"}, timeout=10)
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach Messenger")
    if not r.ok:
        raise HTTPException(502, body.get("error", {}).get("message", "Messenger request failed"))
    return body


# ponytail: first page only (25 threads, 50 messages), add paging when a Page outgrows it
@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user)):
    data = graph(f"{PAGE_ID}/conversations", platform="messenger", fields="id,updated_time,snippet,participants")
    return [
        {
            "id": c["id"],
            "updated_time": c.get("updated_time"),
            "snippet": c.get("snippet", ""),
            "customer": next((p for p in c.get("participants", {}).get("data", []) if p.get("id") != PAGE_ID), None),
        }
        for c in data.get("data", [])
    ]


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)):
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    return fetch_messages(conversation_id)


def fetch_messages(conversation_id: str) -> list[dict]:
    data = graph(f"{conversation_id}/messages", fields="id,message,from,created_time,attachments", limit=50)
    return [
        {
            "id": m["id"],
            "text": m.get("message", ""),
            "from_page": m.get("from", {}).get("id") == PAGE_ID,
            "from_name": m.get("from", {}).get("name"),
            "created_time": m.get("created_time"),
            "attachments": len(m.get("attachments", {}).get("data", [])),
        }
        for m in reversed(data.get("data", []))  # Graph returns newest first
    ]


@lru_cache
def clients():
    """Created on first use so tests and Messenger-only routes need no Google credentials."""
    return genai.Client(vertexai=True, project=PROJECT_ID, location="global"), firestore.Client(project=PROJECT_ID)


def orders():
    return clients()[1].collection("stores").document(STORE).collection("orders")


@app.post("/api/conversations/{conversation_id}/draft-order")
def draft_order(conversation_id: str, user: dict = Depends(current_user)):
    messages = conversation(conversation_id, user)
    if not messages:
        raise HTTPException(400, "Conversation has no messages")
    try:
        draft = extraction.extract(clients()[0], GEMINI_MODEL, messages)
    except Exception:
        raise HTTPException(502, "Could not extract an order from this conversation")
    order = {
        "status": "draft",
        "version": 1,
        "source": {"type": "messenger", "conversation_id": conversation_id},
        "messages": messages,  # snapshot of the evidence the draft was built from
        "draft": draft,  # copy of the current version, for listing
        "confirmations": [],
        "confirm_token": None,
        "model": GEMINI_MODEL,
        "created_by": user["email"],
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    ref = orders().document()
    batch = clients()[1].batch()
    batch.set(ref, order)
    batch.set(ref.collection("versions").document("1"), version_doc(draft, "ai_extraction", user["email"]))
    batch.commit()
    return present(ref.get())


def version_doc(draft: dict, origin: str, by: str) -> dict:
    return {"draft": draft, "origin": origin, "created_by": by, "created_at": firestore.SERVER_TIMESTAMP}


def present(snap) -> dict:
    # Defaults cover drafts saved before confirmations existed.
    order = {"confirmations": [], "confirm_token": None, **snap.to_dict()}
    return {**order, "id": snap.id, "missing": rules.missing(order["draft"])}


def change(ref, rule, by: str = "") -> dict:
    """Apply one of the rules in orders.py inside a transaction, so an edit and a confirmation cannot interleave."""

    @firestore.transactional
    def run(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise HTTPException(404, "Order not found")
        try:
            updates = rule(snap.to_dict())
        except rules.Refused as e:
            raise HTTPException(409, str(e))
        if updates:
            tx.update(ref, updates)
        if "draft" in updates:  # versions are written once and never changed
            tx.set(ref.collection("versions").document(str(updates["version"])), version_doc(updates["draft"], "seller_edit", by))

    run(clients()[1].transaction())
    return present(ref.get())


@app.get("/api/orders")
def list_orders(user: dict = Depends(current_user)):
    docs = orders().order_by("created_at", direction=firestore.Query.DESCENDING).limit(25).stream()
    return [present(d) for d in docs]


def retiring(ref, rule, by: str = "") -> dict:
    """Like change(), and if the rule dropped an unpaid payment request, expire it at PayMongo too."""
    before = (ref.get().to_dict() or {}).get("payment")
    order = change(ref, rule, by)
    if before and before["status"] == "pending" and not order.get("payment"):
        expire_session(before["session_id"])
    return order


@app.put("/api/orders/{order_id}")
def edit_order(order_id: str, body: rules.Edit, user: dict = Depends(current_user)):
    return retiring(orders().document(order_id), lambda o: rules.edit(o, body), user["email"])


@app.post("/api/orders/{order_id}/send-confirmation")
def send_confirmation(order_id: str, user: dict = Depends(current_user)):
    return change(orders().document(order_id), lambda o: rules.send(o, secrets.token_urlsafe(32)))


@app.post("/api/orders/{order_id}/assert-confirmation")
def assert_confirmation(order_id: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    return change(orders().document(order_id), lambda o: rules.confirm(o, "seller_asserted", user["email"], now, token))


@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: rules.Cancel, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return retiring(orders().document(order_id), lambda o: rules.cancel(o, user["email"], body.reason, now))


@app.post("/api/orders/{order_id}/check-payment")
def check_payment(order_id: str, user: dict = Depends(current_user)):
    payment = (orders().document(order_id).get().to_dict() or {}).get("payment")
    if not payment:
        raise HTTPException(409, "No payment has been requested for this order")
    return reconcile_session(payment["session_id"])


# Customer link routes. No sign-in: the unguessable token in the link is the credential.
def by_token(token: str):
    # ponytail: token stored as-is so the seller can re-copy the link; store a hash if the DB ever gets other readers
    found = list(orders().where(filter=firestore.FieldFilter("confirm_token", "==", token)).limit(1).stream()) if token else []
    if not found:
        raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
    return found[0]


def customer_view(order: dict) -> dict:
    """Only what the customer needs to check. No chat, claims or seller details."""
    d = order["draft"]
    return {
        "status": order["status"],
        "items": [{k: i[k] for k in ("name", "variant", "quantity", "unit_price")} for i in d["items"]],
        "total": d["total"],
        **{f: d[f]["value"] for f in rules.FIELDS},
        "payment": (order.get("payment") or {}).get("status"),
        "test_mode": (order.get("payment") or {}).get("livemode") is False,
    }


@app.get("/api/public/orders/{token}")
def public_order(token: str):
    return customer_view(by_token(token).to_dict())


@app.post("/api/public/orders/{token}/confirm")
def public_confirm(token: str):
    now = datetime.now(timezone.utc)

    def rule(order: dict) -> dict:
        if order.get("confirm_token") != token:  # edited between lookup and transaction
            raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
        return rules.confirm(order, "customer", "confirmation_link", now)

    return customer_view(change(by_token(token).reference, rule))


# PayMongo. It owns payment truth: an order is only marked paid from a checkout session we fetched ourselves.
def paymongo(method: str, path: str, attributes: dict | None = None) -> dict:
    if not PAYMONGO_KEY:
        raise HTTPException(503, "Payments are not configured")
    try:
        r = requests.request(
            method,
            f"https://api.paymongo.com/v1/{path}",
            auth=(PAYMONGO_KEY, ""),
            json={"data": {"attributes": attributes}} if attributes else None,
            timeout=15,
        )
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach PayMongo. Payment status is unknown; try again.")
    if not r.ok:
        raise HTTPException(502, "PayMongo: " + "; ".join(e.get("detail", "") for e in body.get("errors", [])))
    return body["data"]


def expire_session(session_id: str) -> None:
    """Best effort. If it fails and the customer pays anyway, reconcile() routes that payment to review."""
    try:
        paymongo("POST", f"checkout_sessions/{session_id}/expire")
    except HTTPException:
        pass


def reconcile_session(session_id: str) -> dict:
    """Fetch the session from PayMongo and apply it to its order. Used by the webhook and by every manual recheck."""
    if not re.fullmatch(r"cs_\w+", session_id):
        raise HTTPException(400, "Invalid session id")
    session = paymongo("GET", f"checkout_sessions/{session_id}")
    a = session["attributes"]
    order_id = str((a.get("metadata") or {}).get("order_id", ""))
    if not re.fullmatch(r"\w{1,64}", order_id):
        raise HTTPException(404, "Session does not belong to an order")
    paid = [
        {"id": p["id"], "amount": p["attributes"]["amount"], "currency": p["attributes"]["currency"], "paid_at": p["attributes"].get("paid_at")}
        for p in a.get("payments", [])
        if p["attributes"].get("status") == "paid"
    ]
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: rules.reconcile(o, session_id, a["status"], paid, now))


@app.post("/api/public/orders/{token}/pay")
def public_pay(token: str):
    snap = by_token(token)
    order = snap.to_dict()
    try:
        existing = rules.reusable_payment(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if existing:
        return {"checkout_url": existing["checkout_url"]}
    d = order["draft"]
    session = paymongo(
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
            "success_url": f"{PUBLIC_URL}/c/{token}?returned=1",
            "cancel_url": f"{PUBLIC_URL}/c/{token}",
        },
    )
    a = session["attributes"]
    now = datetime.now(timezone.utc)
    saved = change(snap.reference, lambda o: rules.request_payment(o, session["id"], a["checkout_url"], a["livemode"], now))
    if saved["payment"]["session_id"] != session["id"]:
        expire_session(session["id"])
    return {"checkout_url": saved["payment"]["checkout_url"]}


@app.post("/api/public/orders/{token}/check-payment")
def public_check_payment(token: str):
    payment = by_token(token).to_dict().get("payment")
    return customer_view(reconcile_session(payment["session_id"]) if payment else by_token(token).to_dict())


def valid_signature(header: str, raw: bytes, secret: str) -> bool:
    """Paymongo-Signature is 't=<timestamp>,te=<test sig>,li=<live sig>'; the signature is HMAC-SHA256 of '<t>.<raw body>'."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    expected = hmac.new(secret.encode(), f"{parts.get('t', '')}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, parts.get(k, "")) for k in ("te", "li"))


@app.post("/api/webhooks/paymongo")
async def paymongo_webhook(request: Request):
    raw = await request.body()
    if not PAYMONGO_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook is not configured")
    if not valid_signature(request.headers.get("Paymongo-Signature", ""), raw, PAYMONGO_WEBHOOK_SECRET):
        raise HTTPException(401, "Bad signature")
    try:
        event = json.loads(raw)["data"]["attributes"]
        session_id = event["data"]["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Unrecognised event")
    if event.get("type") == "checkout_session.payment.paid":
        # The event only tells us where to look; the session we fetch is what counts. Duplicates are harmless.
        await run_in_threadpool(reconcile_session, session_id)
    return {"received": True}


@app.post("/api/webhooks/lalamove")
async def lalamove_webhook(request: Request):
    """Capture only: stores the raw event so we can see what the sandbox sends. Nothing acts on these yet.

    Always answers 200, because Lalamove pings the URL when it is registered.
    """
    # ponytail: filtered by apiKey only; verify the signature before any order state depends on these events
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        event = None
    if LALAMOVE_KEY and isinstance(event, dict) and event.get("apiKey") == LALAMOVE_KEY and len(raw) < 65536:
        doc = {"provider": "lalamove", "raw": raw.decode(), "path": request.url.path, "received_at": firestore.SERVER_TIMESTAMP}
        ref = clients()[1].collection("webhookEvents").document(f"lalamove_{event.get('eventId') or secrets.token_hex(8)}")
        try:
            await run_in_threadpool(ref.create, doc)
        except Exception:
            pass  # duplicate delivery of an event we already have
    return {"received": True}
