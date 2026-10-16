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
from typing import Literal

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
import claims as cases
import delivery
import demo
import samples
import patterns
import refunds

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
LALAMOVE_SECRET = os.environ.get("LALAMOVE_API_SECRET", "")
LALAMOVE_URL = os.environ.get("LALAMOVE_URL", "https://rest.sandbox.lalamove.com")
EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "servermaya-ai-builder-cup-evidence")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://servermaya-ai-builder-cup.web.app")

app = FastAPI()
_http = google_requests.Request()


def current_user(request: Request, authorization: str = Header(default="")) -> dict:
    """Verify the Firebase ID token and the email allowlist. Every protected route depends on this.

    Anonymous Firebase sign-ins are demo visitors: they pass without the allowlist, but only reach their own demo chats and
    the orders made from them (see demo_gate)."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = id_token.verify_firebase_token(token, _http, audience=PROJECT_ID)
    except Exception:
        raise HTTPException(401, "Invalid token")
    if claims.get("iss") != f"https://securetoken.google.com/{PROJECT_ID}":
        raise HTTPException(401, "Invalid token")
    if (claims.get("firebase") or {}).get("sign_in_provider") == "anonymous":
        user = {"uid": claims["sub"], "email": "demo visitor", "name": "Demo visitor", "demo": True}
        demo_gate(request, user)
        return user
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in ALLOWLIST:
        raise HTTPException(403, "This account is not allowed")
    return {"uid": claims["sub"], "email": email, "name": claims.get("name"), "demo": False}


# Routes a demo visitor may call that name no chat or order. Routes with a chat or order in the path are allowed only for
# the visitor's own; every other route is refused, so a new route stays closed to demo visitors until it is listed here.
DEMO_ROUTES = {
    ("GET", "/api/me"), ("GET", "/api/conversations"), ("POST", "/api/demo/threads"), ("POST", "/api/demo/reset"),
    ("GET", "/api/orders"), ("GET", "/api/settings/gcash"), ("GET", "/api/settings/dispatch"), ("GET", "/api/patterns"),
    ("POST", "/api/gcash/statements"), ("GET", "/api/demo/sample-statement"),
}


def demo_gate(request: Request, user: dict):
    params = request.path_params
    if "conversation_id" in params:
        cid = params["conversation_id"]
        if not demo.is_demo(cid) or demo_doc(cid).get("owner") != user["uid"]:
            raise HTTPException(404, "Demo chat not found")
    if "order_id" in params:
        snap = orders().document(params["order_id"]).get()
        if not snap.exists or not visible(snap.to_dict(), user):
            raise HTTPException(404, "Order not found")
    if not params.keys() & {"conversation_id", "order_id"} and (request.method, getattr(request.scope.get("route"), "path", "")) not in DEMO_ROUTES:
        raise HTTPException(403, "Not available in the demo")


def visible(order: dict, user: dict) -> bool:
    """Demo orders belong to whoever made their chat; everything else is the store's, closed to demo visitors."""
    source = order.get("source") or {}
    return source.get("owner") == user["uid"] if source.get("type") == "demo" else not user.get("demo")


# Fictional, so a demo visitor never sees the seller's real GCash account or pickup address. Demo orders always use these.
DEMO_GCASH = {"name": "Juan Dela Cruz", "mobile": "639171234567", "qr": None, "demo": True}
DEMO_PICKUP = {"address": "6750 Ayala Ave., Makati City", "lat": 14.5547, "lng": 121.0244, "name": "Demo shop", "phone": "+639171234567", "demo": True}


def demo_order(order_id: str) -> bool:
    return ((orders().document(order_id).get().to_dict() or {}).get("source") or {}).get("type") == "demo"


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
    if user.get("demo"):
        return demo_list(user)
    try:
        data = graph(f"{PAGE_ID}/conversations", platform="messenger", fields="id,updated_time,snippet,participants,messages.limit(1){from,created_time}")
    except HTTPException as e:
        print(f"Messenger thread list failed: {e.detail}")  # demo chats still load, so the app stays usable
        data = {}
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
    return out + demo_list(user)


def demo_threads():
    return store_doc().collection("demoThreads")


def seed_demo(user: dict):
    batch = clients()[1].batch()
    for t in demo.seed(user["uid"], datetime.now(timezone.utc)):
        batch.set(demo_threads().document(demo.new_id()), t)
    batch.commit()


def demo_list(user: dict) -> list[dict]:
    """This seller's demo chats, seeded the first time they open the inbox. Never read state or reply windows: nobody is waiting."""
    try:
        store_doc().collection("demoUsers").document(user["uid"]).create({"seeded_at": firestore.SERVER_TIMESTAMP})
        seed_demo(user)
    except AlreadyExists:
        pass
    docs = sorted(demo_threads().where(filter=firestore.FieldFilter("owner", "==", user["uid"])).stream(),
                  key=lambda d: d.get("updated_time"), reverse=True)
    actions = order_actions([d.id for d in docs])
    return [{"id": d.id, "updated_time": d.get("updated_time"), "snippet": d.get("snippet"), "customer": {"id": d.id, "name": d.get("name"), "picture": None},
             "open_checks": cached_open_checks(d.id), "unread": False, "awaiting_reply": False, "reply_until": None,
             "order_actions": actions.get(d.id, []), "demo": True} for d in docs]


def demo_doc(conversation_id: str) -> dict:
    snap = demo_threads().document(conversation_id).get()
    if not snap.exists:
        raise HTTPException(404, "Demo chat not found")
    return snap.to_dict()


def demo_post(conversation_id: str, from_page: bool, text: str, attachment: dict | None = None) -> dict:
    """Add a message to a demo chat. Nothing goes to Messenger."""
    d = demo_doc(conversation_id)
    if len(d["messages"]) >= demo.MAX_MESSAGES:
        raise HTTPException(409, "This demo chat is full. Start a new one.")
    m = demo.message(from_page, d["name"], text, datetime.now(timezone.utc), [attachment] if attachment else None)
    demo_threads().document(conversation_id).update({"messages": firestore.ArrayUnion([m]), "updated_time": m["created_time"],
                                                     "snippet": text or "Sent a photo"})
    return {"message_id": m["id"]}


