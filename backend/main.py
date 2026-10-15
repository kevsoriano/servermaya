import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pypdf
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google import genai
from google.api_core.exceptions import AlreadyExists, PreconditionFailed
from google.cloud import firestore, storage
from google.genai import types
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from pydantic import BaseModel, Field

import extraction
import signals as risk
import orders as rules
import packing as pack
import receipts as gcash

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
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "servermaya-ai-builder-cup-evidence")
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


def graph(path: str, send: dict | None = None, files: dict | None = None, **params) -> dict:
    """GET from the Graph API as the Page, or POST `send` as JSON. With `files`, POST `send` as a multipart form instead, objects
    JSON-encoded, as the Send API wants for uploads. Token goes in a header so it never lands in a URL or log."""
    if not PAGE_ID or not PAGE_TOKEN:
        raise HTTPException(503, "Messenger is not configured")
    form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in (send or {}).items()}
    body = {"data": form, "files": files} if files else {"json": send}
    try:
        r = requests.request(
            "POST" if send else "GET", f"{GRAPH}/{path}", params=params, headers={"Authorization": f"Bearer {PAGE_TOKEN}"}, timeout=30 if files else 10, **body
        )
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach Messenger")
    if not r.ok:
        raise HTTPException(502, body.get("error", {}).get("message", "Messenger request failed"))
    return body


_pics: dict[str, tuple[float, str | None]] = {}
PIC_TTL = 3600


# ponytail: in-process cache, so each Cloud Run instance fetches its own; fine at one store
def profile_pics(psids: list[str]) -> dict[str, str | None]:
    """Profile photo links by customer PSID, in one Graph call. Meta's links expire, so each is refetched after an hour.
    On any failure the photo is just missing and the app shows initials."""
    now = time.time()
    stale = [p for p in psids if p not in _pics or now - _pics[p][0] > PIC_TTL]
    if stale:
        try:
            data = graph("", ids=",".join(stale), fields="profile_pic")
            for p in stale:
                _pics[p] = (now, (data.get(p) or {}).get("profile_pic"))
        except HTTPException:
            pass
    return {p: _pics[p][1] if p in _pics else None for p in psids}


# ponytail: first page only (25 threads, 50 messages), add paging when a Page outgrows it
@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user)):
    data = graph(f"{PAGE_ID}/conversations", platform="messenger", fields="id,updated_time,snippet,participants,messages.limit(1){from,created_time}")
    threads = data.get("data", [])
    seen, actions = read_marks(user), order_actions([c["id"] for c in threads])
    customers = {c["id"]: next((p for p in c.get("participants", {}).get("data", []) if p.get("id") != PAGE_ID), None) for c in threads}
    pics = profile_pics([p["id"] for p in customers.values() if p and p.get("id")])
    out = []
    for c in threads:
        customer = customers[c["id"]]
        last = next(iter(c.get("messages", {}).get("data", [])), {})
        # The newest message is the customer's, so the seller owes a reply.
        waiting = bool(last) and last.get("from", {}).get("id") != PAGE_ID
        at = datetime.fromisoformat(last["created_time"]) if waiting and last.get("created_time") else None
        out.append({
            "id": c["id"],
            "updated_time": c.get("updated_time"),
            "snippet": c.get("snippet", ""),
            "customer": customer and {**customer, "picture": pics.get(customer.get("id"))},
            "open_checks": cached_open_checks(c["id"]),
            "unread": bool(at) and (c["id"] not in seen or seen[c["id"]] < at),
            "awaiting_reply": waiting,
            "reply_until": (at + REPLY_WINDOW).isoformat() if at else None,
            "order_actions": actions.get(c["id"], []),
        })
    return out


def read_docs():
    return clients()[1].collection("stores").document(STORE).collection("reads")


def read_marks(user: dict) -> dict:
    """When this seller last opened each thread. Read state is per seller, so one opening a thread does not clear it for the rest."""
    try:
        doc = read_docs().document(user["uid"]).get()
    except Exception:
        return {}  # the list still works without Firestore; every waiting thread just shows as unread
    return (doc.to_dict() or {}).get("seen", {}) if doc.exists else {}


