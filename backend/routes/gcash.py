import hashlib
import io
import re
from datetime import UTC, datetime

import pypdf
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import AlreadyExists, PreconditionFailed
from google.cloud import firestore
from google.genai import types
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import orders as rules
from domain import packing as pack
from domain import receipts as gcash
from seed import samples
from services import chats

router = APIRouter()


@router.get("/api/settings/gcash")
def get_gcash(user: dict = Depends(auth.current_user)):
    if user.get("demo"):
        return {**config.DEMO_GCASH, "qr_image": True}  # the sample QR, stamped as one
    store = db.store_doc().get().to_dict() or {}
    return store.get("gcash") and {**store["gcash"], "qr_image": bool(store.get("gcash_qr_image"))}


QR_TYPES = {"image/jpeg", "image/png"}  # what Messenger shows as an image, and what GCash saves


def qr_image(user: dict) -> tuple[bytes, str] | None:
    """The QR image customers are sent: the demo shop's sample, or the seller's own screenshot if saved."""
    if user.get("demo"):
        return samples_qr(), "image/jpeg"
    saved = (db.store_doc().get().to_dict() or {}).get("gcash_qr_image")
    return (db.bucket().blob(saved["path"]).download_as_bytes(), saved["content_type"]) if saved else None


def samples_qr() -> bytes:
    from seed import catalog

    return catalog.image("gcash-qr-sample")


