import hashlib
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core import auth, db
from domain import patterns
from seed import demo

router = APIRouter()


# Patterns. Recomputed on every load from the store's own orders.
def visible_orders(user: dict) -> list[dict]:
    """The orders patterns are counted from. A demo visitor's include the sample delivery history (never stored)."""
    # ponytail: reads every order on each load; keep a rolling aggregate when a store has thousands of orders
    found = [{**o, "id": d.id} for d in db.orders().stream() if auth.visible(o := d.to_dict(), user)]
    return found + (demo.sample_history(datetime.now(UTC)) if user.get("demo") else [])


def pattern_cards(days: int, user: dict) -> list[dict]:
    found = visible_orders(user)
    reviews = {r["key"]: r for r in (d.to_dict() for d in db.store_doc().collection("patternReviews").stream())}
    return patterns.with_review(patterns.analyse(found, datetime.now(UTC), days), reviews)


@router.get("/api/patterns")
def list_patterns(days: int = 30, user: dict = Depends(auth.current_user)):
    return pattern_cards(max(1, min(days, 365)), user)


@router.get("/api/patterns/returns")
def returns_by_cause(days: int = 30, user: dict = Depends(auth.current_user)):
    found = visible_orders(user)
    summary = patterns.returns_summary(found, datetime.now(UTC), max(1, min(days, 365)))
    return {**summary, "samples": sum(1 for o in found if o.get("sample"))}


@router.get("/api/orders/{order_id}/patterns")
def order_patterns(order_id: str, user: dict = Depends(auth.current_user)):
    return [c for c in pattern_cards(30, user) if any(i["order_id"] == order_id for i in c["incidents"])]


class PatternReviewIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    incidents_hash: str = Field(min_length=1, max_length=64)
    status: Literal["reviewing", "explained_operationally", "unresolved", "dismissed"]
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/api/patterns/review")
def review_pattern(body: PatternReviewIn, user: dict = Depends(auth.current_user)):
    """The seller's review of one card, tied to the cases it covered. New cases make the card new again."""
    record = {**body.model_dump(), "reason": body.reason.strip(), "by": user["email"], "at": datetime.now(UTC)}
    db.store_doc().collection("patternReviews").document(hashlib.sha256(body.key.encode()).hexdigest()).set(record)
    return record
