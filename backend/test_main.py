from fastapi.testclient import TestClient
from google.oauth2 import id_token

import main

client = TestClient(main.app)
ISS = f"https://securetoken.google.com/{main.PROJECT_ID}"


def fake_token(monkeypatch, **claims):
    base = {"sub": "u1", "iss": ISS, "email": "ok@example.com", "email_verified": True}
    monkeypatch.setattr(id_token, "verify_firebase_token", lambda *a, **k: {**base, **claims})
    monkeypatch.setattr(main, "ALLOWLIST", {"ok@example.com"})
    return client.get("/api/me", headers={"Authorization": "Bearer x"})


def test_healthz_is_public():
    assert client.get("/api/healthz").status_code == 200


def test_missing_and_garbage_tokens_rejected():
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_allowlisted_user_passes(monkeypatch):
    r = fake_token(monkeypatch, email="OK@example.com")
    assert r.status_code == 200 and r.json()["email"] == "ok@example.com"


def test_rejections(monkeypatch):
    assert fake_token(monkeypatch, email="other@example.com").status_code == 403
    assert fake_token(monkeypatch, email_verified=False).status_code == 403
    assert fake_token(monkeypatch, iss="https://securetoken.google.com/other").status_code == 401


def test_conversations(monkeypatch):
    main.app.dependency_overrides[main.current_user] = lambda: {"uid": "u1"}
    monkeypatch.setattr(main, "PAGE_ID", "99")
    monkeypatch.setattr(main, "PAGE_TOKEN", "tok")
    calls = []

    class Resp:
        ok = True

        def json(self):
            return {"data": [
                {"id": "m2", "message": "magkano po?", "from": {"id": "7", "name": "Ana"}},
                {"id": "m1", "message": "hello", "from": {"id": "99", "name": "Shop"}},
            ]}

    monkeypatch.setattr(main.requests, "request", lambda method, url, **k: calls.append((url, k)) or Resp())
    try:
        msgs = client.get("/api/conversations/t_123").json()
        assert [m["id"] for m in msgs] == ["m1", "m2"]  # oldest first
        assert [m["from_page"] for m in msgs] == [True, False]
        assert "tok" not in calls[0][0]  # token is not in the URL
        assert client.get("/api/conversations/me%3Ffields=access_token").status_code == 400
        assert len(calls) == 1
    finally:
        main.app.dependency_overrides.clear()


def test_finalize_computes_totals_and_drops_bad_citations():
    from extraction import Claim, Extraction, Item, Text, finalize

    msgs = [{"id": "m1"}, {"id": "m2"}]
    none = Text(value=None, sources=[])
    raw = Extraction(
        items=[
            Item(name="mug", variant="blue", quantity=3, unit_price_php=180, sources=[1, 1, 2]),
            Item(name="tumbler", variant=None, quantity=1, unit_price_php=349.99, sources=[2]),
            Item(name="invented", variant=None, quantity=1, unit_price_php=1, sources=[9]),
        ],
        recipient_name=Text(value="Ana", sources=[2]),
        phone=Text(value="0917", sources=[7]),
        address=none,
        delivery_instructions=none,
        claims=[Claim(kind="payment", text="paid", amount_php=890, reference="r", pressure=True, sources=[2])],
        clarification_questions=[],
    )
    d = finalize(raw, msgs)
    assert [i["name"] for i in d["items"]] == ["mug", "tumbler"]  # uncited item dropped
    assert d["items"][0]["sources"] == ["m1", "m2"]
    assert d["total"] == 3 * 18000 + 34999
    assert d["recipient_name"]["value"] == "Ana" and d["phone"]["value"] is None  # bad citation withheld
    assert d["claims"][0]["status"] == "unverified" and d["claims"][0]["amount"] == 89000

    raw.items[0].unit_price_php = None
    assert finalize(raw, msgs)["total"] is None  # no total when any price is unknown


