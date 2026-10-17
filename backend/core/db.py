from datetime import UTC, datetime
from functools import lru_cache

from fastapi import HTTPException
from google import genai
from google.cloud import firestore, storage

from core import config
from domain import catalog as shop
from domain import custody, delivery, refunds
from domain import orders as rules
from domain import packing as pack


def demo_order(order_id: str) -> bool:
    return ((orders().document(order_id).get().to_dict() or {}).get("source") or {}).get("type") == "demo"


def signal_docs():
    return clients()[1].collection("stores").document(config.STORE).collection("signals")


@lru_cache
def clients():
    """Created on first use so tests and Messenger-only routes need no Google credentials."""
    # 90 s: one Vertex request once hung for over 7 minutes; callers turn the timeout into a 502.
    gemini = genai.Client(vertexai=True, project=config.PROJECT_ID, location="global", http_options={"timeout": 90_000})
    return gemini, firestore.Client(project=config.PROJECT_ID)


def orders():
    return clients()[1].collection("stores").document(config.STORE).collection("orders")


EPOCH = datetime.min.replace(tzinfo=UTC)  # sorts orders saved without created_at last


def chat_orders(conversation_id: str) -> list[tuple[str, dict]]:
    """This chat's orders, newest first."""
    found = [(d.id, d.to_dict()) for d in orders().where(filter=firestore.FieldFilter("source.conversation_id", "==", conversation_id)).stream()]
    return sorted(found, key=lambda x: x[1].get("created_at") or EPOCH, reverse=True)


def version_doc(draft: dict, origin: str, by: str) -> dict:
    return {"draft": draft, "origin": origin, "created_by": by, "created_at": firestore.SERVER_TIMESTAMP}


def present(snap) -> dict:
    # Defaults cover drafts saved before confirmations existed.
    order = {"confirmations": [], "confirm_token": None, **snap.to_dict()}
    try:
        packing, blocked = pack.require_ready(order), None  # the checklist to show, even before the first photo
    except rules.Refused as e:
        packing, blocked = order.get("packing"), str(e)
    try:
        delivery.can_dispatch(order)
        dispatch_blocked = None
    except rules.Refused as e:
        dispatch_blocked = str(e)
    # Each claim's custody check, recomputed from the records on every read: code, never stored as a finding.
    claims = [{**c, "custody": custody.check(order, c)} for c in order.get("claims", [])]
    return {
        **order,
        **({"claims": claims} if claims else {}),
        "id": snap.id,
        "missing": rules.missing(order["draft"]),
        "packing": packing,
        "packing_blocked": blocked,
        "dispatch_blocked": dispatch_blocked,
        "refundable": refunds.refundable(order),
        "catalog_notes": shop.notes(order["draft"]),
    }


def change(ref, rule, by: str = "") -> dict:
    """Apply one of the rules in orders.py inside a transaction, so an edit and a confirmation cannot interleave."""

    @firestore.transactional
    def run(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise HTTPException(404, "Order not found")
        try:
            updates = rule(snap.to_dict())
        except rules.Refused as e:
            raise HTTPException(409, str(e))
        if updates:
            tx.update(ref, updates)
        if "draft" in updates:  # versions are written once and never changed
            tx.set(ref.collection("versions").document(str(updates["version"])), version_doc(updates["draft"], "seller_edit", by))

    run(clients()[1].transaction())
    return present(ref.get())


# Packing. Photos are evidence: stored once, never overwritten, with a SHA-256 of the original bytes.
@lru_cache
def bucket():
    return storage.Client(project=config.PROJECT_ID).bucket(config.EVIDENCE_BUCKET)


def loaded(order_id: str) -> tuple:
    snap = orders().document(order_id).get()
    if not snap.exists:
        raise HTTPException(404, "Order not found")
    return snap.reference, snap.to_dict()


# Direct GCash. Screenshots are claims: code checks them, and only the seller, looking at their own GCash history, confirms.
def store_doc():
    return clients()[1].collection("stores").document(config.STORE)