@router.post("/api/settings/gcash/qr")
async def put_gcash_qr(request: Request, user: dict = Depends(auth.current_user)):
    """The seller's own GCash QR screenshot, sent to customers with the GCash instructions. The browser reads the QR and
    the settings check it against the registered name first; this keeps the picture."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in QR_TYPES:
        raise HTTPException(415, "Upload the QR as a PNG or JPEG screenshot")
    data = await request.body()
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "The QR image must be under 5 MB")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/gcash/qr/{sha}"
    try:
        await run_in_threadpool(lambda: db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    except PreconditionFailed:
        pass  # the same screenshot again
    saved = {"path": path, "sha256": sha, "content_type": content_type, "size": len(data), "by": user["email"], "at": datetime.now(UTC)}
    await run_in_threadpool(lambda: db.store_doc().set({"gcash_qr_image": saved}, merge=True))
    return {"qr_image": True}


@router.get("/api/settings/gcash/qr")
def gcash_qr(user: dict = Depends(auth.current_user)):
    found = qr_image(user)
    if not found:
        raise HTTPException(404, "No QR saved")
    return Response(found[0], media_type=found[1], headers={"Cache-Control": "private, max-age=300"})


@router.put("/api/settings/gcash")
def put_gcash(body: gcash.Settings, user: dict = Depends(auth.current_user)):
    try:
        found = gcash.settings(body)
    except rules.Refused as e:
        raise HTTPException(422, str(e))
    saved = {**found, "updated_by": user["email"], "updated_at": datetime.now(UTC)}
    db.store_doc().set({"gcash": saved}, merge=True)
    return saved


class GcashRequest(BaseModel):
    says_paid: bool = False  # the customer says they already paid: check that payment instead of asking for one


@router.post("/api/orders/{order_id}/gcash/request")
def request_gcash(order_id: str, body: GcashRequest | None = None, user: dict = Depends(auth.current_user)):
    pay_to = config.DEMO_GCASH if db.demo_order(order_id) else get_gcash(user)
    if not pay_to:
        raise HTTPException(409, "Add your GCash name and number in Settings first")
    now = datetime.now(UTC)
    qr = pay_to.get("qr") or {}
    snapshot = {"name": pay_to["name"], "mobile": pay_to["mobile"], "masked_name": qr.get("masked_name"), "user_id": qr.get("user_id")}
    says_paid = bool(body and body.says_paid)
    ref, order = db.loaded(order_id)
    rule = lambda o: gcash.request(o, snapshot, user["email"], now, says_paid)
    source = order.get("source") or {}
    if source.get("type") in ("messenger", "demo"):
        # The customer gets the instructions in the chat with this one click; checked first, recorded only once sent.
        try:
            rule(order)
        except rules.Refused as e:
            raise HTTPException(409, str(e))
        chats.send(source["conversation_id"], gcash_message(order, pay_to, says_paid), user, order_id)
        # the QR follows the instructions; the demo's is the stamped sample, never a real account
        qr = None if says_paid else qr_image({"demo": source["type"] == "demo"})
        if qr:
            chats.send_image(source["conversation_id"], *qr, user, order_id)
    return db.change(ref, rule)


def gcash_message(order: dict, pay_to: dict, says_paid: bool) -> str:
    if says_paid:
        return "Thank you! Please send a screenshot of your GCash receipt here so we can match it with our GCash history."
    m = re.sub(r"\D", "", pay_to["mobile"])
    local = f"0{m[2:5]} {m[5:8]} {m[8:]}" if m.startswith("63") and len(m) == 12 else pay_to["mobile"]
    return (
        f"To pay by GCash, send {rules.peso(order['draft']['total'])} to {pay_to['name']}, {local} (GCash > Send Money), "
        "or scan the QR below. "
        "Then send a screenshot of your receipt here, and we'll confirm once we find the payment in our GCash history."
    )


# ponytail: written before the claim is saved, so a failed read leaves an entry pointing at an order with no claim, and a
# legitimate reuse on another order is then flagged (fails safe: the seller gives a reason). Upgrade: when the entry
# points elsewhere, check that order really has the claim and take the entry over if not.
def first_seen(collection: str, key: str, order_id: str, **extra) -> dict:
    """Index a reference number, image hash or receipt details across orders. Returns whichever record got there first."""
    doc = db.store_doc().collection(collection).document(key)
    record = {"order_id": order_id, **extra}
    try:
        doc.create({**record, "at": firestore.SERVER_TIMESTAMP})
        return record
    except AlreadyExists:
        return doc.get().to_dict()


MAX_RECEIPTS_PER_READ = 5


def transcribe(data: bytes, content_type: str, system: str, schema: type[BaseModel]) -> BaseModel:
    try:
        response = db.clients()[0].models.generate_content(
            model=config.GEMINI_MODEL,
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
        data, content_type = db.bucket().blob(attachment["path"]).download_as_bytes(), attachment["mime_type"]
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
    path = f"stores/{config.STORE}/orders/{order_id}/gcash/{sha}"
    try:
        db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
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
        "model": config.GEMINI_MODEL,
    }


@router.post("/api/orders/{order_id}/gcash/read")
def read_gcash_receipts(order_id: str, user: dict = Depends(auth.current_user)):
    """Check every customer image sent inside the GCash request's window that has not been checked yet."""
    ref, order = db.loaded(order_id)
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    if order.get("source", {}).get("type") not in ("messenger", "demo"):
        raise HTTPException(409, "This order did not come from a chat")
    known = {c["message_id"] for c in manual["claims"]}
    images = [
        (m, a)
        for m in chats.thread_messages(order["source"]["conversation_id"])
        if not m["from_page"] and m["id"] not in known and datetime.fromisoformat(m["created_time"]) >= gcash.window(manual)[0] - gcash.SLACK
        for a in m["attachments"][:1]
        if a["kind"] == "image" and a["name"] != "sticker"
    ][-MAX_RECEIPTS_PER_READ:]
    if not images:
        raise HTTPException(409, "No new images from the customer since you asked for GCash payment")
    auth.ai_budget(user, len(images))
    now = datetime.now(UTC)
    claims = [read_receipt(order_id, manual, m, a, now, index_scope(order)) for m, a in images]
    return db.change(ref, lambda o: gcash.add_claims(o, claims))