def test_order_versioning_and_confirmation():
    from datetime import datetime, timezone

    import pytest

    import orders as rules

    src = lambda v: {"value": v, "sources": ["m1"]}
    item = {"name": "mug", "variant": "blue", "quantity": 2, "unit_price": 18000, "sources": ["m1"]}
    draft = {"items": [item], "total": 36000, "claims": [], "clarification_questions": [],
             "recipient_name": src("Ana"), "phone": src("0917"), "address": {"value": None, "sources": []},
             "delivery_instructions": {"value": None, "sources": []}}
    order = {"status": "draft", "version": 1, "draft": draft, "confirmations": [], "confirm_token": None}
    now = datetime.now(timezone.utc)
    body = lambda **k: rules.Edit(**{"items": [{k2: item[k2] for k2 in ("name", "variant", "quantity", "unit_price")}],
                                     "recipient_name": "Ana", "phone": "0917", **k})

    # Missing address blocks sending and confirming.
    assert rules.missing(draft) == ["Delivery address"]
    with pytest.raises(rules.Refused):
        rules.send(order, "tok")
    with pytest.raises(rules.Refused):
        rules.confirm(order, "seller_asserted", "me", now)

    assert rules.edit(order, body()) == {}  # nothing changed, no new version

    up = rules.edit(order, body(address="QC"))
    assert up["version"] == 2 and up["draft"]["items"][0]["sources"] == ["m1"]  # untouched item keeps its source
    assert up["draft"]["address"] == {"value": "QC", "sources": []}  # seller-entered
    order = {**order, **up}

    order = {**order, **rules.send(order, "tok")}
    assert order["status"] == "awaiting_confirmation" and order["confirm_token"] == "tok"
    assert rules.send(order, "other") == {}  # same link stays valid

    order = {**order, **rules.confirm(order, "customer", "confirmation_link", now)}
    assert order["status"] == "confirmed" and order["confirmations"][0]["version"] == 2
    assert rules.confirm(order, "customer", "confirmation_link", now) == {}  # idempotent

    # Changing a confirmed order: new version, back to draft, link dead, earlier agreement kept.
    changed = body(address="QC")
    changed.items[0].quantity = 3
    up = rules.edit(order, changed)
    assert (up["version"], up["status"], up["confirm_token"]) == (3, "draft", None)
    assert up["draft"]["total"] == 54000 and up["draft"]["items"][0]["sources"] == []
    order = {**order, **up}
    assert len(order["confirmations"]) == 1
    with pytest.raises(rules.Refused):
        rules.confirm(order, "customer", "confirmation_link", now)  # customer cannot confirm without a new link

    # Cancelling keeps history, kills the link, and freezes the order.
    order = {**order, **rules.send(order, "tok2")}
    order = {**order, **rules.cancel(order, "me", " out of stock ", now)}
    assert order["status"] == "cancelled" and order["confirm_token"] is None
    assert order["cancelled"]["reason"] == "out of stock" and len(order["confirmations"]) == 1
    assert rules.cancel(order, "me", "", now) == {}
    for refused in (lambda: rules.edit(order, body()), lambda: rules.send(order, "t"), lambda: rules.confirm(order, "seller_asserted", "me", now)):
        with pytest.raises(rules.Refused):
            refused()


def test_payment_reconciliation():
    from datetime import datetime, timezone

    import pytest

    import orders as rules

    now = datetime.now(timezone.utc)
    order = {"status": "draft", "version": 2, "draft": {"total": 89000}}
    with pytest.raises(rules.Refused):
        rules.reusable_payment(order)  # not confirmed yet
    order["status"] = "confirmed"
    assert rules.reusable_payment(order) is None
    order |= rules.request_payment(order, "cs_1", "https://pay/1", False, now)
    assert order["payment"]["status"] == "pending" and order["payment"]["amount"] == 89000
    assert rules.reusable_payment(order)["session_id"] == "cs_1"
    assert rules.request_payment(order, "cs_2", "https://pay/2", False, now) == {}

    pay = lambda id, amount=89000: {"id": id, "amount": amount, "currency": "PHP", "paid_at": 1}
    # Nothing paid yet: still pending, but the check time is recorded.
    order |= rules.reconcile(order, "cs_1", "active", [], now)
    assert order["payment"]["status"] == "pending" and order["payment"]["checked_at"] == now

    wrong = {**order, **rules.reconcile(order, "cs_1", "active", [pay("pay_x", 71000)], now)}
    assert wrong["payment"]["status"] == "mismatched"
    with pytest.raises(rules.Refused):
        rules.reusable_payment(wrong)

    order |= rules.reconcile(order, "cs_1", "active", [pay("pay_1")], now)
    assert order["payment"]["status"] == "verified" and order["payment"]["payment_id"] == "pay_1"
    # Duplicate and late events change nothing; an expired session cannot undo a verified payment.
    again = rules.reconcile(order, "cs_1", "expired", [pay("pay_1")], now)
    assert again["payment"]["status"] == "verified" and "payment_review" not in again
    with pytest.raises(rules.Refused):
        rules.reusable_payment(order)

    # A second payment, or one on a replaced request, is recorded for review and never marks anything paid.
    order |= rules.reconcile(order, "cs_1", "active", [pay("pay_1"), pay("pay_2")], now)
    order |= rules.reconcile(order, "cs_old", "active", [pay("pay_3")], now)
    assert [r["payment_id"] for r in order["payment_review"]] == ["pay_2", "pay_3"]
    assert order["payment"]["payment_id"] == "pay_1"
    assert rules.reconcile(order, "cs_old", "active", [pay("pay_3")], now) == {}

    # Unpaid request expires; a pending request is dropped when the order changes or is cancelled.
    fresh = {"status": "confirmed", "version": 1, "draft": {"total": 100}}
    fresh |= rules.request_payment(fresh, "cs_9", "u", False, now)
    assert rules.cancel(fresh, "me", "", now)["payment"] is None
    fresh |= rules.reconcile(fresh, "cs_9", "expired", [], now)
    assert fresh["payment"]["status"] == "expired" and rules.reusable_payment(fresh) is None


