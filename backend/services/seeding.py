import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from core import config, db
from seed import demo, samples
from services import chats


def seed_demo(user: dict):
    batch = db.clients()[1].batch()
    now = datetime.now(UTC)
    for t in demo.seed(user["uid"], now):
        batch.set(chats.demo_threads().document(demo.new_id()), t)
    cid = demo.new_id()
    chat, order = demo.returning(user["uid"], cid, now)
    batch.set(chats.demo_threads().document(cid), chat)
    ref = db.orders().document()
    batch.set(ref, order)
    batch.set(ref.collection("versions").document("1"), db.version_doc(order["draft"], "demo_seed", demo.SHOP))
    rid, ref = demo.new_id(), db.orders().document()
    paid_at, reference = now - timedelta(days=10), samples.new_reference()
    receipt = chats.demo_attachment(samples.receipt_png(25000, reference, paid_at, samples.PAY_TO_NAME, samples.PAY_TO_MOBILE), "image/png")
    chat, order = demo.repeat(user["uid"], rid, ref.id, now, receipt, packing_sample(ref.id), config.DEMO_GCASH, reference)
    batch.set(chats.demo_threads().document(rid), chat)
    batch.set(ref, order)
    batch.set(ref.collection("versions").document("1"), db.version_doc(order["draft"], "demo_seed", demo.SHOP))
    # His payment reached the demo account, so it is in the sample GCash history like any matching sample receipt.
    batch.set(
        db.store_doc().collection("demoUsers").document(user["uid"]),
        {"samples": firestore.ArrayUnion([{"reference": reference, "amount": 25000, "at": paid_at, "sender": samples.sender(chat["name"])}])},
        merge=True,
    )
    batch.commit()


def packing_sample(order_id: str) -> dict:
    """The drawn packing photo, stored like an uploaded one."""
    data, photo_id = samples.packing_png(), secrets.token_hex(8)
    path = f"stores/{config.STORE}/orders/{order_id}/packing/{photo_id}"
    db.bucket().blob(path).upload_from_string(data, content_type="image/png", if_generation_match=0)
    return {
        "id": photo_id,
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": "image/png",
        "size": len(data),
        "uploaded_at": datetime.now(UTC),
        "uploaded_by": demo.SHOP,
        "claimed_captured_at": None,
    }
