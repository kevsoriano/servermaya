import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.cloud import firestore
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import orders as rules
from domain import receipts as gcash
from seed import demo, samples
from services import chats, seeding

router = APIRouter()


# ponytail: public by hash so a plain <img> can show it; demo images only. Fetch with the seller's token if real photos ever land here.
@router.get("/api/public/demo/images/{sha}")
def demo_image_file(sha: str):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(404, "Image not found")
    blob = db.bucket().get_blob(f"stores/{config.STORE}/demo/{sha}")
    if not blob:
        raise HTTPException(404, "Image not found")
    return Response(blob.download_as_bytes(), media_type=blob.content_type, headers={"Cache-Control": "private, max-age=86400"})


class DemoChat(BaseModel):
    name: str = Field(default="Demo customer", min_length=1, max_length=100)
    chat: str = Field(default="", max_length=50_000)


@router.post("/api/demo/threads")
def new_demo_chat(body: DemoChat, user: dict = Depends(auth.current_user)):
    """A demo chat from pasted text, or an empty one to type into."""
    try:
        lines = demo.parse(body.chat)
    except ValueError as e:
        raise HTTPException(422, str(e))
    cid = demo.new_id()
    chats.demo_threads().document(cid).set(demo.thread(body.name.strip(), lines, user["uid"], datetime.now(UTC)))
    return {"id": cid}


@router.post("/api/demo/reset")
def reset_demo(user: dict = Depends(auth.current_user)):
    """Delete this seller's demo chats and every order made from them, then seed fresh ones. Real Messenger orders are never touched."""
    ids = [d.id for d in chats.demo_threads().where(filter=firestore.FieldFilter("owner", "==", user["uid"])).stream()]
    client, order_ids = db.clients()[1], []
    for i in range(0, len(ids), 30):  # Firestore caps an "in" filter at 30 values
        for o in db.orders().where(filter=firestore.FieldFilter("source.conversation_id", "in", ids[i : i + 30])).stream():
            order_ids.append(o.id)
            client.recursive_delete(o.reference)
        for s in db.store_doc().collection("sentMessages").where(filter=firestore.FieldFilter("conversation_id", "in", ids[i : i + 30])).stream():
            s.reference.delete()
    # Otherwise a receipt reused after the reset would be flagged as already used on a deleted order.
    for name in ("gcashImages", "gcashReferences", "gcashReceiptDetails"):
        for i in range(0, len(order_ids), 30):
            for d in db.store_doc().collection(name).where(filter=firestore.FieldFilter("order_id", "in", order_ids[i : i + 30])).stream():
                d.reference.delete()
    for cid in ids:
        db.signal_docs().document(cid).delete()
        chats.demo_threads().document(cid).delete()
    db.store_doc().collection("demoUsers").document(user["uid"]).set({"samples": []}, merge=True)
    seeding.seed_demo(user)
    return {"deleted_chats": len(ids), "deleted_orders": len(order_ids)}


def demo_only(conversation_id: str):
    if not demo.is_demo(conversation_id):
        raise HTTPException(404, "Only demo chats take messages written as the customer")


@router.post("/api/conversations/{conversation_id}/customer")
def demo_customer(conversation_id: str, body: chats.Reply, user: dict = Depends(auth.current_user)):
    """In a demo chat, the judge also plays the customer."""
    demo_only(conversation_id)
    return chats.demo_post(conversation_id, False, body.text)


@router.post("/api/conversations/{conversation_id}/customer/image")
async def demo_customer_image(conversation_id: str, request: Request, user: dict = Depends(auth.current_user)):
    demo_only(conversation_id)
    return await chats.demo_image(conversation_id, request, False)


class SampleReceipt(BaseModel):
    kind: Literal[samples.KINDS]


@router.post("/api/conversations/{conversation_id}/customer/sample-receipt")
def demo_sample_receipt(conversation_id: str, body: SampleReceipt, user: dict = Depends(auth.current_user)):
    """A made-up GCash receipt for this chat's waiting GCash request, sent as the customer and dated now."""
    demo_only(conversation_id)
    waiting = []
    for d in db.orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream():
        try:
            m = gcash.current(d.to_dict())
        except rules.Refused:
            continue
        if m["status"] in ("requested", "claimed"):
            waiting.append(m)
    if not waiting:
        raise HTTPException(409, "Ask for GCash payment on this chat's order first.")
    m = max(waiting, key=lambda m: m["requested_at"])
    now, ref = datetime.now(UTC), samples.new_reference()
    amount = m["amount"] - min(10000, m["amount"] // 2) if body.kind == "wrong_amount" else m["amount"]
    name, mobile = (samples.OTHER_NAME, samples.OTHER_MOBILE) if body.kind == "wrong_recipient" else (samples.PAY_TO_NAME, samples.PAY_TO_MOBILE)
    attachment = chats.demo_attachment(samples.receipt_png(amount, ref, now, name, mobile), "image/png")
    if body.kind == "matching":  # only money that reached the demo account appears in the sample history
        sender = samples.sender(chats.demo_doc(conversation_id)["name"])
        db.store_doc().collection("demoUsers").document(user["uid"]).set(
            {"samples": firestore.ArrayUnion([{"reference": ref, "amount": amount, "at": now, "sender": sender}])}, merge=True
        )
    return chats.demo_post(conversation_id, False, "", attachment)


@router.get("/api/demo/sample-statement")
def demo_sample_statement(user: dict = Depends(auth.current_user)):
    """A made-up GCash history export with every matching sample receipt this visitor sent, plus unrelated rows."""
    got = (db.store_doc().collection("demoUsers").document(user["uid"]).get().to_dict() or {}).get("samples", [])
    now = datetime.now(gcash.PHT)
    start = (min([s["at"] for s in got], default=now).astimezone(gcash.PHT) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    pdf = samples.statement_pdf(samples.statement_lines(got, start, now))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="sample-gcash-history.pdf"'})
