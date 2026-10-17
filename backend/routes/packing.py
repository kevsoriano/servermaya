import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.genai import types
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import extraction
from domain import orders as rules
from domain import packing as pack
from services import catalog

router = APIRouter()


def ready(order: dict) -> dict:
    try:
        return pack.require_ready(order)
    except rules.Refused as e:
        raise HTTPException(409, str(e))


@router.post("/api/orders/{order_id}/packing/photos")
async def upload_packing_photo(order_id: str, request: Request, user: dict = Depends(auth.current_user)):
    """The body is the photo or video itself. X-Claimed-Captured-At is what the device says; it proves nothing and is stored apart
    from upload time. A video is kept as evidence of what was packed and sealed, for a later claim."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    video = content_type in pack.VIDEO_TYPES
    if content_type not in pack.IMAGE_TYPES and not video:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo, or an MP4, MOV or WebM video")
    data = await request.body()
    if not data or len(data) > (pack.MAX_VIDEO_BYTES if video else pack.MAX_PHOTO_BYTES):
        raise HTTPException(413, "Videos must be under 30 MB" if video else "Photos must be under 10 MB")
    ref, order = await run_in_threadpool(db.loaded, order_id)
    ready(order)
    photo_id = secrets.token_hex(8)
    path = f"stores/{config.STORE}/orders/{order_id}/packing/{photo_id}"
    # if_generation_match=0: refuse to overwrite an existing object
    await run_in_threadpool(lambda: db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    now = datetime.now(UTC)
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
    return await run_in_threadpool(db.change, ref, lambda o: (pack.add_video if video else pack.add_photo)(o, photo))


@router.get("/api/orders/{order_id}/packing/photos/{photo_id}")
def packing_photo(order_id: str, photo_id: str, user: dict = Depends(auth.current_user)):
    _, order = db.loaded(order_id)
    sessions = [order.get("packing") or {}] + order.get("packing_history", [])
    photo = next((p for s in sessions for p in s.get("photos", []) + s.get("videos", []) if p["id"] == photo_id), None)
    if not photo:
        raise HTTPException(404, "Photo not found")
    return Response(
        db.bucket().blob(photo["path"]).download_as_bytes(), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/api/orders/{order_id}/packing/check")
def check_packing(order_id: str, user: dict = Depends(auth.current_user)):
    ref, order = db.loaded(order_id)
    packing = ready(order)
    if not packing["photos"]:
        raise HTTPException(409, "Add at least one packing photo")
    checklist = "\n".join(
        f"[{r['line']}] {r['expected']['quantity']} x {extraction.inline(r['expected']['name'])}"
        + (f" ({extraction.inline(r['expected']['variant'])})" if r["expected"]["variant"] else "")
        for r in packing["rows"]
    )
    auth.ai_budget(user)
    photos = [db.bucket().blob(p["path"]).download_as_bytes() for p in packing["photos"]]
    contents = [f"<order>\n{checklist}\n</order>"]
    for r in packing["rows"]:
        if r.get("reference"):
            try:
                image = catalog.photo_bytes(r["reference"])
            except Exception:
                continue  # a missing reference only means the check works from the item's name, as without a catalog
            contents += [f"Reference for line {r['line']}:", types.Part.from_bytes(data=image, mime_type=r["reference"]["content_type"])]
    for n, (p, data) in enumerate(zip(packing["photos"], photos), 1):
        contents += [f"Photo {n}:", types.Part.from_bytes(data=data, mime_type=p["content_type"])]
    try:
        response = db.clients()[0].models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config={"system_instruction": pack.SYSTEM, "response_mime_type": "application/json", "response_schema": pack.Report, "temperature": 0},
        )
        report = pack.Report.model_validate_json(response.text)
    except Exception:
        raise HTTPException(502, "Could not check the photos. Try again.")
    now = datetime.now(UTC)
    too_unclear = pack.unclear(photos)
    return db.change(ref, lambda o: pack.apply_check(o, report, config.GEMINI_MODEL, now, too_unclear))


class Inspection(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/api/orders/{order_id}/packing/lines/{line}/confirm")
def confirm_packing_line(order_id: str, line: int, body: Inspection, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: pack.confirm_row(o, line, user["email"], body.reason, now))


@router.post("/api/orders/{order_id}/packing/complete")
def complete_packing(order_id: str, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: pack.complete(o, user["email"], now))
