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

    assert demo.parse("Customer: hi\n  2 pcs please\n\nseller: ₱180 each") == [(False, "hi\n2 pcs please"), (True, "₱180 each")]
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
        assert client.post(f"/api/conversations/{cid}/customer", json={"text": "I paid already"}).status_code == 200
        assert client.post(f"/api/conversations/{cid}/reply", json={"text": "Thank you"}).status_code == 200
        sent = [u["messages"].values[0] for u in posted]
        assert [(x["from_page"], x["from_name"], x["text"]) for x in sent] == [(False, "Ana", "I paid already"), (True, demo.SHOP, "Thank you")]
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
    photo, film = {"id": "p1"}, {"id": "v1", "content_type": "video/mp4"}
    sent = {"kind": "image", "url": "/d"}, {"kind": "video", "url": "/v"}
    claim_photo, claim_video = {"id": "c1"}, {"id": "c2", "content_type": "video/mp4"}
    chat, bea = demo.damaged("u1", "demo_cccccccccccc", "o1", now, (photo, film), sent, "k1", [claim_photo, claim_video], pay_to, "1012345678901")
    # Delivered but not yet received, so her report after delivery is still about this order, and it waits for review.
    assert bea["fulfillment"]["status"] == "delivered" and "receipt" not in bea and not rules.done(bea)
    assert (bea["claims"][0]["status"], bea["claims"][0]["photos"], bea["claims"][0]["review"]) == ("open", [claim_photo], None)
    assert bea["claims"][0]["videos"] == [claim_video] and bea["packing"]["videos"] == [film]  # her unboxing video; the seller's packing video
    assert bea["draft"]["total"] == 18000 and [m["attachments"] for m in chat["messages"][6:8]] == [[sent[0]], [sent[1]]]
    assert max(m["created_time"] for m in chat["messages"]) < (now - timedelta(hours=2 * len(demo.SEED) + 2)).isoformat()  # listed after Rico

    chat, nico = demo.to_pack("u1", "demo_dddddddddddd", now, photo, pay_to, "1012345678902")
    assert pack.paid_by(nico) == "gcash_seller_confirmed" and pack.require_ready(nico)["photos"] == [photo]
    assert not pack.require_ready(nico)["completed"] and nico["draft"]["total"] == 25000


