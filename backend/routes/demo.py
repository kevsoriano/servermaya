import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.cloud import firestore
from google.genai import types
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import customer_agent as agent
from domain import orders as rules
from domain import receipts as gcash
from seed import demo, samples
from services import catalog, chats, seeding

router = APIRouter()
log = logging.getLogger(__name__)


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
    seeding.reseed(user)
    return {"deleted_chats": len(ids), "deleted_orders": len(order_ids)}


def demo_only(conversation_id: str):
    if not demo.is_demo(conversation_id):
        raise HTTPException(404, "Only demo chats take messages written as the customer")


@router.post("/api/conversations/{conversation_id}/customer")
def demo_customer(conversation_id: str, body: chats.Reply, user: dict = Depends(auth.current_user)):
    """In a demo chat, the judge also plays the customer."""
    demo_only(conversation_id)
    sent = chats.demo_post(conversation_id, False, body.text)
    chats.apply_answers(conversation_id)  # a tap on Confirm or Received takes effect at once
    return sent


@router.post("/api/conversations/{conversation_id}/customer/image")
async def demo_customer_image(conversation_id: str, request: Request, user: dict = Depends(auth.current_user)):
    demo_only(conversation_id)
    return await chats.demo_image(conversation_id, request, False)


def gcash_waiting(conversation_id: str) -> dict | None:
    """The newest GCash request still waiting for the customer's payment in this chat, unless PayMongo already verified one."""
    waiting = []
    for d in db.orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream():
        o = d.to_dict()
        try:
            m = gcash.current(o)
        except rules.Refused:
            continue
        if m["status"] in ("requested", "claimed") and (o.get("payment") or {}).get("status") != "verified":
            waiting.append(m)
    return max(waiting, key=lambda m: m["requested_at"]) if waiting else None


def sample_receipt(conversation_id: str, m: dict, kind: str, user: dict) -> dict:
    """A made-up GCash receipt for the waiting GCash request m, sent as the customer. Dated now, except one paid to someone
    else: that one went to an old number earlier, 2 minutes back but not before receipts count. Otherwise the customer's
    corrected receipt, sent seconds later, shows the same amount and minute, and is rightly flagged as a possible copy."""
    now, ref = datetime.now(UTC), samples.new_reference()
    amount = m["amount"] - min(10000, m["amount"] // 2) if kind == "wrong_amount" else m["amount"]
    name, mobile = (samples.OTHER_NAME, samples.OTHER_MOBILE) if kind == "wrong_recipient" else (samples.PAY_TO_NAME, samples.PAY_TO_MOBILE)
    at = max(gcash.window(m)[0], now - timedelta(minutes=2)) if kind == "wrong_recipient" else now
    attachment = chats.demo_attachment(samples.receipt_png(amount, ref, at, name, mobile), "image/png")
    if kind == "matching":  # only money that reached the demo account appears in the sample history
        sender = samples.sender(chats.demo_doc(conversation_id)["name"])
        db.store_doc().collection("demoUsers").document(user["uid"]).set(
            {"samples": firestore.ArrayUnion([{"reference": ref, "amount": amount, "at": now, "sender": sender}])}, merge=True
        )
    return chats.demo_post(conversation_id, False, "", attachment)


def quick_reply_waiting(conversation_id: str, last: dict) -> str | None:
    """The seller's latest message carries a Confirm or Received button that one of this chat's orders is waiting for."""
    button = (last.get("quick_replies") or [None])[0]
    if not last["from_page"] or not button:
        return None
    for _, o in db.chat_orders(conversation_id):
        if any(((o.get("asked") or {}).get(k) or {}).get("text") == button for k in rules.waiting_for(o)):
            return button
    return None


@router.post("/api/conversations/{conversation_id}/customer/auto")
def demo_customer_turn(conversation_id: str, user: dict = Depends(auth.current_user)):
    """The demo customer answers the seller's latest message, or opens an empty chat. A tap on the seller's button needs no
    Gemini. Otherwise Gemini writes the words, and code decides whether a sample GCash receipt goes with them and which one."""
    demo_only(conversation_id)
    d = chats.demo_doc(conversation_id)
    messages = d["messages"]
    if messages and not messages[-1]["from_page"]:
        return {"action": "none"}  # nothing new from the seller to answer
    if messages and (button := quick_reply_waiting(conversation_id, messages[-1])):
        chats.demo_post(conversation_id, False, button)
        chats.apply_answers(conversation_id)
        return {"action": "tap"}

    p = agent.persona(d.get("persona"))
    # An empty chat a judge made: the customer starts it, asking about something the shop sells (picked here, not by Gemini).
    opening = None if messages else secrets.choice(agent.wants(catalog.products(True)))
    m = gcash_waiting(conversation_id) if messages else None
    sent = agent.receipts_sent(messages, m["requested_at"]) if m else 0
    kind = agent.receipt_kind(p, sent) if m else None
    attaching = bool(kind) and sent == 0  # the first receipt for a request always follows it
    auth.ai_budget(user)
    turn = None
    for attempt in range(2):  # one quiet retry, as for the brief: a Vertex call occasionally fails
        try:
            turn = write_turn(agent.prompt(d["name"], p, messages, attaching, bool(kind), opening))
            break
        except Exception as e:
            log.warning("demo customer attempt %d failed: %r", attempt + 1, e)
    if turn is None and not attaching:
        raise HTTPException(502, f"{d['name']} could not reply right now.")
    # The seller wrote again while Gemini was answering: that message gets its own turn, so this answer is dropped.
    latest = chats.demo_doc(conversation_id)["messages"]
    if [x["id"] for x in latest[-1:]] != [x["id"] for x in messages[-1:]]:
        return {"action": "none"}
    text = (turn.message.strip() if turn else "")[:2000]
    if text:
        chats.demo_post(conversation_id, False, text)
    if kind and (attaching or (turn and turn.send_receipt)):
        sample_receipt(conversation_id, m, kind, user)
        return {"action": "receipt", "kind": kind}
    return {"action": "reply" if text else "none"}


def write_turn(prompt: str) -> agent.Turn:
    r = db.clients()[0].models.generate_content(
        model=config.GEMINI_DEMO_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=agent.SYSTEM,
            response_mime_type="application/json",
            response_schema=agent.Turn,
            temperature=0.7,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        ),
    )
    return agent.Turn.model_validate_json(r.text)


@router.get("/api/demo/sample-statement")
def demo_sample_statement(user: dict = Depends(auth.current_user)):
    """A made-up GCash history export with every matching sample receipt this visitor sent, plus unrelated rows."""
    got = (db.store_doc().collection("demoUsers").document(user["uid"]).get().to_dict() or {}).get("samples", [])
    now = datetime.now(gcash.PHT)
    start = (min([s["at"] for s in got], default=now).astimezone(gcash.PHT) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    pdf = samples.statement_pdf(samples.statement_lines(got, start, now))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="sample-gcash-history.pdf"'})
