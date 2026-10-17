import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from core import auth, config, db
from domain import orders as rules
from domain import packing as pack
from domain import returns
from routes.packing import photo_check

router = APIRouter()


# Returns: the customer sends the item back on an approved claim, and a refund on that claim waits for it (enforced in
# refunds.approved). The seller books nothing.
@router.post("/api/orders/{order_id}/claims/{cid}/return")
def request_return(order_id: str, cid: str, user: dict = Depends(auth.current_user)):
    """Record that the item is asked back. The message to the customer is a separate send the seller makes."""
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: returns.request(o, secrets.token_hex(6), cid, user["email"], now))


class SentIn(BaseModel):
    courier: str = Field(min_length=1, max_length=100)
    tracking: str = Field(default="", max_length=200)


@router.post("/api/orders/{order_id}/returns/{rid}/sent")
def return_sent(order_id: str, rid: str, body: SentIn, user: dict = Depends(auth.current_user)):
    """The customer sent the item back with a courier of their choice; the seller records what they reported. Nothing is booked."""
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: returns.sent(o, rid, body.courier, body.tracking, user["email"], now))


@router.post("/api/orders/{order_id}/returns/{rid}/received")
def receive_return(order_id: str, rid: str, user: dict = Depends(auth.current_user)):
    """The seller has the item in hand. A refund held for it can go ahead."""
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: returns.receive(o, rid, user["email"], now))


@router.post("/api/orders/{order_id}/returns/{rid}/photos")
async def return_photo(order_id: str, rid: str, request: Request, user: dict = Depends(auth.current_user)):
    """A photo of the item that came back. Stored once, never overwritten, with its hash."""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photos must be under 10 MB")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/orders/{order_id}/returns/{rid}/{sha}"
    try:
        await run_in_threadpool(lambda: db.bucket().blob(path).upload_from_string(data, content_type=kind, if_generation_match=0))
    except Exception:
        pass  # the same photo again
    photo = {
        "id": sha[:16],
        "path": path,
        "sha256": sha,
        "content_type": kind,
        "size": len(data),
        "uploaded_at": datetime.now(UTC),
        "uploaded_by": user["email"],
    }
    return await run_in_threadpool(db.change, db.orders().document(order_id), lambda o: returns.add_photo(o, rid, photo))


@router.get("/api/orders/{order_id}/returns/{rid}/photos/{photo_id}")
def return_photo_file(order_id: str, rid: str, photo_id: str, user: dict = Depends(auth.current_user)):
    _, order = db.loaded(order_id)
    try:
        photo = next(p for p in returns.find(order, rid)["photos"] if p["id"] == photo_id)
    except (rules.Refused, StopIteration):
        raise HTTPException(404, "Photo not found")
    return Response(
        db.bucket().blob(photo["path"]).download_as_bytes(), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/api/orders/{order_id}/returns/{rid}/check")
def check_return(order_id: str, rid: str, user: dict = Depends(auth.current_user)):
    """The returned item's photos read against the order's lines, as packing photos are. Code decides each line's result;
    the seller still records what came back."""
    ref, order = db.loaded(order_id)
    try:
        r = returns.find(order, rid)
    except rules.Refused as e:
        raise HTTPException(404, str(e))
    if not r["photos"]:
        raise HTTPException(409, "Add at least one photo of the returned item")
    rows = [
        {
            "line": n,
            "expected": {"name": i["name"], "variant": i["variant"], "quantity": i["quantity"]},
            "reference": (i.get("catalog") or {}).get("photo"),
        }
        for n, i in enumerate(order["draft"]["items"], 1)
    ]
    auth.ai_budget(user)
    photos = [(db.bucket().blob(p["path"]).download_as_bytes(), p["content_type"]) for p in r["photos"]]
    report = photo_check(rows, photos)
    by_line = {s.line: s for s in report.lines}
    checked = [
        {
            "line": row["line"],
            "expected": row["expected"],
            "result": pack.result(row["expected"], by_line.get(row["line"])),
            "damage": (by_line.get(row["line"]).damage if by_line.get(row["line"]) else None),
            "note": (by_line.get(row["line"]).note if by_line.get(row["line"]) else None),
        }
        for row in rows
    ]
    now = datetime.now(UTC)
    return db.change(ref, lambda o: returns.record_check(o, rid, checked, config.GEMINI_MODEL, now))


@router.post("/api/orders/{order_id}/returns/{rid}/inspect")
def inspect_return(order_id: str, rid: str, body: returns.InspectionIn, user: dict = Depends(auth.current_user)):
    now = datetime.now(UTC)
    return db.change(db.orders().document(order_id), lambda o: returns.inspect(o, rid, body, user["email"], now))
