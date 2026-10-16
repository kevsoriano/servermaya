from datetime import UTC, timedelta

import pytest

import main
from core import auth
from helpers import client
from integrations import messenger
from services import chats


def test_demo_chats_parse_seed_and_never_reach_messenger(monkeypatch):
    from datetime import datetime

    from seed import demo

    assert demo.parse("Customer: hi\n  2 pcs po\n\nseller: ₱180 each") == [(False, "hi\n2 pcs po"), (True, "₱180 each")]
    assert demo.parse("") == []
    for bad in ("hello", "Customer:"):
        with pytest.raises(ValueError):
            demo.parse(bad)
    now = datetime.now(UTC)
    threads = demo.seed("u1", now)
    assert len(threads) == len(demo.SEED) and all(t["owner"] == "u1" for t in threads)
    m = threads[0]["messages"]
    assert [x["created_time"] for x in m] == sorted(x["created_time"] for x in m) and m[-1]["created_time"] == now.isoformat()
    assert {x["from_name"] for x in m} == {"Ana Reyes", demo.SHOP}
    assert demo.is_demo(demo.new_id()) and not demo.is_demo("t_123") and not demo.is_demo("demo_x/../t_1")

    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": "u1", "email": "s@example.com"}
    posted = []
    monkeypatch.setattr(messenger, "graph", lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo chats never call Messenger")))
    monkeypatch.setattr(chats, "demo_doc", lambda cid: {"name": "Ana", "messages": [m[0]]})
    monkeypatch.setattr(
        chats, "demo_threads", lambda: type("C", (), {"document": lambda self, cid: type("D", (), {"update": lambda self, u: posted.append(u)})()})()
    )
    try:
        cid = demo.new_id()
        assert client.get(f"/api/conversations/{cid}").json()[0]["id"] == m[0]["id"]
        assert client.post("/api/conversations/t_1/customer", json={"text": "hi"}).status_code == 404  # only demo chats
        assert client.post(f"/api/conversations/{cid}/customer", json={"text": "Bayad na po"}).status_code == 200
        assert client.post(f"/api/conversations/{cid}/reply", json={"text": "Salamat po"}).status_code == 200
        sent = [u["messages"].values[0] for u in posted]
        assert [(x["from_page"], x["from_name"], x["text"]) for x in sent] == [(False, "Ana", "Bayad na po"), (True, demo.SHOP, "Salamat po")]
    finally:
        main.app.dependency_overrides.clear()


def test_returning_customer_gets_a_new_order_from_new_messages_only():
    from datetime import datetime

    from domain import orders as rules
    from seed import demo

    now = datetime.now(UTC)
    chat, old = demo.returning("u1", "demo_aaaaaaaaaaaa", now)
    # The seeded earlier order went through the real rules: price cited to the seller, confirmed by her link, then cancelled.
    assert (old["status"], old["draft"]["total"], old["confirmations"][-1]["kind"], old["confirm_token"]) == ("cancelled", 22000, "customer", None)
    assert old["draft"]["items"][0]["price_source"] == old["messages"][1]["id"] and rules.done(old)
    assert chat["messages"][: len(old["messages"])] == old["messages"] and chat["owner"] == "u1"

    # The new draft reads only what came after the old order's messages.
    new = rules.since_last_order(chat["messages"], [old])
    assert [m["text"] for m in new] == [t for _, t in demo.RETURNING[2]]
    assert rules.since_last_order(chat["messages"], []) == chat["messages"]
    assert rules.since_last_order(old["messages"], [old]) == []  # nothing new: no second order from the same messages

    # "Same address and number as before": the details come from the order she confirmed, marked as copied, never as cited.
    blank = lambda: {"value": None, "sources": []}
    draft = {
        "items": [{"name": "Candle", "variant": "Lavender", "quantity": 2, "unit_price": 22000, "sources": ["x"]}],
        "total": 44000,
        "recipient_name": {"value": "Leah", "sources": [new[0]["id"]]},
        "phone": blank(),
        "address": blank(),
        "delivery_instructions": blank(),
    }
    filled = rules.carry_over(draft, [("o1", old)])
    assert filled["recipient_name"] == {"value": "Leah Cruz", "sources": [new[0]["id"]], "from_order": "o1"}  # only her first name this time
    other = {**draft, "recipient_name": {"value": "Leah Santos", "sources": ["x"]}}
    assert rules.carry_over(other, [("o1", old)])["recipient_name"] == other["recipient_name"]  # a different name in this chat wins
    assert filled["phone"] == {"value": "0919 555 0177", "sources": [], "from_order": "o1"}
    assert filled["address"]["value"].startswith("7 Sampaguita") and filled["delivery_instructions"] == blank()
    assert rules.missing(filled) == []
    unconfirmed = {**old, "confirmations": []}
    assert rules.carry_over(draft, [("o2", unconfirmed)]) == draft  # only details a customer confirmed are carried
    edited = {**old, "version": 2}
    assert rules.carry_over(draft, [("o2", edited), ("o1", old)])["phone"]["from_order"] == "o1"  # o2 was edited after she confirmed it
    assert not rules.done({"status": "confirmed"}) and rules.done({"status": "confirmed", "receipt": {"status": "customer_confirmed"}})


def test_repeat_customer_seed_and_its_checks():
    from datetime import datetime

    from domain import orders as rules
    from domain import signals as risk
    from seed import demo

    now = datetime.now(UTC)
    receipt, photo = {"kind": "image", "url": "/r"}, {"id": "p1"}
    pay_to = {"name": "Juan Dela Cruz", "mobile": "639171234567", "qr": None, "demo": True}
    chat, old = demo.repeat("u1", "demo_bbbbbbbbbbbb", "o1", now, receipt, photo, pay_to, "1012345678901")
    # The first order went through the real rules: GCash found by the seller, packed by hand, sent, received.
    assert old["manual_payment"]["status"] == "seller_confirmed" and old["packing"]["completed"] and old["fulfillment"]["status"] == "delivered"
    assert old["receipt"]["status"] == "customer_confirmed" and rules.done(old)
    assert [e["source"] for e in old["fulfillment"]["events"]] == ["seller_reported"] * 3
    assert chat["messages"][4]["attachments"] == [receipt]

    # The new draft reads only today's messages: not his first order's "already paid" or "received".
    new = rules.since_last_order(chat["messages"], [old])
    assert [m["text"] for m in new] == [t for _, t in demo.REPEAT[2]]

    # The same claim on both orders: the old one is history settled by its own payment, the new one waits.
    claim = lambda m: {"kind": "payment_claim", "summary": "", "sources": [m["id"]], "check": "", "times": [m["created_time"]]}
    first, again = risk.resolve([claim(chat["messages"][4]), claim(chat["messages"][-1])], [old])
    assert first["earlier"] and first["resolved"] and not again["earlier"] and risk.is_open(again)


def test_damaged_and_to_pack_seeds():
    from datetime import datetime

    from domain import orders as rules
    from domain import packing as pack
    from seed import demo

    now = datetime.now(UTC)
    pay_to = {"name": "Juan Dela Cruz", "mobile": "639171234567", "qr": None, "demo": True}
    photo, sent, claim_photo = {"id": "p1"}, {"kind": "image", "url": "/d"}, {"id": "c1"}
    chat, bea = demo.damaged("u1", "demo_cccccccccccc", "o1", now, photo, sent, "k1", claim_photo, pay_to, "1012345678901")
    # Delivered but not yet received, so her report after delivery is still about this order, and it waits for review.
    assert bea["fulfillment"]["status"] == "delivered" and "receipt" not in bea and not rules.done(bea)
    assert (bea["claims"][0]["status"], bea["claims"][0]["photos"], bea["claims"][0]["review"]) == ("open", [claim_photo], None)
    assert bea["draft"]["total"] == 18000 and chat["messages"][6]["attachments"] == [sent]
    assert max(m["created_time"] for m in chat["messages"]) < (now - timedelta(hours=2 * len(demo.SEED) + 2)).isoformat()  # listed after Rico

    chat, nico = demo.to_pack("u1", "demo_dddddddddddd", now, photo, pay_to, "1012345678902")
    assert pack.paid_by(nico) == "gcash_seller_confirmed" and pack.require_ready(nico)["photos"] == [photo]
    assert not pack.require_ready(nico)["completed"] and nico["draft"]["total"] == 25000
