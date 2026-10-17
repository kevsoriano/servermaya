import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import PreconditionFailed
from google.cloud import firestore
from pydantic import BaseModel, Field

from core import config, db
from domain import delivery
from domain import orders as rules
from domain import packing as pack
from integrations import messenger
from seed import demo


def demo_threads():
    return db.store_doc().collection("demoThreads")


def demo_doc(conversation_id: str) -> dict:
    snap = demo_threads().document(conversation_id).get()
    if not snap.exists:
        raise HTTPException(404, "Demo chat not found")
    return snap.to_dict()


def demo_post(conversation_id: str, from_page: bool, text: str, attachment: dict | None = None, button: str | None = None) -> dict:
    """Add a message to a demo chat. Nothing goes to Messenger."""
    d = demo_doc(conversation_id)
    if len(d["messages"]) >= demo.MAX_MESSAGES:
        raise HTTPException(409, "This demo chat is full. Start a new one.")
    m = demo.message(from_page, d["name"], text, datetime.now(UTC), [attachment] if attachment else None, button)
    sent = "Sent a video" if (attachment or {}).get("kind") == "video" else "Sent a photo"
    demo_threads().document(conversation_id).update(
        {"messages": firestore.ArrayUnion([m]), "updated_time": m["created_time"], "snippet": text or sent}
    )
    return {"message_id": m["id"]}


DEMO_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


async def demo_image(conversation_id: str, request: Request, from_page: bool) -> dict:
    """An image, or a video such as an unboxing video, sent in a demo chat."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    video = content_type in pack.VIDEO_TYPES
    if content_type not in DEMO_IMAGE_TYPES and not video:
        raise HTTPException(415, "Send a JPEG, PNG, GIF or WebP image, or an MP4, MOV or WebM video")
    data = await request.body()
    if not data or len(data) > (pack.MAX_VIDEO_BYTES if video else pack.MAX_PHOTO_BYTES):
        raise HTTPException(413, "Videos must be under 30 MB" if video else "Images must be under 10 MB")
    attachment = await run_in_threadpool(demo_attachment, data, content_type)
    return await run_in_threadpool(demo_post, conversation_id, from_page, "", attachment)


def demo_attachment(data: bytes, content_type: str) -> dict:
    """Store an image or video for a demo chat and describe it like a Messenger attachment."""
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/demo/{sha}"
    try:
        db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same image was sent before
    return {
        "kind": content_type.split("/")[0],
        "mime_type": content_type,
        "name": None,
        "url": f"/api/public/demo/images/{sha}",
        "preview_url": None,
        "path": path,
    }


def thread_messages(conversation_id: str) -> list[dict]:
    """Current messages of the chat an order came from, Messenger or demo."""
    return demo_doc(conversation_id)["messages"] if demo.is_demo(conversation_id) else messenger.fetch_messages(conversation_id)


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)  # Messenger's limit for one text message
    order_id: str | None = Field(default=None, max_length=64)


REPLY_WINDOW = timedelta(hours=24)


def reply_until(messages: list[dict]) -> datetime | None:
    """Messenger only allows a normal reply within 24 hours of the customer's latest message."""
    times = [datetime.fromisoformat(m["created_time"]) for m in messages if not m["from_page"]]
    return max(times) + REPLY_WINDOW if times else None


def customer_to_reply(conversation_id: str) -> str:
    """The customer's PSID, if the 24-hour reply window is still open."""
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    until = reply_until(messenger.fetch_messages(conversation_id))
    if not until or datetime.now(UTC) > until:
        raise HTTPException(409, "Messenger only allows replies within 24 hours of the customer's last message.")
    participants = messenger.graph(conversation_id, fields="participants").get("participants", {}).get("data", [])
    customer = next((p["id"] for p in participants if p.get("id") != config.PAGE_ID), None)
    if not customer:
        raise HTTPException(409, "Could not find the customer in this conversation")
    return customer


def record_sent(conversation_id: str, order_id: str | None, sent: dict, user: dict, **what) -> dict:
    # Meta shows the reply as coming from the Page; this records which seller sent it.
    record = {
        "conversation_id": conversation_id,
        "order_id": order_id,
        **what,
        "message_id": sent.get("message_id"),
        "sent_by": user["email"],
        "sent_at": firestore.SERVER_TIMESTAMP,
    }
    db.clients()[1].collection("stores").document(config.STORE).collection("sentMessages").add(record)
    return {"message_id": sent.get("message_id")}


def send(conversation_id: str, text: str, user: dict, order_id: str | None = None, button: str | None = None) -> dict:
    """Send one text message as the Page, with at most one quick-reply button. Only ever called from an explicit seller click."""
    if demo.is_demo(conversation_id):
        return demo_post(conversation_id, True, text, button=button)
    customer = customer_to_reply(conversation_id)
    message = {"text": text} | ({"quick_replies": [{"content_type": "text", "title": button, "payload": button}]} if button else {})
    sent = messenger.graph(f"{config.PAGE_ID}/messages", send={"recipient": {"id": customer}, "messaging_type": "RESPONSE", "message": message})
    return record_sent(conversation_id, order_id, sent, user, text=text, **({"button": button} if button else {}))


def apply_answers(conversation_id: str, messages: list[dict] | None = None) -> None:
    """Apply the customer's taps in this chat: Confirm confirms the version it names, Received records that they have it.
    The chat is only read when one of its orders is waiting for a tap."""
    waiting = [(oid, o) for oid, o in db.chat_orders(conversation_id) if rules.waiting_for(o)]
    if not waiting:
        return
    if messages is None:
        messages = thread_messages(conversation_id)

    def rule(o: dict) -> dict:
        kinds = rules.waiting_for(o)  # again, inside the transaction
        if "confirm" in kinds and (m := rules.answer(o, "confirm", messages)):
            return rules.confirm(o, "customer", "chat", datetime.fromisoformat(m["created_time"]), message_id=m["id"])
        if "received" in kinds and (m := rules.answer(o, "received", messages)):
            try:
                return delivery.acknowledge(o, "customer_chat", datetime.fromisoformat(m["created_time"]))
            except rules.Refused:
                pass  # not sent yet
        return {}

    for oid, o in waiting:
        if any(rules.answer(o, k, messages) for k in rules.waiting_for(o)):
            db.change(db.orders().document(oid), rule)


ATTACHMENT_HOSTS = (".fbcdn.net", ".fbsbx.com", ".facebook.com")


def attachment_bytes(a: dict) -> tuple[bytes, str]:
    """A chat image's or video's bytes and type: a demo one from our storage, a Messenger one from Meta's CDN (only Meta's hosts)."""
    if a.get("path"):
        blob = db.bucket().blob(a["path"])
        return blob.download_as_bytes(), a.get("mime_type") or "image/jpeg"
    host = urlparse(a.get("url") or "").hostname or ""
    if not a["url"].startswith("https://") or not host.endswith(ATTACHMENT_HOSTS):
        raise HTTPException(422, "That photo is not from Messenger")
    try:
        r = requests.get(a["url"], timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        raise HTTPException(502, "Could not download the photo from Messenger. Reload the chat and try again.")
    return r.content, (r.headers.get("content-type") or a.get("mime_type") or "").split(";")[0].strip().lower()