@app.post("/api/conversations/{conversation_id}/seen")
def mark_seen(conversation_id: str, user: dict = Depends(current_user)):
    """Recorded in our database only. Nothing goes to Meta, so the customer never gets a "Seen" receipt."""
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    read_docs().document(user["uid"]).set({"seen": {conversation_id: datetime.now(timezone.utc)}}, merge=True)
    return {"ok": True}


ORDER_ACTIONS = {"draft": "Send the draft order", "awaiting_confirmation": "Waiting for the customer to confirm"}
PAYMENT_ACTIONS = {None: "Request payment", "pending": "Waiting for payment", "mismatched": "Review a payment"}
GCASH_ACTIONS = {"requested": "Waiting for GCash payment", "claimed": "Review a GCash receipt", "seller_confirmed": None}


def order_action(order: dict) -> str | None:
    if order["status"] == "confirmed":
        manual = order.get("manual_payment") or {}
        if manual and manual["version"] == order.get("version") and (order.get("payment") or {}).get("status") != "verified":
            return GCASH_ACTIONS[manual["status"]]
        return PAYMENT_ACTIONS.get((order.get("payment") or {}).get("status"))
    return ORDER_ACTIONS.get(order["status"])


def order_actions(conversation_ids: list[str]) -> dict[str, list[str]]:
    """What is still open on each thread's orders. Cancelled and paid orders need nothing."""
    found: dict[str, list[str]] = {}
    try:
        for i in range(0, len(conversation_ids), 30):  # Firestore caps an "in" filter at 30 values
            batch = conversation_ids[i : i + 30]
            for d in orders().where(filter=firestore.FieldFilter("source.conversation_id", "in", batch)).stream():
                o = d.to_dict()
                action = order_action(o)
                cid = o["source"]["conversation_id"]
                if action and action not in found.setdefault(cid, []):
                    found[cid].append(action)
    except Exception:
        return {}
    return found


def cached_open_checks(conversation_id: str) -> int | None:
    """Count from the last analysis only, so listing never calls Gemini. None means not analysed yet."""
    try:
        doc = signal_docs().document(conversation_id).get()
    except Exception:
        return None  # the list still works without Firestore
    if not doc.exists:
        return None
    d = doc.to_dict()
    paid, confirmed = payments(conversation_id)
    return sum(1 for s in risk.resolve(d["signals"], paid, d.get("reviews"), confirmed) if risk.is_open(s))


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)):
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    return fetch_messages(conversation_id)


def attachments(message: dict) -> list[dict]:
    """Normalise Messenger attachments to {kind, mime_type, name, url, preview_url}. Anything without an https link is dropped."""
    out = []
    for a in message.get("attachments", {}).get("data", []):
        mime = a.get("mime_type") or ""
        media = a.get("image_data") or a.get("video_data") or {}
        kind = mime.split("/")[0] if mime.split("/")[0] in ("image", "video", "audio") else "file"
        out.append({"kind": kind, "mime_type": mime, "name": a.get("name"), "url": media.get("url") or a.get("file_url"), "preview_url": media.get("preview_url")})
    if message.get("sticker"):
        out.append({"kind": "image", "mime_type": "", "name": "sticker", "url": message["sticker"], "preview_url": None})
    # ponytail: these are Meta's temporary links; copy the files into our own storage (with hashes) when evidence retention lands
    return [a for a in out if str(a["url"]).startswith("https://")]


def fetch_messages(conversation_id: str) -> list[dict]:
    data = graph(f"{conversation_id}/messages", fields="id,message,from,created_time,sticker,attachments{id,mime_type,name,size,file_url,image_data,video_data}",
        limit=50,
    )
    return [
        {
            "id": m["id"],
            "text": m.get("message", ""),
            "from_page": m.get("from", {}).get("id") == PAGE_ID,
            "from_name": m.get("from", {}).get("name"),
            "created_time": m.get("created_time"),
            "attachments": attachments(m),
        }
        for m in reversed(data.get("data", []))  # Graph returns newest first
    ]


def signal_docs():
    return clients()[1].collection("stores").document(STORE).collection("signals")