DEMO_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


async def demo_image(conversation_id: str, request: Request, from_page: bool) -> dict:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in DEMO_IMAGE_TYPES:
        raise HTTPException(415, "Send a JPEG, PNG, GIF or WebP image")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Images must be under 10 MB")
    attachment = await run_in_threadpool(demo_attachment, data, content_type)
    return await run_in_threadpool(demo_post, conversation_id, from_page, "", attachment)


def demo_attachment(data: bytes, content_type: str) -> dict:
    """Store an image for a demo chat and describe it like a Messenger attachment."""
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{STORE}/demo/{sha}"
    try:
        bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same image was sent before
    return {"kind": "image", "mime_type": content_type, "name": None, "url": f"/api/public/demo/images/{sha}", "preview_url": None, "path": path}


# ponytail: public by hash so a plain <img> can show it; demo images only. Fetch with the seller's token if real photos ever land here.
@app.get("/api/public/demo/images/{sha}")
def demo_image_file(sha: str):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(404, "Image not found")
    blob = bucket().get_blob(f"stores/{STORE}/demo/{sha}")
    if not blob:
        raise HTTPException(404, "Image not found")
    return Response(blob.download_as_bytes(), media_type=blob.content_type, headers={"Cache-Control": "private, max-age=86400"})


class DemoChat(BaseModel):
    name: str = Field(default="Demo customer", min_length=1, max_length=100)
    chat: str = Field(default="", max_length=50_000)


@app.post("/api/demo/threads")
def new_demo_chat(body: DemoChat, user: dict = Depends(current_user)):
    """A demo chat from pasted text, or an empty one to type into."""
    try:
        lines = demo.parse(body.chat)
    except ValueError as e:
        raise HTTPException(422, str(e))
    cid = demo.new_id()
    demo_threads().document(cid).set(demo.thread(body.name.strip(), lines, user["uid"], datetime.now(timezone.utc)))
    return {"id": cid}


@app.post("/api/demo/reset")
def reset_demo(user: dict = Depends(current_user)):
    """Delete this seller's demo chats and every order made from them, then seed fresh ones. Real Messenger orders are never touched."""
    ids = [d.id for d in demo_threads().where(filter=firestore.FieldFilter("owner", "==", user["uid"])).stream()]
    db, order_ids = clients()[1], []
    for i in range(0, len(ids), 30):  # Firestore caps an "in" filter at 30 values
        for o in orders().where(filter=firestore.FieldFilter("source.conversation_id", "in", ids[i : i + 30])).stream():
            order_ids.append(o.id)
            db.recursive_delete(o.reference)
        for s in store_doc().collection("sentMessages").where(filter=firestore.FieldFilter("conversation_id", "in", ids[i : i + 30])).stream():
            s.reference.delete()
    # Otherwise a receipt reused after the reset would be flagged as already used on a deleted order.
    for name in ("gcashImages", "gcashReferences", "gcashReceiptDetails"):
        for i in range(0, len(order_ids), 30):
            for d in store_doc().collection(name).where(filter=firestore.FieldFilter("order_id", "in", order_ids[i : i + 30])).stream():
                d.reference.delete()
    for cid in ids:
        signal_docs().document(cid).delete()
        demo_threads().document(cid).delete()
    store_doc().collection("demoUsers").document(user["uid"]).set({"samples": []}, merge=True)
    seed_demo(user)
    return {"deleted_chats": len(ids), "deleted_orders": len(order_ids)}


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
    if not (re.fullmatch(r"t_\d+", conversation_id) or demo.is_demo(conversation_id)):
        raise HTTPException(400, "Invalid conversation id")
    read_docs().document(user["uid"]).set({"seen": {conversation_id: datetime.now(timezone.utc)}}, merge=True)
    return {"ok": True}


ORDER_ACTIONS = {"draft": "Send the draft order", "awaiting_confirmation": "Waiting for the customer to confirm"}
PAYMENT_ACTIONS = {None: "Request payment", "pending": "Waiting for payment", "mismatched": "Review a payment"}
GCASH_ACTIONS = {"requested": "Waiting for GCash payment", "claimed": "Review a GCash receipt", "seller_confirmed": None}


def order_action(order: dict) -> str | None:
    if any(c["status"] != "resolved" for c in order.get("claims", [])):
        return "Review a customer claim"
    if order["status"] == "confirmed":
        try:
            delivery.can_dispatch(order)
            if not order.get("fulfillment"):
                return "Ready to dispatch"
        except rules.Refused:
            pass
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
    if demo.is_demo(conversation_id):
        return demo_doc(conversation_id)["messages"]
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    return fetch_messages(conversation_id)


def thread_messages(conversation_id: str) -> list[dict]:
    """Current messages of the chat an order came from, Messenger or demo."""
    return demo_doc(conversation_id)["messages"] if demo.is_demo(conversation_id) else fetch_messages(conversation_id)


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
    if demo.is_demo(conversation_id):
        return demo_post(conversation_id, True, body.text)
    customer = customer_to_reply(conversation_id, user)
    sent = graph(f"{PAGE_ID}/messages", send={"recipient": {"id": customer}, "messaging_type": "RESPONSE", "message": {"text": body.text}})
    return record_sent(conversation_id, body.order_id, sent, user, text=body.text)


REPLY_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}  # what Messenger shows as an image


@app.post("/api/conversations/{conversation_id}/reply/image")
async def reply_image(conversation_id: str, request: Request, order_id: str | None = None, user: dict = Depends(current_user)):
    """Send one image as the Page, such as the seller's GCash QR. The body is the image itself. Kept as evidence of what was sent."""
    if demo.is_demo(conversation_id):
        return await demo_image(conversation_id, request, True)
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


def demo_only(conversation_id: str):
    if not demo.is_demo(conversation_id):
        raise HTTPException(404, "Only demo chats take messages written as the customer")