@router.get("/api/orders/{order_id}/gcash/claims/{claim_id}/image")
def gcash_screenshot(order_id: str, claim_id: str, user: dict = Depends(auth.current_user)):
    """The stored copy of a receipt screenshot, so the seller can compare it with the checks by eye."""
    _, order = db.loaded(order_id)
    requests_ = [order.get("manual_payment") or {}] + order.get("manual_payment_history", [])
    claim = next((c for r in requests_ for c in r.get("claims", []) if c["id"] == claim_id), None)
    if not claim:
        raise HTTPException(404, "Screenshot not found")
    image = claim["image"]
    return Response(
        db.bucket().blob(image["path"]).download_as_bytes(), media_type=image["content_type"], headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/api/orders/{order_id}/gcash/history")
async def read_gcash_history(order_id: str, request: Request, user: dict = Depends(auth.current_user)):
    """The seller's screenshot of the payment in their own GCash history. Read to fill in the reference; it confirms nothing."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC screenshot")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Screenshots must be under 10 MB")
    return await run_in_threadpool(history_read, order_id, data, content_type, user)


@router.post("/api/orders/{order_id}/gcash/sample-history")
def demo_sample_history(order_id: str, user: dict = Depends(auth.current_user)):
    """Demo only: the seller's GCash history entry for the newest matching sample receipt that fits this request, read like an upload."""
    ref, order = db.loaded(order_id)
    if (order.get("source") or {}).get("type") != "demo":
        raise HTTPException(404, "Sample history screenshots are for demo orders only")
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    start = gcash.window(manual)[0] - gcash.SLACK
    got = (db.store_doc().collection("demoUsers").document(user["uid"]).get().to_dict() or {}).get("samples", [])
    fits = [s for s in got if s["amount"] == manual["amount"] and s["at"] >= start]
    if not fits:
        raise HTTPException(409, "No payment from this customer has reached the demo account yet. Only money that reached it is in its history.")
    s = max(fits, key=lambda s: s["at"])
    return history_read(order_id, samples.history_png(s["amount"], s["reference"], s["at"], s["sender"]), "image/png", user)


def history_read(order_id: str, data: bytes, content_type: str, user: dict) -> dict:
    ref, order = db.loaded(order_id)
    try:
        manual = gcash.current(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))
    auth.ai_budget(user)
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/orders/{order_id}/gcash/history/{sha}"
    try:
        db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same file is already stored
    entry = transcribe(data, content_type, gcash.HISTORY_SYSTEM, gcash.HistoryEntry)
    read = {
        "image": {"path": path, "sha256": sha, "content_type": content_type, "size": len(data)},
        "entry": entry.model_dump(),
        "reference": gcash.reference(entry.reference) or None,
        "checks": gcash.check_history(entry, manual["amount"], *gcash.window(manual)),
        "read_at": datetime.now(UTC),
        "read_by": user["email"],
        "model": config.GEMINI_MODEL,
    }
    return db.change(ref, lambda o: gcash.add_history(o, read))


MAX_STATEMENT_BYTES = 5 * 1024 * 1024


def statement_text(data: bytes, password: str) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password):
            raise HTTPException(422, "Wrong password. GCash uses your last name and the last 4 digits of your mobile number, like delacruz1234.")
        return "\n".join(page.extract_text() for page in reader.pages)
    except pypdf.errors.PyPdfError:
        raise HTTPException(422, "Could not open this PDF")


@router.post("/api/gcash/statements")
async def upload_gcash_statement(request: Request, x_pdf_password: str = Header(default=""), user: dict = Depends(auth.current_user)):
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
    now = datetime.now(UTC)
    db.store_doc().collection("gcashStatements").document(statement_id).set({**statement, "uploaded_by": by, "uploaded_at": now})
    waiting = [
        d
        for d in db.orders().where(filter=firestore.FieldFilter("manual_payment.status", "in", ["requested", "claimed", "seller_confirmed"])).stream()
        if auth.visible(d.to_dict(), user)
    ]
    taken = {m["confirmed"]["reference"]: d.id for d in waiting if (m := d.to_dict()["manual_payment"]).get("confirmed")}
    result = {"start": statement["start"], "last_at": statement["last_at"], "received": len(statement["credits"])}
    for d in waiting:
        others = {ref for ref, order_id in taken.items() if order_id != d.id}
        # check outside the transaction first, so unchanged orders cost no write
        if not gcash.reconcile(d.to_dict(), statement, statement_id, others, by, now):
            continue
        saved = db.change(d.reference, lambda o, others=others: gcash.reconcile(o, statement, statement_id, others, by, now))
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


@router.post("/api/orders/{order_id}/gcash/confirm")
def confirm_gcash(order_id: str, body: GcashConfirm, user: dict = Depends(auth.current_user)):
    if not body.found_in_history:
        raise HTTPException(422, "Only confirm a reference you found in your own GCash transaction history")
    ref = gcash.reference(body.reference)
    _, order = db.loaded(order_id)
    others = db.orders().where(filter=firestore.FieldFilter("manual_payment.confirmed.reference", "==", ref)).stream()
    elsewhere = any(d.id != order_id and index_scope(d.to_dict()) == index_scope(order) for d in others)
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: gcash.confirm(o, ref, user["email"], body.reason, now, elsewhere, body.invno))