def test_webhook_signature():
    import hashlib
    import hmac

    raw = b'{"data":{"id":"evt_1"}}'
    sig = hmac.new(b"whsk", b"1700000000." + raw, hashlib.sha256).hexdigest()
    assert main.valid_signature(f"t=1700000000,te={sig},li=", raw, "whsk")
    assert not main.valid_signature(f"t=1700000000,te={sig},li=", raw + b" ", "whsk")
    assert not main.valid_signature(f"t=1700000001,te={sig},li=", raw, "whsk")
    assert not main.valid_signature("", raw, "whsk") and not main.valid_signature("t=1,te=,li=", raw, "whsk")


def test_attachments_are_normalised():
    msg = {
        "sticker": "https://cdn/sticker.png",
        "attachments": {"data": [
            {"mime_type": "image/jpeg", "name": "a.jpg", "image_data": {"url": "https://cdn/a.jpg", "preview_url": "https://cdn/a_p.jpg"}},
            {"mime_type": "video/mp4", "name": "v.mp4", "video_data": {"url": "https://cdn/v.mp4", "preview_url": "https://cdn/v.jpg"}},
            {"mime_type": "audio/mp4", "name": "voice.mp4", "file_url": "https://cdn/voice.mp4"},
            {"mime_type": "application/pdf", "name": "r.pdf", "file_url": "https://cdn/r.pdf"},
            {"mime_type": "image/png", "name": "bad", "image_data": {"url": "javascript:alert(1)"}},
            {"mime_type": "image/png", "name": "missing"},
        ]},
    }
    got = main.attachments(msg)
    assert [(a["kind"], a["url"]) for a in got] == [
        ("image", "https://cdn/a.jpg"), ("video", "https://cdn/v.mp4"), ("audio", "https://cdn/voice.mp4"),
        ("file", "https://cdn/r.pdf"), ("image", "https://cdn/sticker.png"),
    ]
    assert got[0]["preview_url"] == "https://cdn/a_p.jpg"
    assert main.attachments({"message": "text only"}) == []