@app.post("/api/conversations/{conversation_id}/customer")
def demo_customer(conversation_id: str, body: Reply, user: dict = Depends(current_user)):
    """In a demo chat, the judge also plays the customer."""
    demo_only(conversation_id)
    return demo_post(conversation_id, False, body.text)


@app.post("/api/conversations/{conversation_id}/customer/image")
async def demo_customer_image(conversation_id: str, request: Request, user: dict = Depends(current_user)):
    demo_only(conversation_id)
    return await demo_image(conversation_id, request, False)


class SampleReceipt(BaseModel):
    kind: Literal[samples.KINDS]


@app.post("/api/conversations/{conversation_id}/customer/sample-receipt")
def demo_sample_receipt(conversation_id: str, body: SampleReceipt, user: dict = Depends(current_user)):
    """A made-up GCash receipt for this chat's waiting GCash request, sent as the customer and dated now."""
    demo_only(conversation_id)
    waiting = []
    for d in orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream():
        try:
            m = gcash.current(d.to_dict())
        except rules.Refused:
            continue
        if m["status"] in ("requested", "claimed"):
            waiting.append(m)
    if not waiting:
        raise HTTPException(409, "Ask for GCash payment on this chat's order first.")
    m = max(waiting, key=lambda m: m["requested_at"])
    now, ref = datetime.now(timezone.utc), samples.new_reference()
    amount = m["amount"] - min(10000, m["amount"] // 2) if body.kind == "wrong_amount" else m["amount"]
    name, mobile = (samples.OTHER_NAME, samples.OTHER_MOBILE) if body.kind == "wrong_recipient" else (samples.PAY_TO_NAME, samples.PAY_TO_MOBILE)
    attachment = demo_attachment(samples.receipt_png(amount, ref, now, name, mobile), "image/png")
    if body.kind == "matching":  # only money that reached the demo account appears in the sample history
        sender = samples.sender(demo_doc(conversation_id)["name"])
        store_doc().collection("demoUsers").document(user["uid"]).set(
            {"samples": firestore.ArrayUnion([{"reference": ref, "amount": amount, "at": now, "sender": sender}])}, merge=True)
    return demo_post(conversation_id, False, "", attachment)


@app.get("/api/demo/sample-statement")
def demo_sample_statement(user: dict = Depends(current_user)):
    """A made-up GCash history export with every matching sample receipt this visitor sent, plus unrelated rows."""
    got = (store_doc().collection("demoUsers").document(user["uid"]).get().to_dict() or {}).get("samples", [])
    now = datetime.now(gcash.PHT)
    start = (min([s["at"] for s in got], default=now).astimezone(gcash.PHT) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    pdf = samples.statement_pdf(samples.statement_lines(got, start, now))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="sample-gcash-history.pdf"'})


@lru_cache
def clients():
    """Created on first use so tests and Messenger-only routes need no Google credentials."""
    # 90 s: one Vertex request once hung for over 7 minutes; callers turn the timeout into a 502.
    gemini = genai.Client(vertexai=True, project=PROJECT_ID, location="global", http_options={"timeout": 90_000})
    return gemini, firestore.Client(project=PROJECT_ID)


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
    source = {"type": "messenger", "conversation_id": conversation_id}
    if demo.is_demo(conversation_id):
        source = {"type": "demo", "conversation_id": conversation_id, "owner": demo_doc(conversation_id)["owner"]}
    order = {
        "status": "draft",
        "version": 1,
        "source": source,
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
    try:
        delivery.can_dispatch(order)
        dispatch_blocked = None
    except rules.Refused as e:
        dispatch_blocked = str(e)
    return {**order, "id": snap.id, "missing": rules.missing(order["draft"]), "packing": packing, "packing_blocked": blocked,
            "dispatch_blocked": dispatch_blocked, "refundable": refunds.refundable(order)}


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
    if user.get("demo"):
        docs = orders().where(filter=firestore.FieldFilter("source.owner", "==", user["uid"])).stream()
    else:
        # ponytail: over-fetches to skip other people's demo orders; query by source.type once demo orders outnumber real ones
        docs = orders().order_by("created_at", direction=firestore.Query.DESCENDING).limit(100).stream()
    mine = [d for d in docs if visible(d.to_dict(), user)]
    mine.sort(key=lambda d: d.get("created_at") or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
    return [present(d) for d in mine[:25]]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(current_user)):
    snap = orders().document(order_id).get()
    if not snap.exists:
        raise HTTPException(404, "Order not found")
    return present(snap)


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
    shipment = order.get("fulfillment") or (order.get("fulfillment_history") or [None])[-1]
    return {
        "status": order["status"],
        "items": [{k: i[k] for k in ("name", "variant", "quantity", "unit_price")} for i in d["items"]],
        "total": d["total"],
        **{f: d[f]["value"] for f in rules.FIELDS},
        "payment": (order.get("payment") or {}).get("status"),
        # Before the first payment request, the key decides: PayMongo's test-mode QR codes are real and must not be scanned.
        "test_mode": order["payment"]["livemode"] is False if order.get("payment") else PAYMONGO_KEY.startswith("sk_test_"),
        "gcash_confirmed": pack.paid_by(order) == "gcash_seller_confirmed",
        "shipment": shipment and {
            "status": shipment["status"],
            "courier": shipment["courier"],
            "simulated": shipment["courier"] == "simulator",
            "share_link": shipment.get("share_link"),  # only the customer holding this order's link sees it
        },
        "received": (order.get("receipt") or {}).get("fulfillment_id") == (shipment or {}).get("id") and bool(shipment),
        "claims": [{"id": c["id"], "type": cases.TYPES[c["type"]], "status": c["status"], "created_at": c["created_at"], "photos": len(c["photos"])}
                   for c in order.get("claims", [])],
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
        subject = event["data"]
        subject_id = subject["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Unrecognised event")
    # Each event only tells us where to look; what we fetch from PayMongo is what counts. Duplicates are harmless.
    if event.get("type") == "checkout_session.payment.paid":
        await run_in_threadpool(reconcile_session, subject_id)
    elif event.get("type") in ("payment.refunded", "payment.refund.updated"):
        payment_id = (subject.get("attributes") or {}).get("payment_id") or (subject_id if subject.get("type") == "payment" else None)
        if payment_id:
            await run_in_threadpool(reconcile_refunds_for, payment_id)
    return {"received": True}


class LalamoveNoAnswer(Exception):
    """No answer, or a server error: the request may or may not have taken effect."""


def lalamove_signature(timestamp: str, method: str, path: str, body: str) -> str:
    """Lalamove v3: HMAC-SHA256 of "<timestamp>\\r\\n<METHOD>\\r\\n<path>\\r\\n\\r\\n<body>" with the API secret.
    Webhooks are signed the same way, over the compact JSON of their data. Checked against real sandbox events."""
    return hmac.new(LALAMOVE_SECRET.encode(), f"{timestamp}\r\n{method}\r\n{path}\r\n\r\n{body}".encode(), hashlib.sha256).hexdigest()


def lalamove(method: str, path: str, body: dict | None = None) -> dict:
    if not LALAMOVE_KEY or not LALAMOVE_SECRET:
        raise HTTPException(503, "Lalamove is not configured")
    raw = json.dumps(body, separators=(",", ":")) if body else ""
    ts = str(int(time.time() * 1000))
    headers = {"Authorization": f"hmac {LALAMOVE_KEY}:{ts}:{lalamove_signature(ts, method, path, raw)}", "Market": "PH", "Content-Type": "application/json"}
    try:
        r = requests.request(method, LALAMOVE_URL + path, data=raw or None, headers=headers, timeout=15)
    except requests.RequestException:
        raise LalamoveNoAnswer()
    if r.status_code >= 500:
        raise LalamoveNoAnswer()
    try:
        payload = r.json() if r.content else {}
    except ValueError:
        payload = {}
    if not r.ok:
        problems = "; ".join(e.get("message") or e.get("id", "") for e in payload.get("errors", [])) or f"HTTP {r.status_code}"
        raise HTTPException(502, f"Lalamove: {problems}")
    return payload.get("data") or {}


def lalamove_time(text: str | None) -> datetime:
    """Lalamove sends times like 2026-10-13T19:05.00Z; the minute is what is reliable."""
    found = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", text or "")
    return datetime.fromisoformat(found[1]).replace(tzinfo=timezone.utc) if found else datetime.now(timezone.utc)


def lalamove_pod(provider_order_id: str) -> dict | None:
    """Proof of delivery for the drop-off stop, as Lalamove reports it. Missing POD means unavailable, not failed."""
    try:
        stops = lalamove("GET", f"/v3/orders/{provider_order_id}").get("stops", [])
    except (HTTPException, LalamoveNoAnswer):
        return None
    pod = (stops[-1].get("POD") or {}) if stops else {}
    if not pod:
        return None
    # ponytail: keeps Lalamove's image link, which may expire; copy the image into the evidence bucket when retention lands
    return {"status": pod.get("status"), "image_url": pod.get("image"), "delivered_at": pod.get("deliveredAt"), "source": "lalamove",
            "fetched_at": datetime.now(timezone.utc)}


def shipment_ref(provider_order_id: str | None, quotation_id: str | None = None):
    for field, value in (("fulfillment.provider_order_id", provider_order_id), ("fulfillment.quotation.id", quotation_id)):
        if value:
            found = list(orders().where(filter=firestore.FieldFilter(field, "==", value)).limit(1).stream())
            if found:
                return found[0].reference
    return None


def lalamove_rule(provider_order_id: str, raw_status: str | None, eid: str, at: datetime, order_data: dict):
    """Link the Lalamove order to its shipment if this is the first we hear of it, then record the status event."""
    now = datetime.now(timezone.utc)
    status = delivery.LALAMOVE.get(raw_status or "") or ("driver_assigned" if raw_status == "DRIVER_ASSIGNED" else None)
    pod = lalamove_pod(provider_order_id) if status == "delivered" else None

    def rule(order: dict) -> dict:
        updates = {}
        if not (order.get("fulfillment") or {}).get("provider_order_id"):
            updates = delivery.booked(order, provider_order_id, order_data.get("shareLink"), now)
            order = {**order, **updates}
        if status:
            ev = delivery.event(eid, "provider", raw_status, status, at, now)
            updates.update(delivery.apply_event(order, ev, driver_id=order_data.get("driverId") or None, share_link=order_data.get("shareLink"), pod=pod))
        return updates

    return rule


@app.post("/api/webhooks/lalamove")
async def lalamove_webhook(request: Request):
    """Lalamove order events. Always answers 200, because Lalamove pings the URL when it is registered and retries otherwise.

    Only events with our API key and a valid signature are stored and acted on. The raw event is kept; the event ID dedupes.
    """
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        return {"received": True}
    if not (LALAMOVE_KEY and isinstance(event, dict) and event.get("apiKey") == LALAMOVE_KEY and len(raw) < 65536):
        return {"received": True}
    data = event.get("data")
    bodies = {json.dumps(data, separators=(",", ":")), json.dumps(data, separators=(",", ":"), ensure_ascii=False)}
    expected = {lalamove_signature(str(event.get("timestamp")), "POST", request.url.path, b) for b in bodies}
    if not any(hmac.compare_digest(e, str(event.get("signature", ""))) for e in expected):
        return {"received": True}
    doc = {"provider": "lalamove", "raw": raw.decode(), "path": request.url.path, "verified": True, "received_at": firestore.SERVER_TIMESTAMP}
    ref = clients()[1].collection("webhookEvents").document(f"lalamove_{event.get('eventId') or secrets.token_hex(8)}")
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
            rule = lalamove_rule(o["orderId"], raw_status, f"lalamove:{event['eventId']}", lalamove_time(data.get("updatedAt")), o)
            try:
                await run_in_threadpool(change, target, rule)
            except HTTPException:
                pass  # the event is stored; a refresh reconciles from Lalamove
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
        f"[{r['line']}] {r['expected']['quantity']} x {extraction.inline(r['expected']['name'])}"
        + (f" ({extraction.inline(r['expected']['variant'])})" if r["expected"]["variant"] else "")
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
    if user.get("demo"):
        return DEMO_GCASH
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


class GcashRequest(BaseModel):
    says_paid: bool = False  # the customer says they already paid: check that payment instead of asking for one


@app.post("/api/orders/{order_id}/gcash/request")
def request_gcash(order_id: str, body: GcashRequest | None = None, user: dict = Depends(current_user)):
    pay_to = DEMO_GCASH if demo_order(order_id) else get_gcash(user)
    if not pay_to:
        raise HTTPException(409, "Add your GCash name and number in Settings first")
    now = datetime.now(timezone.utc)
    qr = pay_to.get("qr") or {}
    snapshot = {"name": pay_to["name"], "mobile": pay_to["mobile"], "masked_name": qr.get("masked_name"), "user_id": qr.get("user_id")}
    says_paid = bool(body and body.says_paid)
    return change(orders().document(order_id), lambda o: gcash.request(o, snapshot, user["email"], now, says_paid))


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


def index_scope(order: dict) -> str:
    """Prefix for the cross-order receipt indexes. Each demo visitor is a store of their own, so their sample receipts are
    compared only with their own orders, never with the real store's or another visitor's."""
    source = order.get("source") or {}
    return f"demo_{source.get('owner')}_" if source.get("type") == "demo" else ""


def read_receipt(order_id: str, manual: dict, message: dict, attachment: dict, now: datetime, scope: str = "") -> dict:
    """Copy one screenshot into our storage as evidence, have Gemini transcribe it, and run the checks."""
    if attachment.get("path"):  # a demo chat's image, already in our storage
        data, content_type = bucket().blob(attachment["path"]).download_as_bytes(), attachment["mime_type"]
    else:
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
    reused = first_seen("gcashImages", scope + sha, order_id)["order_id"] != order_id
    ref_key = gcash.reference_key(ref, receipt)
    used_elsewhere = {ref} if ref_key and first_seen("gcashReferences", scope + ref_key, order_id)["order_id"] != order_id else set()
    key = gcash.details_key(receipt)
    twin = first_seen("gcashReceiptDetails", scope + key, order_id, reference=ref, message_id=message["id"]) if key else None
    twin_reference = (twin["reference"] or "unreadable") if twin and twin["message_id"] != message["id"] and twin["reference"] != ref else None
    received_at = datetime.fromisoformat(message["created_time"])
    start, since = gcash.window(manual)
    checks = gcash.check_receipt(receipt, manual["amount"], start, received_at, manual["pay_to"], used_elsewhere, reused, twin_reference, since)
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
    """Check every customer image sent inside the GCash request's window that has not been checked yet."""
    ref, order = loaded(order_id)
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if order.get("source", {}).get("type") not in ("messenger", "demo"):
        raise HTTPException(409, "This order did not come from a chat")
    known = {c["message_id"] for c in manual["claims"]}
    images = [
        (m, a)
        for m in thread_messages(order["source"]["conversation_id"])
        if not m["from_page"] and m["id"] not in known and datetime.fromisoformat(m["created_time"]) >= gcash.window(manual)[0] - gcash.SLACK
        for a in m["attachments"][:1]
        if a["kind"] == "image" and a["name"] != "sticker"
    ][-MAX_RECEIPTS_PER_READ:]
    if not images:
        raise HTTPException(409, "No new images from the customer since you asked for GCash payment")
    now = datetime.now(timezone.utc)
    claims = [read_receipt(order_id, manual, m, a, now, index_scope(order)) for m, a in images]
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
        "checks": gcash.check_history(entry, manual["amount"], *gcash.window(manual)),
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
    return await run_in_threadpool(apply_statement, statement, hashlib.sha256(data).hexdigest(), user["email"], user)


def apply_statement(statement: dict, statement_id: str, by: str, user: dict) -> dict:
    now = datetime.now(timezone.utc)
    store_doc().collection("gcashStatements").document(statement_id).set({**statement, "uploaded_by": by, "uploaded_at": now})
    waiting = [d for d in orders().where(filter=firestore.FieldFilter("manual_payment.status", "in", ["requested", "claimed", "seller_confirmed"])).stream()
               if visible(d.to_dict(), user)]
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
    _, order = loaded(order_id)
    others = orders().where(filter=firestore.FieldFilter("manual_payment.confirmed.reference", "==", ref)).stream()
    elsewhere = any(d.id != order_id and index_scope(d.to_dict()) == index_scope(order) for d in others)
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: gcash.confirm(o, ref, user["email"], body.reason, now, elsewhere, body.invno))


# Dispatch and delivery. Lalamove is live in its sandbox; the simulator and other couriers are labelled as such.
class DispatchSettings(BaseModel):
    address: str = Field(min_length=5, max_length=300)
    pin: str = Field(min_length=3, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=7, max_length=30)


@app.get("/api/settings/dispatch")
def get_dispatch_settings(user: dict = Depends(current_user)):
    if user.get("demo"):
        return DEMO_PICKUP
    return (store_doc().get().to_dict() or {}).get("dispatch")


@app.put("/api/settings/dispatch")
def put_dispatch_settings(body: DispatchSettings, user: dict = Depends(current_user)):
    """Where couriers pick up from. Lalamove needs a map pin; the seller pastes it from Google Maps."""
    try:
        lat, lng = delivery.parse_pin(body.pin)
        phone = delivery.e164(body.phone)
    except rules.Refused as e:
        raise HTTPException(422, str(e))
    saved = {"address": body.address.strip(), "lat": lat, "lng": lng, "name": body.name.strip(), "phone": phone,
             "updated_by": user["email"], "updated_at": datetime.now(timezone.utc)}
    store_doc().set({"dispatch": saved}, merge=True)
    return saved


class QuoteIn(BaseModel):
    pin: str = Field(min_length=3, max_length=500)
    service: Literal["MOTORCYCLE", "SEDAN", "MPV", "VAN"] = "MOTORCYCLE"


def centavos_of(text: str) -> int:
    return int(round(float(text) * 100))


@app.post("/api/orders/{order_id}/dispatch/quote")
def quote_delivery(order_id: str, body: QuoteIn, user: dict = Depends(current_user)):
    """A Lalamove quotation for the seller to approve. Nothing is booked yet."""
    pickup = DEMO_PICKUP if demo_order(order_id) else get_dispatch_settings(user)
    if not pickup:
        raise HTTPException(409, "Add your pickup address and pin in Settings first")
    ref, order = loaded(order_id)
    try:
        delivery.can_dispatch(order)
        lat, lng = delivery.parse_pin(body.pin)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    stops = [{"coordinates": {"lat": str(pickup["lat"]), "lng": str(pickup["lng"])}, "address": pickup["address"]},
             {"coordinates": {"lat": str(lat), "lng": str(lng)}, "address": order["draft"]["address"]["value"]}]
    try:
        q = lalamove("POST", "/v3/quotations", {"data": {"serviceType": body.service, "language": "en_PH", "stops": stops}})
    except LalamoveNoAnswer:
        raise HTTPException(502, "Lalamove did not answer. Try again.")
    price = q.get("priceBreakdown") or {}
    quotation = {"id": q["quotationId"], "service": body.service, "price": centavos_of(price.get("total", "0")), "currency": price.get("currency", "PHP"),
                 "expires_at": lalamove_time(q.get("expiresAt")), "stop_ids": [s["stopId"] for s in q.get("stops", [])], "dropoff": [lat, lng],
                 "livemode": "sandbox" not in LALAMOVE_URL}
    now = datetime.now(timezone.utc)
    fid = ((order.get("fulfillment") or {}).get("id")) or secrets.token_hex(8)
    return change(ref, lambda o: delivery.start(o, fid, "lalamove", user["email"], now, quotation=quotation))


@app.post("/api/orders/{order_id}/dispatch/book")
def book_delivery(order_id: str, user: dict = Depends(current_user)):
    """Book the approved quote. The quote is locked first, so a retry cannot book twice."""
    pickup = DEMO_PICKUP if demo_order(order_id) else get_dispatch_settings(user)
    ref, order = loaded(order_id)
    f, d = order.get("fulfillment") or {}, order["draft"]
    try:
        phone = delivery.e164(d["phone"]["value"])
    except rules.Refused as e:
        raise HTTPException(409, f"Recipient phone: {e}")
    if not pickup or len((f.get("quotation") or {}).get("stop_ids", [])) != 2:
        raise HTTPException(409, "Get a Lalamove quote first")
    now = datetime.now(timezone.utc)
    change(ref, lambda o: delivery.begin_booking(o, now))
    q = f["quotation"]
    payload = {"data": {
        "quotationId": q["id"],
        "sender": {"stopId": q["stop_ids"][0], "name": pickup["name"], "phone": pickup["phone"]},
        "recipients": [{"stopId": q["stop_ids"][1], "name": d["recipient_name"]["value"], "phone": phone, "remarks": d["delivery_instructions"]["value"] or ""}],
        "isPODEnabled": True,
        "metadata": {"orderId": order_id, "fulfillmentId": f["id"]},
    }}
    try:
        placed = lalamove("POST", "/v3/orders", payload)
    except LalamoveNoAnswer:
        return change(ref, delivery.booking_uncertain)  # Lalamove's ORDER_CREATED event, matched by quotation, settles it
    except HTTPException as e:
        change(ref, lambda o: delivery.booking_failed(o, e.detail))
        raise
    return change(ref, lambda o: delivery.booked(o, placed["orderId"], placed.get("shareLink"), datetime.now(timezone.utc)))


@app.post("/api/orders/{order_id}/dispatch/release")
def release_delivery(order_id: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: delivery.release_booking(o, now))


@app.post("/api/orders/{order_id}/dispatch/refresh")
def refresh_delivery(order_id: str, user: dict = Depends(current_user)):
    """Ask Lalamove for the order's current status, for when a webhook is late or missed."""
    ref, order = loaded(order_id)
    f = order.get("fulfillment") or {}
    if f.get("courier") != "lalamove" or not f.get("provider_order_id"):
        raise HTTPException(409, "No booked Lalamove order to refresh")
    try:
        o = lalamove("GET", f"/v3/orders/{f['provider_order_id']}")
    except LalamoveNoAnswer:
        raise HTTPException(502, "Lalamove did not answer. Status unchanged.")
    raw = o.get("status")
    eid = f"lalamove:{f['provider_order_id']}:poll:{raw}"  # one event per status seen by polling
    return change(ref, lalamove_rule(f["provider_order_id"], raw, eid, datetime.now(timezone.utc), o))


class StartIn(BaseModel):
    courier: Literal["simulator", "other"]
    tracking: str = Field(default="", max_length=200)


@app.post("/api/orders/{order_id}/dispatch/start")
def start_delivery(order_id: str, body: StartIn, user: dict = Depends(current_user)):
    """The delivery simulator (every event labelled demo) or another courier whose updates the seller reports."""
    if body.courier == "other" and not body.tracking.strip():
        raise HTTPException(422, "Enter the courier and tracking number")
    now = datetime.now(timezone.utc)
    fid = secrets.token_hex(8)
    return change(orders().document(order_id), lambda o: delivery.start(o, fid, body.courier, user["email"], now, tracking=body.tracking.strip() or None))


class StepIn(BaseModel):
    status: Literal["driver_assigned", "picked_up", "delivered", "failed"]
    note: str = Field(default="", max_length=500)


@app.post("/api/orders/{order_id}/dispatch/step")
def step_delivery(order_id: str, body: StepIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)

    def rule(o: dict) -> dict:
        source = "demo" if (o.get("fulfillment") or {}).get("courier") == "simulator" else "seller_reported"
        return delivery.step(o, body.status, source, user["email"], now, body.note)

    return change(orders().document(order_id), rule)


@app.post("/api/orders/{order_id}/dispatch/cancel")
def cancel_delivery(order_id: str, body: rules.Cancel, user: dict = Depends(current_user)):
    ref, order = loaded(order_id)
    f = order.get("fulfillment") or {}
    if f.get("status") == "booking":
        raise HTTPException(409, "A booking is waiting for Lalamove's answer. Release it once the quote expires.")
    if f.get("courier") == "lalamove" and f.get("provider_order_id"):
        try:
            lalamove("DELETE", f"/v3/orders/{f['provider_order_id']}")
        except LalamoveNoAnswer:
            raise HTTPException(502, "Lalamove did not answer. The booking may still be active; try again.")
    now = datetime.now(timezone.utc)
    return change(ref, lambda o: delivery.cancel(o, user["email"], body.reason, now))


@app.post("/api/public/orders/{token}/received")
def public_received(token: str):
    now = datetime.now(timezone.utc)
    return customer_view(change(by_token(token).reference, lambda o: delivery.acknowledge(o, "customer_link", now)))


# Claims and evidence review.
MAX_OPEN_CLAIMS = 5


def claim_photo(order_id: str, cid: str, data: bytes, content_type: str, source: str) -> dict:
    if content_type not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo")
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photos must be under 10 MB")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{STORE}/orders/{order_id}/claims/{cid}/{sha}"
    try:
        bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same photo again
    return {"id": sha[:16], "path": path, "sha256": sha, "content_type": content_type, "size": len(data), "source": source,
            "uploaded_at": datetime.now(timezone.utc)}


def new_claim(ref, body: cases.ClaimIn, source: str, by: str) -> dict:
    now, cid = datetime.now(timezone.utc), secrets.token_hex(6)

    def rule(o: dict) -> dict:
        if sum(c["status"] != "resolved" for c in o.get("claims", [])) >= MAX_OPEN_CLAIMS:
            raise rules.Refused("There are already several open reports on this order")
        return cases.open_claim(o, cid, body, source, by, now)

    return change(ref, rule)


@app.post("/api/public/orders/{token}/claims")
def public_claim(token: str, body: cases.ClaimIn):
    return customer_view(new_claim(by_token(token).reference, body, "customer_link", "customer"))


@app.post("/api/public/orders/{token}/claims/{cid}/photos")
async def public_claim_photo(token: str, cid: str, request: Request):
    snap = await run_in_threadpool(by_token, token)
    data = await request.body()
    photo = await run_in_threadpool(claim_photo, snap.id, cid, data, request.headers.get("content-type", "").split(";")[0].strip().lower(), "customer")
    now = datetime.now(timezone.utc)
    return customer_view(await run_in_threadpool(change, snap.reference, lambda o: cases.add_photos(o, cid, [photo], "customer", now)))


@app.post("/api/orders/{order_id}/claims")
def seller_claim(order_id: str, body: cases.ClaimIn, user: dict = Depends(current_user)):
    return new_claim(orders().document(order_id), body, "seller", user["email"])


@app.post("/api/orders/{order_id}/claims/{cid}/photos")
async def seller_claim_photo(order_id: str, cid: str, request: Request, user: dict = Depends(current_user)):
    data = await request.body()
    photo = await run_in_threadpool(claim_photo, order_id, cid, data, request.headers.get("content-type", "").split(";")[0].strip().lower(), "seller")
    now = datetime.now(timezone.utc)
    return await run_in_threadpool(change, orders().document(order_id), lambda o: cases.add_photos(o, cid, [photo], user["email"], now))


@app.get("/api/orders/{order_id}/claims/{cid}/photos/{photo_id}")
def claim_photo_file(order_id: str, cid: str, photo_id: str, user: dict = Depends(current_user)):
    _, order = loaded(order_id)
    try:
        photo = next(p for p in cases.claim(order, cid)["photos"] if p["id"] == photo_id)
    except (rules.Refused, StopIteration):
        raise HTTPException(404, "Photo not found")
    return Response(bucket().blob(photo["path"]).download_as_bytes(), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/orders/{order_id}/claims/{cid}/review")
def review_claim(order_id: str, cid: str, user: dict = Depends(current_user)):
    """Gemini compares the claim with the order's records. Citations are checked in code before anything is shown."""
    ref, order = loaded(order_id)
    try:
        c = cases.claim(order, cid)
    except rules.Refused as e:
        raise HTTPException(404, str(e))
    messages = order.get("messages", [])
    if (order.get("source") or {}).get("type") in ("messenger", "demo"):
        try:
            messages = thread_messages(order["source"]["conversation_id"])
        except HTTPException:
            pass  # the snapshot taken when the order was drafted
    blocks, images = cases.evidence(order, messages, c)
    contents: list = ["Records:\n" + "\n".join(blocks)]
    shown = []
    for label, photo in images:
        try:
            contents += [f"Image {label}:", types.Part.from_bytes(data=bucket().blob(photo["path"]).download_as_bytes(), mime_type=photo["content_type"])]
            shown.append((label, photo))
        except Exception:
            pass  # an image that cannot be loaded cannot be cited
    try:
        response = clients()[0].models.generate_content(
            model=GEMINI_MODEL, contents=contents,
            config={"system_instruction": cases.SYSTEM, "response_mime_type": "application/json", "response_schema": cases.Review, "temperature": 0},
        )
        found = cases.Review.model_validate_json(response.text)
    except Exception:
        raise HTTPException(502, "Could not review this claim. Try again.")
    now = datetime.now(timezone.utc)
    review = cases.checked(found, cases.ids(blocks, shown), GEMINI_MODEL, now)
    # The text behind each citation, so the seller can check it without hunting.
    cited = {e for f in review["findings"] for e in f["evidence"]} | set(review["recommendation"]["evidence"])
    texts = {b[1 : b.index("]")]: b for b in blocks} | {label: f"[{label}] Photo" for label, _ in shown}
    review["sources"] = {k: v for k, v in texts.items() if k in cited}
    return change(ref, lambda o: cases.record_review(o, cid, review, user["email"], now))


@app.post("/api/orders/{order_id}/claims/{cid}/wait")
def wait_claim(order_id: str, cid: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: cases.wait_for_evidence(o, cid, user["email"], now))


@app.post("/api/orders/{order_id}/claims/{cid}/decide")
def decide_claim(order_id: str, cid: str, body: cases.DecisionIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: cases.decide(o, cid, body, user["email"], now))


class EscalateIn(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    next_action: str = Field(min_length=1, max_length=500)


@app.post("/api/orders/{order_id}/claims/{cid}/escalate")
def escalate_claim(order_id: str, cid: str, body: EscalateIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: cases.escalate(o, cid, body.owner, body.next_action, user["email"], now))


@app.post("/api/orders/{order_id}/claims/{cid}/replacement")
def replace_claim(order_id: str, cid: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: cases.start_replacement(o, cid, user["email"], now))


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=1000)


@app.post("/api/orders/{order_id}/claims/{cid}/resolve")
def resolve_claim(order_id: str, cid: str, body: ResolveIn, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: cases.resolve(o, cid, body.note, user["email"], now))


# Refunds. PayMongo first; its refusal or silence leads to the manual route, never to a guess.
def paymongo_refund(payment_id: str, amount: int, reason: str, op_id: str, order_id: str) -> dict:
    """{"refund": {id, status}} if accepted, {"refused": detail} if PayMongo said no, {} if no answer came."""
    try:
        r = requests.post("https://api.paymongo.com/v1/refunds", auth=(PAYMONGO_KEY, ""), timeout=20, json={"data": {"attributes": {
            "amount": amount, "payment_id": payment_id, "reason": reason, "metadata": {"operation_id": op_id, "order_id": order_id}}}})
        body = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if r.status_code >= 500:
        return {}
    if not r.ok:
        return {"refused": "; ".join(e.get("detail", "") for e in body.get("errors", [])) or f"HTTP {r.status_code}"}
    return {"refund": {"id": body["data"]["id"], "status": body["data"]["attributes"]["status"]}}


@app.post("/api/orders/{order_id}/refunds")
def submit_refund(order_id: str, body: refunds.RefundIn, user: dict = Depends(current_user)):
    if not PAYMONGO_KEY:
        raise HTTPException(503, "Payments are not configured")
    ref = orders().document(order_id)
    op_id, now = "op_" + secrets.token_hex(8), datetime.now(timezone.utc)
    saved = change(ref, lambda o: refunds.reserve(o, op_id, body, user["email"], now))
    payment_id = saved["payment"]["payment_id"]
    answer = paymongo_refund(payment_id, body.amount, body.reason, op_id, order_id)
    return change(ref, lambda o: refunds.submitted(o, op_id, answer, datetime.now(timezone.utc)))


@app.post("/api/orders/{order_id}/refunds/manual")
def manual_refund(order_id: str, body: refunds.ManualIn, user: dict = Depends(current_user)):
    op_id, now = "op_" + secrets.token_hex(8), datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: refunds.manual(o, op_id, body, user["email"], now))


def provider_refunds(payment_id: str) -> list[dict]:
    attrs = paymongo("GET", f"payments/{payment_id}")["attributes"]
    out = []
    for x in attrs.get("refunds") or []:
        a = x.get("attributes", x)
        out.append({"id": x.get("id") or a.get("id"), "amount": a["amount"], "status": a["status"], "metadata": a.get("metadata") or {}})
    return out


def reconcile_refunds_for(payment_id: str) -> dict | None:
    found = list(orders().where(filter=firestore.FieldFilter("payment.payment_id", "==", payment_id)).limit(1).stream())
    if not found:
        return None
    listed = provider_refunds(payment_id)
    return change(found[0].reference, lambda o: refunds.reconcile(o, listed, datetime.now(timezone.utc)))


@app.post("/api/orders/{order_id}/refunds/check")
def check_refunds(order_id: str, user: dict = Depends(current_user)):
    """Ask PayMongo which refunds exist on the payment. Settles unanswered submissions and finds Dashboard refunds."""
    _, order = loaded(order_id)
    payment_id = (order.get("payment") or {}).get("payment_id")
    if not payment_id:
        raise HTTPException(409, "This order has no PayMongo payment")
    return reconcile_refunds_for(payment_id)


# Patterns. Recomputed on every load from the store's own orders.
def pattern_cards(days: int, user: dict) -> list[dict]:
    # ponytail: reads every order on each load; keep a rolling aggregate when a store has thousands of orders
    found = [{**o, "id": d.id} for d in orders().stream() if visible(o := d.to_dict(), user)]
    reviews = {r["key"]: r for r in (d.to_dict() for d in store_doc().collection("patternReviews").stream())}
    return patterns.with_review(patterns.analyse(found, datetime.now(timezone.utc), days), reviews)


@app.get("/api/patterns")
def list_patterns(days: int = 30, user: dict = Depends(current_user)):
    return pattern_cards(max(1, min(days, 365)), user)


@app.get("/api/orders/{order_id}/patterns")
def order_patterns(order_id: str, user: dict = Depends(current_user)):
    return [c for c in pattern_cards(30, user) if any(i["order_id"] == order_id for i in c["incidents"])]


class PatternReviewIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    incidents_hash: str = Field(min_length=1, max_length=64)
    status: Literal["reviewing", "explained_operationally", "unresolved", "dismissed"]
    reason: str = Field(min_length=1, max_length=1000)


@app.post("/api/patterns/review")
def review_pattern(body: PatternReviewIn, user: dict = Depends(current_user)):
    """The seller's review of one card, tied to the cases it covered. New cases make the card new again."""
    record = {**body.model_dump(), "reason": body.reason.strip(), "by": user["email"], "at": datetime.now(timezone.utc)}
    store_doc().collection("patternReviews").document(hashlib.sha256(body.key.encode()).hexdigest()).set(record)
    return record
