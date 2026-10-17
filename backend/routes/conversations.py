import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import delivery, extraction
from domain import orders as rules
from domain import packing as pack
from domain import signals as risk
from integrations import messenger
from seed import demo
from services import catalog, chats, deliveries, seeding

router = APIRouter()


# ponytail: first page only (25 threads, 50 messages), add paging when a Page outgrows it
@router.get("/api/conversations")
def conversations(user: dict = Depends(auth.current_user)):
    if user.get("demo"):
        return demo_list(user)
    try:
        data = messenger.graph(
            f"{config.PAGE_ID}/conversations",
            platform="messenger",
            fields="id,updated_time,snippet,participants,messages.limit(1){from,created_time}",
        )
    except HTTPException as e:
        print(f"Messenger thread list failed: {e.detail}")  # demo chats still load, so the app stays usable
        data = {}
    threads = data.get("data", [])
    seen, actions = read_marks(user), order_actions([c["id"] for c in threads])
    customers = {c["id"]: next((p for p in c.get("participants", {}).get("data", []) if p.get("id") != config.PAGE_ID), None) for c in threads}
    pics = messenger.profile_pics([p["id"] for p in customers.values() if p and p.get("id")])
    out = []
    for c in threads:
        customer = customers[c["id"]]
        last = next(iter(c.get("messages", {}).get("data", [])), {})
        # The newest message is the customer's, so the seller owes a reply.
        waiting = bool(last) and last.get("from", {}).get("id") != config.PAGE_ID
        at = datetime.fromisoformat(last["created_time"]) if waiting and last.get("created_time") else None
        out.append(
            {
                "id": c["id"],
                "updated_time": c.get("updated_time"),
                "snippet": c.get("snippet", ""),
                "customer": customer and {**customer, "picture": pics.get(customer.get("id"))},
                "open_checks": cached_open_checks(c["id"]),
                "unread": bool(at) and (c["id"] not in seen or seen[c["id"]] < at),
                "awaiting_reply": waiting,
                "reply_until": (at + chats.reply_window()).isoformat() if at else None,
                "order_actions": actions.get(c["id"], []),
            }
        )
    return out + demo_list(user)


def demo_list(user: dict) -> list[dict]:
    """This seller's demo chats, seeded the first time they open the inbox. Never read state or reply windows: nobody is waiting."""
    try:
        db.store_doc().collection("demoUsers").document(user["uid"]).create({"seeded_at": firestore.SERVER_TIMESTAMP})
        seeding.seed_demo(user)
    except AlreadyExists:
        pass
    docs = sorted(
        chats.demo_threads().where(filter=firestore.FieldFilter("owner", "==", user["uid"])).stream(),
        key=lambda d: d.get("updated_time"),
        reverse=True,
    )
    actions = order_actions([d.id for d in docs])
    return [
        {
            "id": d.id,
            "updated_time": d.get("updated_time"),
            "snippet": d.get("snippet"),
            "customer": {"id": d.id, "name": d.get("name"), "picture": None},
            "hint": (d.to_dict() or {}).get("hint"),  # seeded chats only: {short, steps}, what to try in this one
            "open_checks": cached_open_checks(d.id),
            "unread": False,
            "awaiting_reply": False,
            "reply_until": None,
            "order_actions": actions.get(d.id, []),
            "demo": True,
        }
        for d in docs
    ]


def read_docs():
    return db.clients()[1].collection("stores").document(config.STORE).collection("reads")


def read_marks(user: dict) -> dict:
    """When this seller last opened each thread. Read state is per seller, so one opening a thread does not clear it for the rest."""
    try:
        doc = read_docs().document(user["uid"]).get()
    except Exception:
        return {}  # the list still works without Firestore; every waiting thread just shows as unread
    return (doc.to_dict() or {}).get("seen", {}) if doc.exists else {}


@router.post("/api/conversations/{conversation_id}/seen")
def mark_seen(conversation_id: str, user: dict = Depends(auth.current_user)):
    """Recorded in our database only. Nothing goes to Meta, so the customer never gets a "Seen" receipt."""
    if not (re.fullmatch(r"t_\d+", conversation_id) or demo.is_demo(conversation_id)):
        raise HTTPException(400, "Invalid conversation id")
    read_docs().document(user["uid"]).set({"seen": {conversation_id: datetime.now(UTC)}}, merge=True)
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
        packing = order.get("packing") or {}
        if pack.paid_by(order) and not (packing.get("version") == order.get("version") and packing.get("completed")):
            return "Ready to pack"
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
            for d in db.orders().where(filter=firestore.FieldFilter("source.conversation_id", "in", batch)).stream():
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
        doc = db.signal_docs().document(conversation_id).get()
    except Exception:
        return None  # the list still works without Firestore
    if not doc.exists:
        return None
    d = doc.to_dict()
    return sum(1 for s in risk.resolve(d["signals"], [o for _, o in db.chat_orders(conversation_id)], d.get("reviews")) if risk.is_open(s))


@router.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(auth.current_user)):
    if demo.is_demo(conversation_id):
        return chats.demo_doc(conversation_id)["messages"]
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    return messenger.fetch_messages(conversation_id)