def test_the_demo_customer_taps_answers_and_sends_receipts_in_its_script(monkeypatch):
    from datetime import datetime

    from core import config, db
    from domain import customer_agent as agent
    from helpers import fake_google, ready_order
    from seed import demo

    prompts, turns = [], []

    def generate_content(self, model, contents, config):
        prompts.append(contents)
        return type("R", (), {"text": turns.pop(0).model_dump_json()})()

    gemini = type("G", (), {"models": type("M", (), {"generate_content": generate_content})()})()
    store, _ = fake_google(monkeypatch, gemini)
    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": "u1", "email": "v@example.com", "demo": True}
    now = datetime.now(UTC)
    mark, dan = demo.new_id(), demo.new_id()
    for cid, (name, lines) in ((mark, demo.SEED[1]), (dan, demo.SEED[2])):
        chats.demo_threads().document(cid).set(demo.thread(name, lines, "u1", now - timedelta(minutes=5)))
    source = {"type": "demo", "conversation_id": mark, "owner": "u1"}
    drafted = chats.demo_doc(mark)["messages"]  # the evidence a draft is made from
    db.orders().document("mark1").set(ready_order(status="draft", confirmations=[], payment=None, packing=None, source=source, messages=drafted))
    chat = lambda cid: client.get(f"/api/conversations/{cid}").json()
    auto = lambda cid: client.post(f"/api/conversations/{cid}/customer/auto").json()
    try:
        assert auto(mark) == {"action": "none"}  # the customer wrote last: nothing to answer

        # The seller's summary carries a Confirm button: the customer taps it at once, with no Gemini call.
        client.post("/api/orders/mark1/ask-confirmation")
        assert auto(mark) == {"action": "tap"} and not prompts
        assert chat(mark)[-1]["text"] == "Confirm #MARK1 v1" and db.orders().document("mark1").get().to_dict()["status"] == "confirmed"

        # "Already paid by GCash": the first receipt always follows the request, and Mark's script sends one paid to someone else.
        r = client.post("/api/orders/mark1/gcash/request", json={"says_paid": True})
        assert r.status_code == 200, r.text
        turns.append(agent.Turn(message="Here's my receipt, please ship today!", send_receipt=False))
        assert auto(mark) == {"action": "receipt", "kind": "wrong_recipient"}
        assert "attaching your GCash receipt" in prompts[-1] and "Mark is in a hurry" in prompts[-1]
        words, receipt = chat(mark)[-2:]
        assert words["text"] == "Here's my receipt, please ship today!" and receipt["attachments"][0]["kind"] == "image"
        assert not (store.docs.get(f"stores/{config.STORE}/demoUsers/u1") or {}).get("samples")  # not paid to the shop

        # The seller says it went to someone else: Gemini asks for another, and code sends the next in his script, the matching one.
        client.post(f"/api/conversations/{mark}/reply", json={"text": "This receipt shows a different recipient. Please send the right one."})
        turns.append(agent.Turn(message="Sorry! Sent it to an old number. Here's the new one.", send_receipt=True))
        assert auto(mark) == {"action": "receipt", "kind": "matching"}
        assert "can send a GCash receipt" in prompts[-1]
        assert len(store.docs[f"stores/{config.STORE}/demoUsers/u1"]["samples"]) == 1  # now in the sample GCash history

        # Anything else is words only.
        client.post(f"/api/conversations/{mark}/reply", json={"text": "Got it, thanks Mark!"})
        turns.append(agent.Turn(message="Thank you!", send_receipt=False))
        assert auto(mark) == {"action": "reply"} and chat(mark)[-1]["text"] == "Thank you!"

        # Dan never has a receipt, whatever Gemini says.
        client.post(f"/api/conversations/{dan}/reply", json={"text": "Sorry, we only take GCash."})
        turns.append(agent.Turn(message="Just open Binance please, it's quick!", send_receipt=True))
        assert auto(dan) == {"action": "reply"} and not chat(dan)[-1]["attachments"]
        assert "no receipt or photo to send" in prompts[-1]

        # The seller wrote again while Gemini was answering: the stale answer is dropped.
        client.post(f"/api/conversations/{dan}/reply", json={"text": "Do you want to pay by GCash?"})

        def late(self, model, contents, config):
            client.post(f"/api/conversations/{dan}/reply", json={"text": "Hello?"})
            return type("R", (), {"text": agent.Turn(message="Binance!", send_receipt=False).model_dump_json()})()

        monkeypatch.setattr(type(gemini.models), "generate_content", late)
        assert auto(dan) == {"action": "none"} and chat(dan)[-1]["text"] == "Hello?"
    finally:
        main.app.dependency_overrides.clear()


def test_customer_personas_match_the_seed_and_scripts():
    from datetime import datetime

    from domain import customer_agent as agent
    from seed import demo

    seeded = [n for n, _ in demo.SEED] + [demo.RETURNING[0], demo.REPEAT[0], demo.DAMAGED[0], demo.TO_PACK[0]]
    assert set(agent.PERSONAS) == set(seeded)  # every seeded customer has a persona, and only they do
    mark = agent.persona("Mark Dizon")
    assert [agent.receipt_kind(mark, n) for n in range(3)] == ["wrong_recipient", "matching", "matching"]
    assert agent.receipt_kind(agent.persona("Dan Cortez"), 0) is None and agent.persona("Someone new") is agent.ANYONE
    at = datetime(2026, 10, 1, tzinfo=UTC)
    msg = lambda page, minutes, kind=None: {
        "from_page": page,
        "created_time": (at + timedelta(minutes=minutes)).isoformat(),
        "attachments": [{"kind": kind}] if kind else [],
    }
    messages = [msg(False, -5, "image"), msg(True, 1, "image"), msg(False, 2), msg(False, 3, "image")]
    assert agent.receipts_sent(messages, at) == 1  # only the customer's images since the request
