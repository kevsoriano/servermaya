import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import PreconditionFailed
from google.cloud import firestore
from pydantic import BaseModel, Field

from core import config, db
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


def demo_post(conversation_id: str, from_page: bool, text: str, attachment: dict | None = None) -> dict:
    """Add a message to a demo chat. Nothing goes to Messenger."""
    d = demo_doc(conversation_id)
    if len(d["messages"]) >= demo.MAX_MESSAGES:
        raise HTTPException(409, "This demo chat is full. Start a new one.")
    m = demo.message(from_page, d["name"], text, datetime.now(UTC), [attachment] if attachment else None)
    demo_threads().document(conversation_id).update(
        {"messages": firestore.ArrayUnion([m]), "updated_time": m["created_time"], "snippet": text or "Sent a photo"}
    )
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
    path = f"stores/{config.STORE}/demo/{sha}"
    try:
        db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same image was sent before
    return {"kind": "image", "mime_type": content_type, "name": None, "url": f"/api/public/demo/images/{sha}", "preview_url": None, "path": path}


def thread_messages(conversation_id: str) -> list[dict]:
    """Current messages of the chat an order came from, Messenger or demo."""
    return demo_doc(conversation_id)["messages"] if demo.is_demo(conversation_id) else messenger.fetch_messages(conversation_id)


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)  # Messenger's limit for one text message
    order_id: str | None = Field(default=None, max_length=64)