@router.get("/api/conversations/{conversation_id}/signals")
def conversation_signals(conversation_id: str, user: dict = Depends(auth.current_user)):
    """Checks before shipping for one thread. Gemini only reruns when a new message has arrived since the last analysis."""
    messages = conversation(conversation_id, user)
    latest = messages[-1]["id"] if messages else None
    ref = db.signal_docs().document(conversation_id)
    cached = ref.get().to_dict() or {}
    if cached.get("latest_message_id") == latest and "signals" in cached:
        found = cached["signals"]
    else:
        if messages:
            auth.ai_budget(user)
        try:
            found = risk.detect(db.clients()[0], config.GEMINI_MODEL, messages) if messages else []
        except Exception:
            raise HTTPException(502, "Could not analyse this conversation")
        # merge keeps the seller's reviews across re-analysis
        ref.set({"signals": found, "latest_message_id": latest, "model": config.GEMINI_MODEL, "analysed_at": firestore.SERVER_TIMESTAMP}, merge=True)
    chat = [o for _, o in db.chat_orders(conversation_id)]
    return [{**s, "context": risk.payment_context(s, messages, chat)} for s in risk.resolve(found, chat, cached.get("reviews"))]


class Review(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=500)
    reopen: bool = False


@router.post("/api/conversations/{conversation_id}/signals/review")
def review_signal(conversation_id: str, body: Review, user: dict = Depends(auth.current_user)):
    """Seller marks a check as reviewed, with a reason, or reopens it. History of who and why stays on the record."""
    ref = db.signal_docs().document(conversation_id)
    if body.key not in {risk.key(s) for s in (ref.get().to_dict() or {}).get("signals", [])}:
        raise HTTPException(404, "That check is no longer in this conversation")
    if body.reopen:
        ref.set({"reviews": {body.key: firestore.DELETE_FIELD}}, merge=True)
    else:
        if not body.note.strip():
            raise HTTPException(422, "Say why this check is fine")
        now = datetime.now(UTC)
        ref.set({"reviews": {body.key: {"by": user["email"], "at": now, "note": body.note.strip()}}}, merge=True)
    return conversation_signals(conversation_id, user)


@router.post("/api/conversations/{conversation_id}/reply")
def reply(conversation_id: str, body: chats.Reply, user: dict = Depends(auth.current_user)):
    """Send one text message as the Page. Only ever called from an explicit seller click."""
    return {"message_id": chats.send(conversation_id, body.text, user, body.order_id)["message_id"]}


REPLY_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}  # what Messenger shows as an image


@router.post("/api/conversations/{conversation_id}/reply/image")
async def reply_image(conversation_id: str, request: Request, order_id: str | None = None, user: dict = Depends(auth.current_user)):
    """Send one image as the Page, such as the seller's GCash QR. The body is the image itself. Kept as evidence of what was sent."""
    if demo.is_demo(conversation_id):
        return await chats.demo_image(conversation_id, request, True)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in REPLY_IMAGE_TYPES:
        raise HTTPException(415, "Send a JPEG, PNG or GIF image")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Images must be under 10 MB")
    if order_id and not re.fullmatch(r"\w{1,64}", order_id):
        raise HTTPException(400, "Invalid order id")
    return await run_in_threadpool(chats.send_image, conversation_id, data, content_type, user, order_id)


@router.get("/api/conversations/{conversation_id}/orders")
def list_chat_orders(conversation_id: str, user: dict = Depends(auth.current_user)):
    """Every order made from this chat, newest first, so a returning customer's earlier orders stay in view. Taps on
    Confirm or Received in the chat are applied first."""
    if demo.is_demo(conversation_id) or re.fullmatch(r"t_\d+", conversation_id):
        chats.apply_answers(conversation_id)
        for oid, o in db.chat_orders(conversation_id):
            deliveries.advance_sandbox(oid, o)
    snaps = [
        d
        for d in db.orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream()
        if auth.visible(d.to_dict(), user)
    ]
    return [db.present(d) for d in sorted(snaps, key=lambda d: d.to_dict().get("created_at") or db.EPOCH, reverse=True)]


@router.post("/api/conversations/{conversation_id}/draft-order")
def draft_order(conversation_id: str, user: dict = Depends(auth.current_user)):
    messages = conversation(conversation_id, user)
    if not messages:
        raise HTTPException(400, "Conversation has no messages")
    previous = db.chat_orders(conversation_id)
    messages = rules.since_last_order(messages, [o for _, o in previous])
    if not any(not m["from_page"] for m in messages):
        raise HTTPException(409, "The customer has not written since this chat's last order. To change that order, edit it.")
    auth.ai_budget(user)
    try:
        draft = extraction.extract(db.clients()[0], config.GEMINI_MODEL, messages, catalog.products(demo.is_demo(conversation_id)))
    except Exception:
        raise HTTPException(502, "Could not extract an order from this conversation")
    draft = rules.carry_over(draft, previous)
    source = {"type": "messenger", "conversation_id": conversation_id}
    if demo.is_demo(conversation_id):
        source = {"type": "demo", "conversation_id": conversation_id, "owner": chats.demo_doc(conversation_id)["owner"]}
    order = {
        "status": "draft",
        "version": 1,
        "source": source,
        "messages": messages,  # snapshot of the evidence the draft was built from
        "draft": draft,  # copy of the current version, for listing
        "confirmations": [],
        "confirm_token": None,
        "model": config.GEMINI_MODEL,
        "created_by": user["email"],
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    ref = db.orders().document()
    batch = db.clients()[1].batch()
    batch.set(ref, order)
    batch.set(ref.collection("versions").document("1"), db.version_doc(draft, "ai_extraction", user["email"]))
    batch.commit()
    return db.present(ref.get())
