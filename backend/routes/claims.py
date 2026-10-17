import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import PreconditionFailed
from google.genai import types
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import claim_agent, custody
from domain import claims as cases
from domain import orders as rules
from domain import packing as pack
from integrations import memory
from seed import catalog
from services import chats, customer_link

router = APIRouter()


# Claims and evidence review.
MAX_OPEN_CLAIMS = 5


def claim_photo(order_id: str, cid: str, data: bytes, content_type: str, source: str) -> dict:
    """A photo or video for a claim, such as the customer's unboxing video. Stored once, never overwritten, with its hash."""
    video = content_type in pack.VIDEO_TYPES
    if content_type not in pack.IMAGE_TYPES and not video:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo, or an MP4, MOV or WebM video")
    if not data or len(data) > (pack.MAX_VIDEO_BYTES if video else pack.MAX_PHOTO_BYTES):
        raise HTTPException(413, "Videos must be under 30 MB" if video else "Photos must be under 10 MB")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/orders/{order_id}/claims/{cid}/{sha}"
    try:
        db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    except PreconditionFailed:
        pass  # the same photo again
    return {
        "id": sha[:16],
        "path": path,
        "sha256": sha,
        "content_type": content_type,
        "size": len(data),
        "source": source,
        "uploaded_at": datetime.now(UTC),
    }


def new_claim(ref, body: cases.ClaimIn, source: str, by: str, cid: str | None = None, photos: list[dict] = (), message_ids: list[str] = ()) -> dict:
    now, cid = datetime.now(UTC), cid or secrets.token_hex(6)

    def rule(o: dict) -> dict:
        if sum(c["status"] != "resolved" for c in o.get("claims", [])) >= MAX_OPEN_CLAIMS:
            raise rules.Refused("There are already several open reports on this order")
        return cases.open_claim(o, cid, body, source, by, now, photos, message_ids)

    return db.change(ref, rule)


@router.post("/api/public/orders/{token}/claims")
def public_claim(token: str, body: cases.ClaimIn):
    return customer_link.customer_view(new_claim(customer_link.by_token(token).reference, body, "customer_link", "customer"))


@router.post("/api/public/orders/{token}/claims/{cid}/photos")
async def public_claim_photo(token: str, cid: str, request: Request):
    snap = await run_in_threadpool(customer_link.by_token, token)
    data = await request.body()
    photo = await run_in_threadpool(
        claim_photo, snap.id, cid, data, request.headers.get("content-type", "").split(";")[0].strip().lower(), "customer"
    )
    now = datetime.now(UTC)
    return customer_link.customer_view(
        await run_in_threadpool(db.change, snap.reference, lambda o: cases.add_photos(o, cid, [photo], "customer", now))
    )


@router.post("/api/orders/{order_id}/claims")
def seller_claim(order_id: str, body: cases.ClaimIn, user: dict = Depends(auth.current_user)):
    return new_claim(db.orders().document(order_id), body, "seller", user["email"])


@router.post("/api/orders/{order_id}/claims/{cid}/photos")
async def seller_claim_photo(order_id: str, cid: str, request: Request, user: dict = Depends(auth.current_user)):
    data = await request.body()
    photo = await run_in_threadpool(claim_photo, order_id, cid, data, request.headers.get("content-type", "").split(";")[0].strip().lower(), "seller")
    now = datetime.now(UTC)
    return await run_in_threadpool(db.change, db.orders().document(order_id), lambda o: cases.add_photos(o, cid, [photo], user["email"], now))


class ChatClaimIn(BaseModel):
    type: cases.ClaimType
    message_ids: list[str] = Field(min_length=1, max_length=10)


@router.post("/api/orders/{order_id}/claims/from-chat")
def chat_claim(order_id: str, body: ChatClaimIn, user: dict = Depends(auth.current_user)):
    """A claim the customer made in the chat, opened by the seller from the messages they pick. Its text is the customer's
    own words and its photos are the images in those messages, so the review sees what the customer sent, not a retelling."""
    ref, order = db.loaded(order_id)
    if (order.get("source") or {}).get("type") not in ("messenger", "demo"):
        raise HTTPException(409, "This order did not come from a chat")
    picked = [m for m in chats.thread_messages(order["source"]["conversation_id"]) if m["id"] in body.message_ids and not m["from_page"]]
    if not picked:
        raise HTTPException(422, "Pick the customer's messages about the problem")
    media = [a for m in picked for a in m.get("attachments") or [] if a["kind"] in ("image", "video") and a.get("name") != "sticker"]
    if sum(a["kind"] == "image" for a in media) > cases.MAX_PHOTOS or sum(a["kind"] == "video" for a in media) > cases.MAX_VIDEOS:
        raise HTTPException(422, f"Up to {cases.MAX_PHOTOS} photos and {cases.MAX_VIDEOS} videos per report: pick fewer messages")
    cid = secrets.token_hex(6)
    photos = [claim_photo(order_id, cid, *chats.attachment_bytes(a), "customer_chat") for a in media]
    narrative = "\n".join(m["text"].strip() for m in picked if m.get("text", "").strip())[:2000] or "Sent photos only, no text."
    return new_claim(ref, cases.ClaimIn(type=body.type, narrative=narrative), "customer_chat", user["email"], cid, photos, [m["id"] for m in picked])


