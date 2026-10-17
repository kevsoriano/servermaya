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
    paid = []  # payments that reached the demo account, for the sample GCash history
    # One stored packing photo per product, shared by the earlier sample orders.
    shared = {
        product: packing_sample(f"demo-{uid}", catalog.image(name))
        for product, name in (("mug", "packing-mug-blue"), ("tote", "packing-tote-cream"), ("candle", "candle-vanilla"), ("pouch", "pouch-linen"))
    }

    def put_order(ref, order, name, paid_at=None):
        batch.set(ref, order)
        batch.set(ref.collection("versions").document("1"), db.version_doc(order["draft"], "demo_seed", demo.SHOP))
        if (m := order.get("manual_payment")) and m.get("status") == "seller_confirmed":
            paid.append(
                {"reference": m["confirmed"]["reference"], "amount": m["amount"], "at": paid_at or m["requested_at"], "sender": samples.sender(name)}
            )

    def save(cid, chat, orders):
        """A chat with its earlier sample orders first (their messages before the chat's own), then its own orders."""
        refs = [db.orders().document() for _ in demo.HISTORY.get(chat["name"], [])]
        earlier, past = demo.history(uid, cid, chat["name"], now, [r.id for r in refs], shared, pay_to)
        batch.set(chats.demo_threads().document(cid), {**chat, "messages": earlier + chat["messages"], "hint": demo.HINTS.get(chat["name"])})
        for ref, order in zip(refs, past):
            put_order(ref, order, chat["name"])
        for ref, order, paid_at in orders:
            put_order(ref, order, chat["name"], paid_at)

    for t in demo.seed(uid, now):
        save(demo.new_id(), t, [])

    cid = demo.new_id()
    chat, order = demo.returning(uid, cid, now)
    save(cid, chat, [(db.orders().document(), order, None)])

    cid, ref = demo.new_id(), db.orders().document()
    paid_at, reference = now - timedelta(days=10), samples.new_reference()
    receipt = chats.demo_attachment(samples.receipt_png(25000, reference, paid_at, samples.PAY_TO_NAME, samples.PAY_TO_MOBILE), "image/png")
    chat, order = demo.repeat(uid, cid, ref.id, now, receipt, packing_sample(ref.id, catalog.image("packing-tote-cream")), pay_to, reference)
    save(cid, chat, [(ref, order, paid_at)])

    # Her ID starts with the code on the packing label in her videos, so the claim review's label check finds her order's code.
    cid, ref, claim_id = demo.new_id(), db.orders().document(demo.LABEL_CODE.lower() + secrets.token_hex(8)), secrets.token_hex(6)
    cracked, unboxing = catalog.image("damage-mug-blue"), catalog.video("unboxing-labelled-cracked")
    packed = packing_sample(ref.id, catalog.image("packing-mug-blue")), packing_sample(ref.id, catalog.video("packing-labelled"), "video/mp4")
    sent = chats.demo_attachment(cracked, "image/jpeg"), chats.demo_attachment(unboxing, "video/mp4")
    media = [claim_sample(ref.id, claim_id, cracked), claim_sample(ref.id, claim_id, unboxing, "video/mp4")]
    chat, order = demo.damaged(uid, cid, ref.id, now, packed, sent, claim_id, media, pay_to, samples.new_reference())
    save(cid, chat, [(ref, order, None)])

    cid, ref = demo.new_id(), db.orders().document()
    chat, order = demo.to_pack(uid, cid, now, packing_sample(ref.id, catalog.image("packing-tote-cream", blur=8)), pay_to, samples.new_reference())
    save(cid, chat, [(ref, order, None)])

    batch.set(db.store_doc().collection("demoUsers").document(uid), {"samples": firestore.ArrayUnion(paid)}, merge=True)
    batch.commit()


def packing_sample(order_id: str, data: bytes, content_type: str = "image/jpeg") -> dict:
    """A sample packing photo or video, stored like an uploaded one."""
    photo_id = secrets.token_hex(8)
    path = f"stores/{config.STORE}/orders/{order_id}/packing/{photo_id}"
    db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    return {
        "id": photo_id,
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": content_type,
        "size": len(data),
        "uploaded_at": datetime.now(UTC),
        "uploaded_by": demo.SHOP,
        "claimed_captured_at": None,
    }


def claim_sample(order_id: str, claim_id: str, data: bytes, content_type: str = "image/jpeg") -> dict:
    """A sample damage photo or unboxing video, stored like one copied from the customer's chat into a report."""
    sha = hashlib.sha256(data).hexdigest()
    path = f"stores/{config.STORE}/orders/{order_id}/claims/{claim_id}/{sha}"
    db.bucket().blob(path).upload_from_string(data, content_type=content_type, if_generation_match=0)
    return {
        "id": sha[:16],
        "path": path,
        "sha256": sha,
        "content_type": content_type,
        "size": len(data),
        "source": "customer_chat",
        "uploaded_at": datetime.now(UTC),
    }