def payments(conversation_id: str) -> tuple[list[dict], list[dict]]:
    """PayMongo-verified payments and seller-confirmed direct GCash payments on orders from this chat."""
    found = [d.to_dict() for d in orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream()]
    paid = [p for p in (o.get("payment") or {} for o in found) if p.get("status") == "verified"]
    confirmed = [m for m in (o.get("manual_payment") or {} for o in found) if m.get("status") == "seller_confirmed"]
    return paid, confirmed


@app.get("/api/conversations/{conversation_id}/signals")
def conversation_signals(conversation_id: str, user: dict = Depends(current_user)):
    """Checks before shipping for one thread. Gemini only reruns when a new message has arrived since the last analysis."""
    messages = conversation(conversation_id, user)
    latest = messages[-1]["id"] if messages else None
    ref = signal_docs().document(conversation_id)
    cached = ref.get().to_dict() or {}
    if cached.get("latest_message_id") == latest and "signals" in cached:
        found = cached["signals"]
    else:
        try:
            found = risk.detect(clients()[0], GEMINI_MODEL, messages) if messages else []
        except Exception:
            raise HTTPException(502, "Could not analyse this conversation")
        # merge keeps the seller's reviews across re-analysis
        ref.set({"signals": found, "latest_message_id": latest, "model": GEMINI_MODEL, "analysed_at": firestore.SERVER_TIMESTAMP}, merge=True)
    paid, confirmed = payments(conversation_id)
    return risk.resolve(found, paid, cached.get("reviews"), confirmed)


class Review(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=500)
    reopen: bool = False


@app.post("/api/conversations/{conversation_id}/signals/review")
def review_signal(conversation_id: str, body: Review, user: dict = Depends(current_user)):
    """Seller marks a check as reviewed, with a reason, or reopens it. History of who and why stays on the record."""
    ref = signal_docs().document(conversation_id)
    if body.key not in {risk.key(s) for s in (ref.get().to_dict() or {}).get("signals", [])}:
        raise HTTPException(404, "That check is no longer in this conversation")
    if body.reopen:
        ref.set({"reviews": {body.key: firestore.DELETE_FIELD}}, merge=True)
    else:
        if not body.note.strip():
            raise HTTPException(422, "Say why this check is fine")
        now = datetime.now(timezone.utc)
        ref.set({"reviews": {body.key: {"by": user["email"], "at": now, "note": body.note.strip()}}}, merge=True)
    return conversation_signals(conversation_id, user)


REPLY_WINDOW = timedelta(hours=24)


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)  # Messenger's limit for one text message
    order_id: str | None = Field(default=None, max_length=64)


def reply_until(messages: list[dict]) -> datetime | None:
    """Messenger only allows a normal reply within 24 hours of the customer's latest message."""
    times = [datetime.fromisoformat(m["created_time"]) for m in messages if not m["from_page"]]
    return max(times) + REPLY_WINDOW if times else None


def customer_to_reply(conversation_id: str, user: dict) -> str:
    """The customer's PSID, if the 24-hour reply window is still open."""
    until = reply_until(conversation(conversation_id, user))
    if not until or datetime.now(timezone.utc) > until:
        raise HTTPException(409, "Messenger only allows replies within 24 hours of the customer's last message.")
    participants = graph(conversation_id, fields="participants").get("participants", {}).get("data", [])
    customer = next((p["id"] for p in participants if p.get("id") != PAGE_ID), None)
    if not customer:
        raise HTTPException(409, "Could not find the customer in this conversation")
    return customer


def record_sent(conversation_id: str, order_id: str | None, sent: dict, user: dict, **what) -> dict:
    # Meta shows the reply as coming from the Page; this records which seller sent it.
    record = {"conversation_id": conversation_id, "order_id": order_id, **what, "message_id": sent.get("message_id"), "sent_by": user["email"],
              "sent_at": firestore.SERVER_TIMESTAMP}
    clients()[1].collection("stores").document(STORE).collection("sentMessages").add(record)
    return {"message_id": sent.get("message_id")}


