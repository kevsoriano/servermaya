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
        """A chat with its earlier sample orders first (their messages before the chat's own), then its own orders. A chat
        in demo.CHECKS gets its checks stored as already read up to its last message."""
        refs = [db.orders().document() for _ in demo.HISTORY.get(chat["name"], [])]
        earlier, past = demo.history(uid, cid, chat["name"], now, [r.id for r in refs], shared, pay_to)
        messages = earlier + chat["messages"]
        # persona: the AI customer plays this seeded customer only here, never in a judge's own chat with the same name
        batch.set(chats.demo_threads().document(cid), {**chat, "messages": messages, "persona": chat["name"]})
        if chat["name"] in demo.CHECKS:
            found = demo.checks(messages, demo.CHECKS[chat["name"]])
            batch.set(
                db.signal_docs().document(cid),
                {"signals": found, "screened": {}, "latest_message_id": messages[-1]["id"], "model": "demo_seed", "analysed_at": now},
            )
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
    cid, ref = demo.new_id(), db.orders().document(demo.LABEL_CODE.lower() + secrets.token_hex(8))
    cracked, unboxing = catalog.image("damage-mug-blue"), catalog.video("unboxing-labelled-cracked")
    packed = packing_sample(ref.id, catalog.image("packing-mug-blue")), packing_sample(ref.id, catalog.video("packing-labelled"), "video/mp4")
    sent = chats.demo_attachment(cracked, "image/jpeg"), chats.demo_attachment(unboxing, "video/mp4")
    chat, order = demo.damaged(uid, cid, ref.id, now, packed, sent, pay_to, samples.new_reference())
    save(cid, chat, [(ref, order, None)])

    cid, ref = demo.new_id(), db.orders().document()
    chat, order = demo.to_pack(uid, cid, now, packing_sample(ref.id, catalog.image("packing-tote-cream", blur=8)), pay_to, samples.new_reference())
    save(cid, chat, [(ref, order, None)])

    # Chats for Patterns: receipts that did not fit, requests to open an account, a refunded damage report, and a parcel that
    # arrived empty. Each one's earlier orders come from demo.HISTORY.
    def receipt(data: bytes) -> tuple[dict, dict]:
        a = chats.demo_attachment(data, "image/png")
        return a, {"path": a["path"], "sha256": hashlib.sha256(data).hexdigest(), "content_type": "image/png", "size": len(data)}

    for spec in demo.MISFITS:
        cid, ref = demo.new_id(), db.orders().document()
        chat, order = demo.misfit(uid, cid, spec, now, receipt, pay_to)
        save(cid, chat, [(ref, order, None)])
    for name, days, lines in demo.ACCOUNTS:
        save(demo.new_id(), demo.thread(name, lines, uid, now - timedelta(days=days)), [])
    # Lito's thanks comes after his refund (at most 41 hours ago); Carla writes after receiving her parcel (at most 22 hours ago).
    save(demo.new_id(), demo.thread(*demo.LITO, uid, now - timedelta(hours=38)), [])
    chat = demo.thread(*demo.CONTENTS, uid, now - timedelta(hours=2 * len(demo.SEED) + 8))
    chat["messages"][0]["attachments"] = [chats.demo_attachment(catalog.image("contents-empty-box"), "image/jpeg")]
    save(demo.new_id(), chat, [])

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