def test_reply_window_and_send(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+0000")
    msgs = [{"from_page": False, "created_time": iso(now - timedelta(hours=30))},
            {"from_page": True, "created_time": iso(now - timedelta(hours=1))}]
    # Only the customer's messages open the window; the Page's own replies do not extend it.
    assert main.reply_until(msgs) < now
    assert main.reply_until([msgs[1]]) is None

    main.app.dependency_overrides[main.current_user] = lambda: {"uid": "u1", "email": "s@example.com"}
    monkeypatch.setattr(main, "PAGE_ID", "99")
    sent, stored = [], []
    monkeypatch.setattr(main, "conversation", lambda cid, user: msgs)
    monkeypatch.setattr(main, "graph", lambda path, send=None, **k: sent.append((path, send)) or (
        {"participants": {"data": [{"id": "99"}, {"id": "psid7"}]}} if "fields" in k else {"message_id": "mid.1"}))

    class Store:
        def collection(self, *_):
            return self

        def document(self, *_):
            return self

        def add(self, doc):
            stored.append(doc)

    monkeypatch.setattr(main, "clients", lambda: (None, Store()))
    try:
        assert client.post("/api/conversations/t_1/reply", json={"text": "hi"}).status_code == 409  # window closed
        assert sent == []
        msgs[0]["created_time"] = iso(now - timedelta(hours=2))
        assert client.post("/api/conversations/t_1/reply", json={"text": ""}).status_code == 422
        r = client.post("/api/conversations/t_1/reply", json={"text": "Here is your link", "order_id": "o1"})
        assert r.status_code == 200 and r.json() == {"message_id": "mid.1"}
        assert sent[-1] == ("99/messages", {"recipient": {"id": "psid7"}, "messaging_type": "RESPONSE", "message": {"text": "Here is your link"}})
        assert stored[0]["sent_by"] == "s@example.com" and stored[0]["order_id"] == "o1"
    finally:
        main.app.dependency_overrides.clear()


def test_thread_list_read_state_and_pending_actions(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+0000")
    thread = lambda cid, sender, at: {"id": cid, "updated_time": iso(at), "participants": {"data": [{"id": "99"}, {"id": "7", "name": "Ana"}]},
                                      "messages": {"data": [{"from": {"id": sender}, "created_time": iso(at)}]}}
    main.app.dependency_overrides[main.current_user] = lambda: {"uid": "u1"}
    monkeypatch.setattr(main, "PAGE_ID", "99")
    monkeypatch.setattr(main, "graph", lambda path, **k: {"data": [
        thread("t_1", "7", now - timedelta(hours=21)),  # customer waiting, window closing
        thread("t_2", "99", now - timedelta(hours=1)),  # seller replied last
        thread("t_3", "7", now - timedelta(hours=2)),  # customer waiting, but seen since
    ]})
    monkeypatch.setattr(main, "cached_open_checks", lambda cid: None)
    monkeypatch.setattr(main, "read_marks", lambda user: {"t_3": now - timedelta(hours=1)})
    monkeypatch.setattr(main, "order_actions", lambda ids: {"t_2": ["Waiting for payment"]})
    try:
        t1, t2, t3 = client.get("/api/conversations").json()
        assert (t1["unread"], t1["awaiting_reply"]) == (True, True)
        assert datetime.fromisoformat(t1["reply_until"]) - now < timedelta(hours=4)
        assert (t2["unread"], t2["awaiting_reply"], t2["reply_until"]) == (False, False, None)
        assert t2["order_actions"] == ["Waiting for payment"]
        assert (t3["unread"], t3["awaiting_reply"]) == (False, True)
        assert client.post("/api/conversations/bad/seen").status_code == 400
    finally:
        main.app.dependency_overrides.clear()

    assert main.order_action({"status": "draft"}) == "Send the draft order"
    assert main.order_action({"status": "confirmed"}) == "Request payment"
    assert main.order_action({"status": "confirmed", "payment": {"status": "verified"}}) is None
    assert main.order_action({"status": "cancelled"}) is None


def test_signals_keep_only_cited_customer_messages_and_resolve_on_payment():
    import signals as risk

    msgs = [{"id": "c1", "from_page": False}, {"id": "s1", "from_page": True}, {"id": "c2", "from_page": False}]
    found = risk.Found(signals=[
        risk.Raw(kind="payment_claim", summary="Says paid via GCash", sources=[3, 3]),
        risk.Raw(kind="pressure", summary="Asks to ship today", sources=[2, 3]),  # seller message dropped from citations
        risk.Raw(kind="inconsistent_details", summary="Seller changed price", sources=[2]),  # only seller: dropped
        risk.Raw(kind="receipt_image", summary="Invented", sources=[9]),  # no such message: dropped
    ])
    got = risk.finalize(found, msgs)
    assert [(s["kind"], s["sources"]) for s in got] == [("payment_claim", ["c2"]), ("pressure", ["c2"])]
    assert all(s["check"] for s in got)

    assert all(s["resolved"] is None for s in risk.resolve(got, []))
    done = risk.resolve(got + [{"kind": "changed_payment_details", "summary": "", "sources": ["c1"], "check": ""}], [{"amount": 89000}])
    assert [bool(s["resolved"]) for s in done] == [True, True, False]  # a verified payment does not settle changed refund details

    # A seller review closes a check without claiming it was settled; it survives re-analysis while the evidence is the same.
    reviewed = risk.resolve(got, [], {"pressure:c2": {"by": "s", "note": "Regular customer, paid via link before"}})
    assert [(risk.is_open(s), bool(s["reviewed"]), s["resolved"]) for s in reviewed] == [(True, False, None), (False, True, None)]
    moved = [{**got[1], "sources": ["c1"]}]  # re-analysis now rests on a different message
    assert risk.is_open(risk.resolve(moved, [], {"pressure:c2": {"by": "s", "note": "x"}})[0])


def test_packing_rules():
    from datetime import datetime, timezone

    import pytest

    import orders as rules
    import packing as pack

    now = datetime.now(timezone.utc)
    items = [{"name": "mug", "variant": "blue", "quantity": 2, "unit_price": 18000, "sources": []},
             {"name": "tumbler", "variant": None, "quantity": 1, "unit_price": 35000, "sources": []}]
    order = {"status": "confirmed", "version": 1, "draft": {"items": items}, "payment": {"status": "pending", "version": 1}}
    with pytest.raises(rules.Refused):
        pack.require_ready(order)  # not paid yet
    order["payment"] = {"status": "verified", "version": 1}
    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # no photos
    order |= pack.add_photo(order, {"id": "p1"})
    order |= pack.add_photo(order, {"id": "p2"})

    seen = lambda line, **k: pack.Seen(**{"line": line, "visible": True, "counts": [], "damage": None, "photos": [1], "note": "", **k})
    c = lambda *pairs: [pack.Count(variant=v, quantity=q) for v, q in pairs]
    # The PRD case: two blue mugs expected, one blue and one red in the photo.
    report = pack.Report(lines=[seen(1, counts=c(("blue", 1), ("red", 1))), seen(2, counts=c((None, 1)), photos=[7])], suggestions=["Lay the mugs side by side"])
    order |= pack.apply_check(order, report, "m", now)
    assert [r["auto_result"] for r in order["packing"]["rows"]] == ["conflict", "insufficient_visibility"]
    assert order["packing"]["rows"][1]["photos"] == []  # citation of a photo that does not exist is dropped

    blue2 = {"variant": "blue", "quantity": 2}
    assert pack.result(blue2, seen(1, counts=c(("Blue", 2)))) == "matched"
    assert pack.result(blue2, seen(1, counts=c(("light blue", 1), ("blue", 1)))) == "matched"
    assert pack.result(blue2, seen(1, counts=c(("red", 2)))) == "conflict"
    assert pack.result(blue2, seen(1, counts=c(("blue", 1)))) == "conflict"  # missing one
    assert pack.result(blue2, seen(1, counts=c(("blue", 3)))) == "conflict"  # one too many
    assert pack.result(blue2, seen(1, counts=c(("blue", 2)), damage="chipped rim")) == "conflict"
    assert pack.result(blue2, seen(1, counts=c((None, 2)))) == "insufficient_visibility"
    assert pack.result(blue2, seen(1, counts=[])) == "insufficient_visibility"  # could not count
    assert pack.result(blue2, seen(1, counts=c(("blue", 2)), photos=[])) == "insufficient_visibility"  # no photo cited
    assert pack.result({"variant": None, "quantity": 1}, seen(2, visible=False, counts=c((None, 1)))) == "insufficient_visibility"
    assert pack.result({"variant": None, "quantity": 1}, seen(2, counts=c((None, 1)))) == "matched"
    assert pack.result(blue2, None) == "insufficient_visibility"

    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # mug conflict unresolved
    order |= pack.confirm_row(order, 1, "me", "Swapped the red mug for a blue one", now)
    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # tumbler still unresolved
    with pytest.raises(rules.Refused):
        pack.confirm_row(order, 2, "me", "  ", now)
    order |= pack.confirm_row(order, 2, "me", "Checked tumbler by hand, sealed box", now)
    row = order["packing"]["rows"][1]
    assert pack.status(row) == "seller_confirmed" and row["auto_result"] == "insufficient_visibility"  # both kept
    order |= pack.complete(order, "me", now)
    assert order["packing"]["completed"]["by"] == "me"
    with pytest.raises(rules.Refused):
        pack.add_photo(order, {"id": "p3"})

    # Changing the order voids packing but keeps it on record.
    body = rules.Edit(items=[{"name": "mug", "variant": "blue", "quantity": 3, "unit_price": 18000}])
    full = {**order, "draft": {**order["draft"], "total": 36000, "recipient_name": {"value": None, "sources": []}, "phone": {"value": None, "sources": []},
                                "address": {"value": None, "sources": []}, "delivery_instructions": {"value": None, "sources": []}}}
    up = rules.edit(full, body)
    assert up["packing"] is None and up["packing_history"][0]["completed"]
