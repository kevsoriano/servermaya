import hashlib
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from google.cloud import firestore
from pydantic import BaseModel, Field

from core import auth, db
from domain import patterns
from seed import demo
from services import chats

router = APIRouter()


# Patterns. Recomputed on every load from the store's own orders.
def visible_orders(user: dict) -> list[dict]:
    # A demo visitor's own orders only, by query, as the Orders list does: every visitor adds about 30 seeded orders.
    # ponytail: a seller reads every order on each load; keep a rolling aggregate when a store has thousands of orders
    docs = db.orders().where(filter=firestore.FieldFilter("source.owner", "==", user["uid"])) if user.get("demo") else db.orders()
    return [{**o, "id": d.id} for d in docs.stream() if auth.visible(o := d.to_dict(), user)]


def chat_checks(user: dict) -> list[dict]:
    """The stored checks of the chats this user can see: a demo visitor's own demo chats, or the store's Messenger chats."""
    if user.get("demo"):
        threads = chats.demo_threads().where(filter=firestore.FieldFilter("owner", "==", user["uid"])).stream()
        names = {d.id: d.get("name") for d in threads}
        docs = [db.signal_docs().document(i).get() for i in names]
    else:
        names, docs = {}, [d for d in db.signal_docs().stream() if not demo.is_demo(d.id)]
    return [{"conversation_id": d.id, "name": names.get(d.id), "signals": (d.to_dict() or {}).get("signals", [])} for d in docs if d.exists]


def pattern_cards(days: int, user: dict, rising: bool = True) -> list[dict]:
    found = visible_orders(user)
    reviews = {r["key"]: r for r in (d.to_dict() for d in db.store_doc().collection("patternReviews").stream())}
    checks = chat_checks(user) if rising else []
    cards = patterns.analyse(found, datetime.now(UTC), days, checks)
    return patterns.with_review([c for c in cards if rising or c["rule"] != "rising"], reviews)


@router.get("/api/patterns")
def list_patterns(days: int = 30, user: dict = Depends(auth.current_user)):
    return pattern_cards(max(1, min(days, 365)), user)


@router.get("/api/patterns/returns")
def returns_by_cause(days: int = 30, user: dict = Depends(auth.current_user)):
    return patterns.returns_summary(visible_orders(user), datetime.now(UTC), max(1, min(days, 365)))


@router.get("/api/orders/{order_id}/patterns")
def order_patterns(order_id: str, user: dict = Depends(auth.current_user)):
    return [c for c in pattern_cards(30, user, rising=False) if any(i.get("order_id") == order_id for i in c["incidents"])]


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