@app.post("/api/conversations/{conversation_id}/reply")
def reply(conversation_id: str, body: Reply, user: dict = Depends(current_user)):
    """Send one text message as the Page. Only ever called from an explicit seller click."""
    customer = customer_to_reply(conversation_id, user)
    sent = graph(f"{PAGE_ID}/messages", send={"recipient": {"id": customer}, "messaging_type": "RESPONSE", "message": {"text": body.text}})
    return record_sent(conversation_id, body.order_id, sent, user, text=body.text)


REPLY_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}  # what Messenger shows as an image


@app.post("/api/conversations/{conversation_id}/reply/image")
async def reply_image(conversation_id: str, request: Request, order_id: str | None = None, user: dict = Depends(current_user)):
    """Send one image as the Page, such as the seller's GCash QR. The body is the image itself. Kept as evidence of what was sent."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in REPLY_IMAGE_TYPES:
        raise HTTPException(415, "Send a JPEG, PNG or GIF image")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Images must be under 10 MB")
    if order_id and not re.fullmatch(r"\w{1,64}", order_id):
        raise HTTPException(400, "Invalid order id")
    customer = await run_in_threadpool(customer_to_reply, conversation_id, user)
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{STORE}/sent/{sha}"
    try:
        await run_in_threadpool(lambda: bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    except PreconditionFailed:
        pass  # the same image was sent before
    sent = await run_in_threadpool(
        graph,
        f"{PAGE_ID}/messages",
        {"recipient": {"id": customer}, "messaging_type": "RESPONSE", "message": {"attachment": {"type": "image", "payload": {"is_reusable": False}}}},
        {"filedata": (f"image.{content_type.split('/')[1]}", data, content_type)},
    )
    image = {"path": path, "sha256": sha, "content_type": content_type, "size": len(data)}
    return await run_in_threadpool(record_sent, conversation_id, order_id, sent, user, image=image)


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
    try:
        packing, blocked = pack.require_ready(order), None  # the checklist to show, even before the first photo
    except rules.Refused as e:
        packing, blocked = order.get("packing"), str(e)
    return {**order, "id": snap.id, "missing": rules.missing(order["draft"]), "packing": packing, "packing_blocked": blocked}


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
        "gcash_confirmed": pack.paid_by(order) == "gcash_seller_confirmed",
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


# Packing. Photos are evidence: stored once, never overwritten, with a SHA-256 of the original bytes.
@lru_cache
def bucket():
    return storage.Client(project=PROJECT_ID).bucket(EVIDENCE_BUCKET)


def loaded(order_id: str) -> tuple:
    snap = orders().document(order_id).get()
    if not snap.exists:
        raise HTTPException(404, "Order not found")
    return snap.reference, snap.to_dict()


def ready(order: dict) -> dict:
    try:
        return pack.require_ready(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))


@app.post("/api/orders/{order_id}/packing/photos")
async def upload_packing_photo(order_id: str, request: Request, user: dict = Depends(current_user)):
    """The body is the image itself. X-Claimed-Captured-At is what the device says; it proves nothing and is stored apart from upload time."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photos must be under 10 MB")
    ref, order = await run_in_threadpool(loaded, order_id)
    ready(order)
    photo_id = secrets.token_hex(8)
    path = f"stores/{STORE}/orders/{order_id}/packing/{photo_id}"
    # if_generation_match=0: refuse to overwrite an existing object
    await run_in_threadpool(lambda: bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    now = datetime.now(timezone.utc)
    photo = {
        "id": photo_id,
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": content_type,
        "size": len(data),
        "uploaded_at": now,
        "uploaded_by": user["email"],
        "claimed_captured_at": request.headers.get("x-claimed-captured-at", "")[:40] or None,
    }
    return await run_in_threadpool(change, ref, lambda o: pack.add_photo(o, photo))


@app.get("/api/orders/{order_id}/packing/photos/{photo_id}")
def packing_photo(order_id: str, photo_id: str, user: dict = Depends(current_user)):
    _, order = loaded(order_id)
    sessions = [order.get("packing") or {}] + order.get("packing_history", [])
    photo = next((p for s in sessions for p in s.get("photos", []) if p["id"] == photo_id), None)
    if not photo:
        raise HTTPException(404, "Photo not found")
    return Response(bucket().blob(photo["path"]).download_as_bytes(), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/orders/{order_id}/packing/check")
def check_packing(order_id: str, user: dict = Depends(current_user)):
    ref, order = loaded(order_id)
    packing = ready(order)
    if not packing["photos"]:
        raise HTTPException(409, "Add at least one packing photo")
    checklist = "\n".join(
        f"[{r['line']}] {r['expected']['quantity']} x {r['expected']['name']}" + (f" ({r['expected']['variant']})" if r["expected"]["variant"] else "")
        for r in packing["rows"]
    )
    contents = [f"<order>\n{checklist}\n</order>"]
    for n, p in enumerate(packing["photos"], 1):
        contents += [f"Photo {n}:", types.Part.from_bytes(data=bucket().blob(p["path"]).download_as_bytes(), mime_type=p["content_type"])]
    try:
        response = clients()[0].models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config={"system_instruction": pack.SYSTEM, "response_mime_type": "application/json", "response_schema": pack.Report, "temperature": 0},
        )
        report = pack.Report.model_validate_json(response.text)
    except Exception:
        raise HTTPException(502, "Could not check the photos. Try again.")
    now = datetime.now(timezone.utc)
    return change(ref, lambda o: pack.apply_check(o, report, GEMINI_MODEL, now))


class Inspection(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@app.post("/api/orders/{order_id}/packing/lines/{line}/confirm")
def confirm_packing_line(order_id: str, line: int, body: Inspection, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: pack.confirm_row(o, line, user["email"], body.reason, now))


@app.post("/api/orders/{order_id}/packing/complete")
def complete_packing(order_id: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: pack.complete(o, user["email"], now))


# Direct GCash. Screenshots are claims: code checks them, and only the seller, looking at their own GCash history, confirms.
def store_doc():
    return clients()[1].collection("stores").document(STORE)


@app.get("/api/settings/gcash")
def get_gcash(user: dict = Depends(current_user)):
    return (store_doc().get().to_dict() or {}).get("gcash")


@app.put("/api/settings/gcash")
def put_gcash(body: gcash.Settings, user: dict = Depends(current_user)):
    try:
        found = gcash.settings(body)
    except rules.Refused as e:
        raise HTTPException(422, str(e))
    saved = {**found, "updated_by": user["email"], "updated_at": datetime.now(timezone.utc)}
    store_doc().set({"gcash": saved}, merge=True)
    return saved


@app.post("/api/orders/{order_id}/gcash/request")
def request_gcash(order_id: str, user: dict = Depends(current_user)):
    pay_to = get_gcash(user)
    if not pay_to:
        raise HTTPException(409, "Add your GCash name and number in Settings first")
    now = datetime.now(timezone.utc)
    qr = pay_to.get("qr") or {}
    snapshot = {"name": pay_to["name"], "mobile": pay_to["mobile"], "masked_name": qr.get("masked_name"), "user_id": qr.get("user_id")}
    return change(orders().document(order_id), lambda o: gcash.request(o, snapshot, user["email"], now))


# ponytail: written before the claim is saved, so a failed read leaves an entry pointing at an order with no claim, and a
# legitimate reuse on another order is then flagged (fails safe: the seller gives a reason). Upgrade: when the entry
# points elsewhere, check that order really has the claim and take the entry over if not.
def first_seen(collection: str, key: str, order_id: str, **extra) -> dict:
    """Index a reference number, image hash or receipt details across orders. Returns whichever record got there first."""
    doc = store_doc().collection(collection).document(key)
    record = {"order_id": order_id, **extra}
    try:
        doc.create({**record, "at": firestore.SERVER_TIMESTAMP})
        return record
    except AlreadyExists:
        return doc.get().to_dict()


MAX_RECEIPTS_PER_READ = 5


def transcribe(data: bytes, content_type: str, system: str, schema: type[BaseModel]) -> BaseModel:
    try:
        response = clients()[0].models.generate_content(
            model=GEMINI_MODEL,
            contents=["Screenshot:", types.Part.from_bytes(data=data, mime_type=content_type)],
            config={"system_instruction": system, "response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
        )
        return schema.model_validate_json(response.text)
    except Exception:
        raise HTTPException(502, "Could not read a screenshot. Try again.")


def read_receipt(order_id: str, manual: dict, message: dict, attachment: dict, now: datetime) -> dict:
    """Copy one screenshot into our storage as evidence, have Gemini transcribe it, and run the checks."""
    try:
        r = requests.get(attachment["url"], timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        raise HTTPException(502, "Could not download a screenshot from Messenger. Try again.")
    data, content_type = r.content, r.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in pack.IMAGE_TYPES or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(415, "A screenshot is not a supported image")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{STORE}/orders/{order_id}/gcash/{sha}"
    try:
        bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same file is already stored
    receipt = transcribe(data, content_type, gcash.SYSTEM, gcash.Receipt)
    ref = gcash.reference(receipt.reference)
    # Copies are caught three ways: the same file, the same reference, or the same amount and minute under another reference (an edited copy).
    reused = first_seen("gcashImages", sha, order_id)["order_id"] != order_id
    ref_key = gcash.reference_key(ref, receipt)
    used_elsewhere = {ref} if ref_key and first_seen("gcashReferences", ref_key, order_id)["order_id"] != order_id else set()
    key = gcash.details_key(receipt)
    twin = first_seen("gcashReceiptDetails", key, order_id, reference=ref, message_id=message["id"]) if key else None
    twin_reference = (twin["reference"] or "unreadable") if twin and twin["message_id"] != message["id"] and twin["reference"] != ref else None
    received_at = datetime.fromisoformat(message["created_time"])
    checks = gcash.check_receipt(receipt, manual["amount"], manual["requested_at"], received_at, manual["pay_to"], used_elsewhere, reused, twin_reference)
    return {
        "id": sha[:16],
        "message_id": message["id"],
        "received_at": received_at,
        "image": {"path": path, "sha256": sha, "content_type": content_type, "size": len(data)},
        "receipt": receipt.model_dump(),
        "reference": ref or None,
        "checks": checks,
        "read_at": now,
        "model": GEMINI_MODEL,
    }


@app.post("/api/orders/{order_id}/gcash/read")
def read_gcash_receipts(order_id: str, user: dict = Depends(current_user)):
    """Check every customer image sent since GCash payment was requested that has not been checked yet."""
    ref, order = loaded(order_id)
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if order.get("source", {}).get("type") != "messenger":
        raise HTTPException(409, "This order did not come from Messenger")
    known = {c["message_id"] for c in manual["claims"]}
    images = [
        (m, a)
        for m in fetch_messages(order["source"]["conversation_id"])
        if not m["from_page"] and m["id"] not in known and datetime.fromisoformat(m["created_time"]) >= manual["requested_at"] - gcash.SLACK
        for a in m["attachments"][:1]
        if a["kind"] == "image" and a["name"] != "sticker"
    ][-MAX_RECEIPTS_PER_READ:]
    if not images:
        raise HTTPException(409, "No new images from the customer since you asked for GCash payment")
    now = datetime.now(timezone.utc)
    claims = [read_receipt(order_id, manual, m, a, now) for m, a in images]
    return change(ref, lambda o: gcash.add_claims(o, claims))


@app.get("/api/orders/{order_id}/gcash/claims/{claim_id}/image")
def gcash_screenshot(order_id: str, claim_id: str, user: dict = Depends(current_user)):
    """The stored copy of a receipt screenshot, so the seller can compare it with the checks by eye."""
    _, order = loaded(order_id)
    requests_ = [order.get("manual_payment") or {}] + order.get("manual_payment_history", [])
    claim = next((c for r in requests_ for c in r.get("claims", []) if c["id"] == claim_id), None)
    if not claim:
        raise HTTPException(404, "Screenshot not found")
    image = claim["image"]
    return Response(bucket().blob(image["path"]).download_as_bytes(), media_type=image["content_type"], headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/orders/{order_id}/gcash/history")
async def read_gcash_history(order_id: str, request: Request, user: dict = Depends(current_user)):
    """The seller's screenshot of the payment in their own GCash history. Read to fill in the reference; it confirms nothing."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC screenshot")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Screenshots must be under 10 MB")
    ref, order = await run_in_threadpool(loaded, order_id)
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{STORE}/orders/{order_id}/gcash/history/{sha}"
    try:
        await run_in_threadpool(lambda: bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    except PreconditionFailed:
        pass  # the same file is already stored
    entry = await run_in_threadpool(transcribe, data, content_type, gcash.HISTORY_SYSTEM, gcash.HistoryEntry)
    read = {
        "image": {"path": path, "sha256": sha, "content_type": content_type, "size": len(data)},
        "entry": entry.model_dump(),
        "reference": gcash.reference(entry.reference) or None,
        "checks": gcash.check_history(entry, manual["amount"], manual["requested_at"]),
        "read_at": datetime.now(timezone.utc),
        "read_by": user["email"],
        "model": GEMINI_MODEL,
    }
    return await run_in_threadpool(change, ref, lambda o: gcash.add_history(o, read))


MAX_STATEMENT_BYTES = 5 * 1024 * 1024


def statement_text(data: bytes, password: str) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password):
            raise HTTPException(422, "Wrong password. GCash uses your last name and the last 4 digits of your mobile number, like delacruz1234.")
        return "\n".join(page.extract_text() for page in reader.pages)
    except pypdf.errors.PyPdfError:
        raise HTTPException(422, "Could not open this PDF")


@app.post("/api/gcash/statements")
async def upload_gcash_statement(request: Request, x_pdf_password: str = Header(default=""), user: dict = Depends(current_user)):
    """The seller's GCash transaction history export. Confirms waiting GCash payments it shows, and checks confirmed ones.

    The password is used once and never stored. Only received rows are kept; the PDF itself is not.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/pdf":
        raise HTTPException(415, "Upload the PDF that GCash emailed you")
    data = await request.body()
    if not data or len(data) > MAX_STATEMENT_BYTES:
        raise HTTPException(413, "The PDF must be under 5 MB")
    text = await run_in_threadpool(statement_text, data, x_pdf_password)
    try:
        statement = gcash.parse_statement(text)
    except rules.Refused as e:
        raise HTTPException(422, str(e))
    return await run_in_threadpool(apply_statement, statement, hashlib.sha256(data).hexdigest(), user["email"])


def apply_statement(statement: dict, statement_id: str, by: str) -> dict:
    now = datetime.now(timezone.utc)
    store_doc().collection("gcashStatements").document(statement_id).set({**statement, "uploaded_by": by, "uploaded_at": now})
    waiting = list(orders().where(filter=firestore.FieldFilter("manual_payment.status", "in", ["requested", "claimed", "seller_confirmed"])).stream())
    taken = {m["confirmed"]["reference"]: d.id for d in waiting if (m := d.to_dict()["manual_payment"]).get("confirmed")}
    result = {"start": statement["start"], "last_at": statement["last_at"], "received": len(statement["credits"])}
    for d in waiting:
        others = {ref for ref, order_id in taken.items() if order_id != d.id}
        # check outside the transaction first, so unchanged orders cost no write
        if not gcash.reconcile(d.to_dict(), statement, statement_id, others, by, now):
            continue
        saved = change(d.reference, lambda o: gcash.reconcile(o, statement, statement_id, others, by, now))
        m = saved["manual_payment"]
        status = "confirmed" if m["confirmed"].get("statement") == statement_id else m["history_check"]["status"]
        result.setdefault(status, []).append(d.id)
        taken[m["confirmed"]["reference"]] = d.id
    return result


class GcashConfirm(BaseModel):
    reference: str = Field(min_length=1, max_length=40)
    reason: str = Field(default="", max_length=500)
    found_in_history: bool
    invno: str = Field(default="", max_length=80)


@app.post("/api/orders/{order_id}/gcash/confirm")
def confirm_gcash(order_id: str, body: GcashConfirm, user: dict = Depends(current_user)):
    if not body.found_in_history:
        raise HTTPException(422, "Only confirm a reference you found in your own GCash transaction history")
    ref = gcash.reference(body.reference)
    others = orders().where(filter=firestore.FieldFilter("manual_payment.confirmed.reference", "==", ref)).limit(2).stream()
    elsewhere = any(d.id != order_id for d in others)
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: gcash.confirm(o, ref, user["email"], body.reason, now, elsewhere, body.invno))