@router.get("/api/orders/{order_id}/claims/{cid}/photos/{photo_id}")
def claim_photo_file(order_id: str, cid: str, photo_id: str, user: dict = Depends(auth.current_user)):
    _, order = db.loaded(order_id)
    try:
        photo = next(p for p in cases.claim(order, cid)["photos"] if p["id"] == photo_id)
    except (rules.Refused, StopIteration):
        raise HTTPException(404, "Photo not found")
    return Response(
        db.bucket().blob(photo["path"]).download_as_bytes(), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/api/orders/{order_id}/claims/{cid}/review")
def review_claim(order_id: str, cid: str, user: dict = Depends(auth.current_user)):
    """An ADK agent compares the claim with the order's records. Citations are checked in code before anything is shown."""
    ref, order = db.loaded(order_id)
    try:
        c = cases.claim(order, cid)
    except rules.Refused as e:
        raise HTTPException(404, str(e))
    messages = order.get("messages", [])
    if (order.get("source") or {}).get("type") in ("messenger", "demo"):
        try:
            messages = chats.thread_messages(order["source"]["conversation_id"])
        except HTTPException:
            pass  # the snapshot taken when the order was drafted
    blocks, images = cases.evidence(order, messages, c)
    shown, shas = [], {label: photo.get("sha256") for label, photo in images}
    for label, photo in images:
        if photo["content_type"].startswith("video/"):  # Gemini reads a video from the bucket, so its size never limits the request
            shown.append((label, types.Part.from_uri(file_uri=f"gs://{config.EVIDENCE_BUCKET}/{photo['path']}", mime_type=photo["content_type"])))
            continue
        try:
            data = catalog.image(photo["sample"]) if photo.get("sample") else db.bucket().blob(photo["path"]).download_as_bytes()
            shas[label] = shas.get(label) or hashlib.sha256(data).hexdigest()
            shown.append((label, types.Part.from_bytes(data=data, mime_type=photo["content_type"])))
        except Exception:
            pass  # an image that cannot be loaded cannot be cited
    checked = sum(label[:2] in ("CV", "KV") or label in cases.PROOFS for label, _ in shown)
    # the review (its tool turns count as one, like any other read) and one check per video or courier photo; a kept check still counts
    auth.ai_budget(user, 1 + checked)
    expect = (rules.code(order_id), order["draft"]["recipient_name"]["value"])
    video_checks: dict = {}
    blocks += cases.video_records(db.clients()[0], config.GEMINI_MODEL, shown, expect, shas, db.store_doc().collection("videoChecks"), video_checks)
    proofs = [(label, part, shas.get(label)) for label, part in shown if label in cases.PROOFS]
    texts, parcel_checks = cases.parcel_records(db.clients()[0], config.GEMINI_MODEL, proofs, expect, db.store_doc().collection("photoChecks"))
    blocks += texts
    # The custody check reads the checks just made, so the review can cite where the chain first broke.
    current = {**order, **cases.with_checks(order, cid, video_checks, parcel_checks)}
    blocks.append(custody.record(custody.check(current, cases.claim(current, cid))))
    now = datetime.now(UTC)
    try:
        review = claim_agent.review(
            claim_agent.gemini(config.GEMINI_MODEL, config.PROJECT_ID),
            config.GEMINI_MODEL,
            blocks,
            cases.photos_only(shown),
            now,
            recall=lambda: memory.recall(order, cases.recall_query(order, c)),
        )
    except Exception:
        raise HTTPException(502, "Could not review this claim. Try again.")
    # The text behind each citation, so the seller can check it without hunting.
    cited = {e for f in review["findings"] for e in f["evidence"]} | set(review["recommendation"]["evidence"])
    texts = {b[1 : b.index("]")]: b for b in blocks} | {label: f"[{label}] {claim_agent.kind(p)}" for label, p in shown} | review.pop("recalled")
    review["sources"] = {k: v for k, v in texts.items() if k in cited}
    return db.change(ref, lambda o: cases.record_review(o, cid, review, user["email"], now, video_checks, parcel_checks))


@router.post("/api/orders/{order_id}/claims/{cid}/wait")
def wait_claim(order_id: str, cid: str, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: cases.wait_for_evidence(o, cid, user["email"], now))


@router.post("/api/orders/{order_id}/claims/{cid}/decide")
def decide_claim(order_id: str, cid: str, body: cases.DecisionIn, tasks: BackgroundTasks, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    suggested = lambda o: custody.check(o, cases.claim(o, cid))["suggested_cause"]
    order = db.change(db.orders().document(order_id), lambda o: cases.decide(o, cid, body, user["email"], now, suggested(o)))
    # After the response: the shop's memory of how it decides claims, for later reviews (see claims.decision_fact).
    tasks.add_task(memory.remember, order, cases.decision_fact(order, cases.claim(order, cid)))
    return order


class CauseIn(BaseModel):
    cause: cases.Cause
    note: str = Field(default="", max_length=500)


@router.post("/api/orders/{order_id}/claims/{cid}/cause")
def claim_cause(order_id: str, cid: str, body: CauseIn, user: dict = Depends(auth.current_user)):
    """The seller changes the recorded cause, for example after the returned item is inspected. The earlier cause stays on record."""
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: cases.set_cause(o, cid, body.cause, body.note, user["email"], now))


class EscalateIn(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    next_action: str = Field(min_length=1, max_length=500)


@router.post("/api/orders/{order_id}/claims/{cid}/escalate")
def escalate_claim(order_id: str, cid: str, body: EscalateIn, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: cases.escalate(o, cid, body.owner, body.next_action, user["email"], now))


@router.post("/api/orders/{order_id}/claims/{cid}/replacement")
def replace_claim(order_id: str, cid: str, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: cases.start_replacement(o, cid, user["email"], now))


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/api/orders/{order_id}/claims/{cid}/resolve")
def resolve_claim(order_id: str, cid: str, body: ResolveIn, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: cases.resolve(o, cid, body.note, user["email"], now))
