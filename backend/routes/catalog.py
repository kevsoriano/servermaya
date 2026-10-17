import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from google.api_core.exceptions import PreconditionFailed

from core import auth, config, db
from domain import catalog as shop
from domain import packing as pack
from services import catalog

router = APIRouter()


# The catalog. Demo visitors see the demo shop's, read-only: editing routes are not in auth.DEMO_ROUTES.
@router.get("/api/catalog")
def list_catalog(user: dict = Depends(auth.current_user)):
    return catalog.products(bool(user.get("demo")))


def product(product_id: str) -> tuple:
    snap = catalog.docs().document(product_id).get()
    if not snap.exists:
        raise HTTPException(404, "Product not found")
    return snap.reference, snap.to_dict()


def save(ref, body: shop.ProductIn, old: dict, user: dict) -> dict:
    """Keep each variant's photo across edits, matched by variant name."""
    photos = {v["name"]: v.get("photo") for v in old.get("variants", [])}
    variants = [
        {**v.model_dump(), "name": (v.name or "").strip() or None, "photo": photos.get((v.name or "").strip() or None)} for v in body.variants
    ]
    doc = {"name": body.name.strip(), "variants": variants, "updated_by": user["email"], "updated_at": datetime.now(UTC)}
    ref.set(doc)
    return {"id": ref.id, **doc}


@router.post("/api/catalog")
def add_product(body: shop.ProductIn, user: dict = Depends(auth.current_user)):
    return save(catalog.docs().document(secrets.token_hex(6)), body, {}, user)


@router.put("/api/catalog/{product_id}")
def edit_product(product_id: str, body: shop.ProductIn, user: dict = Depends(auth.current_user)):
    ref, old = product(product_id)
    return save(ref, body, old, user)


@router.delete("/api/catalog/{product_id}")
def delete_product(product_id: str, user: dict = Depends(auth.current_user)):
    """Orders keep their own snapshot of the catalog line they were drafted with, so deleting a product changes no order."""
    product(product_id)[0].delete()
    return {"deleted": product_id}


@router.post("/api/catalog/{product_id}/photos/{n}")
async def upload_reference(product_id: str, n: int, request: Request, user: dict = Depends(auth.current_user)):
    """A photo of this product and variant, for the packing check to compare with. Photos of the seller's own products only."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in pack.IMAGE_TYPES:
        raise HTTPException(415, "Upload a JPEG, PNG, WebP or HEIC photo")
    data = await request.body()
    if not data or len(data) > pack.MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photos must be under 10 MB")
    ref, old = await run_in_threadpool(product, product_id)
    if not 0 <= n < len(old["variants"]):
        raise HTTPException(404, "Variant not found")
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/catalog/{product_id}/{sha}"
    try:
        await run_in_threadpool(lambda: db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0))
    except PreconditionFailed:
        pass  # the same photo again
    variants = [dict(v) for v in old["variants"]]
    variants[n]["photo"] = {"path": path, "content_type": content_type, "sha256": sha}
    await run_in_threadpool(ref.update, {"variants": variants, "updated_by": user["email"], "updated_at": datetime.now(UTC)})
    return {"id": product_id, **old, "variants": variants}


@router.get("/api/catalog/{product_id}/photos/{n}")
def reference_photo(product_id: str, n: int, user: dict = Depends(auth.current_user)):
    found = next((p for p in catalog.products(bool(user.get("demo"))) if p["id"] == product_id), None)
    if not found or not 0 <= n < len(found["variants"]) or not found["variants"][n].get("photo"):
        raise HTTPException(404, "Photo not found")
    photo = found["variants"][n]["photo"]
    return Response(catalog.photo_bytes(photo), media_type=photo["content_type"], headers={"Cache-Control": "private, max-age=3600"})
