import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from core import config, db
from seed import catalog, demo, samples
from services import chats


def seed_demo(user: dict):
    batch = db.clients()[1].batch()
    now, uid, pay_to = datetime.now(UTC), user["uid"], config.DEMO_GCASH
    for t in demo.seed(uid, now):
        batch.set(chats.demo_threads().document(demo.new_id()), {**t, "hint": demo.HINTS.get(t["name"])})
    paid = []  # payments that reached the demo account, for the sample GCash history

    def save(cid, ref, made, paid_at=None):
        chat, order = made
        batch.set(chats.demo_threads().document(cid), {**chat, "hint": demo.HINTS.get(chat["name"])})
        batch.set(ref, order)
        batch.set(ref.collection("versions").document("1"), db.version_doc(order["draft"], "demo_seed", demo.SHOP))
        if m := order.get("manual_payment"):
            paid.append(
                {
                    "reference": m["confirmed"]["reference"],
                    "amount": m["amount"],
                    "at": paid_at or m["requested_at"],
                    "sender": samples.sender(chat["name"]),
                }
            )

    cid = demo.new_id()
    save(cid, db.orders().document(), demo.returning(uid, cid, now))

    cid, ref = demo.new_id(), db.orders().document()
    paid_at, reference = now - timedelta(days=10), samples.new_reference()
    receipt = chats.demo_attachment(samples.receipt_png(25000, reference, paid_at, samples.PAY_TO_NAME, samples.PAY_TO_MOBILE), "image/png")
    save(
        cid, ref, demo.repeat(uid, cid, ref.id, now, receipt, packing_sample(ref.id, catalog.image("packing-tote-cream")), pay_to, reference), paid_at
    )

    cid, ref, claim_id = demo.new_id(), db.orders().document(), secrets.token_hex(6)
    cracked = catalog.image("damage-mug-blue")
    packed, sent = packing_sample(ref.id, catalog.image("packing-mug-blue")), chats.demo_attachment(cracked, "image/jpeg")
    claim_photo = claim_sample(ref.id, claim_id, cracked)
    save(cid, ref, demo.damaged(uid, cid, ref.id, now, packed, sent, claim_id, claim_photo, pay_to, samples.new_reference()))

    cid, ref = demo.new_id(), db.orders().document()
    save(cid, ref, demo.to_pack(uid, cid, now, packing_sample(ref.id, catalog.image("packing-tote-cream", blur=8)), pay_to, samples.new_reference()))

    batch.set(db.store_doc().collection("demoUsers").document(uid), {"samples": firestore.ArrayUnion(paid)}, merge=True)
    batch.commit()


def packing_sample(order_id: str, data: bytes) -> dict:
    """A sample packing photo (JPEG), stored like an uploaded one."""
    photo_id = secrets.token_hex(8)
    path = f"stores/{config.STORE}/orders/{order_id}/packing/{photo_id}"
    db.bucket().blob(path).upload_from_string(data, content_type="image/jpeg", if_generation_match=0)
    return {
        "id": photo_id,
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "image/jpeg",
        "size": len(data),
        "uploaded_at": datetime.now(UTC),
        "uploaded_by": demo.SHOP,
        "claimed_captured_at": None,
    }


def claim_sample(order_id: str, claim_id: str, data: bytes) -> dict:
    """A sample damage photo (JPEG), stored like one copied from the customer's chat into a report."""
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/orders/{order_id}/claims/{claim_id}/{sha}"
    db.bucket().blob(path).upload_from_string(data, content_type="image/jpeg", if_generation_match=0)
    return {
        "id": sha[:16],
        "path": path,
        "sha256": sha,
        "content_type": "image/jpeg",
        "size": len(data),
        "source": "customer_chat",
        "uploaded_at": datetime.now(UTC),
    }
