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
    # A GCash payment the seller found in their own history settles the payment claim only.
    gcash = [{"amount": 89000, "confirmed": {"reference": "1234567890123"}}]
    assert [bool(s["resolved"]) for s in risk.resolve(got, [], None, gcash)] == [True, False]

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


def test_gcash_receipt_checks():
    from datetime import datetime

    import receipts as gc

    sent = datetime(2026, 10, 15, 14, 0, tzinfo=gc.PHT)
    arrived = datetime(2026, 10, 15, 14, 20, tzinfo=gc.PHT)
    seller = {"name": "Juan Miguel Dela Cruz", "mobile": "0917 123 1234"}
    good = {"readable": True, "outcome": "successful", "amount_php": 1250.37, "reference": "1012 345 678901", "paid_at": "2026-10-15T14:12",
            "recipient_name": "JU** MI**** D.", "recipient_mobile": "+63 917 *** 1234"}

    def run(used=frozenset(), **k):
        return {c["check"]: c["result"] for c in gc.check_receipt(gc.Receipt(**{**good, **k}), 125037, sent, arrived, seller, set(used))}

    assert set(run().values()) == {"pass"}
    assert run(outcome="failed")["outcome"] == "fail"  # a "Your transfer has failed" screen still shows amount and recipient
    assert run(outcome="pending")["outcome"] == "fail"
    assert run(outcome="not_shown")["outcome"] == "unreadable"
    assert run(amount_php=1250.0)["amount"] == "fail"  # the centavos matter
    assert run(amount_php=None)["amount"] == "unreadable"

    assert run(paid_at="2026-10-14T09:30")["time"] == "fail"  # an old receipt
    assert run(paid_at="2026-10-15T14:40")["time"] == "fail"  # dated after the screenshot arrived
    assert run(paid_at="2026-10-15T13:59")["time"] == "pass"  # minute precision: within the slack
    assert run(paid_at="Oct 15, 2:12 PM")["time"] == "unreadable"

    assert run(used={"1012345678901"})["reference"] == "fail"  # reused on another order
    assert run(reference="12345")["reference"] == "fail"
    assert run(reference=None)["reference"] == "unreadable"
    assert run(reference="757427")["reference"] == "pass"  # a bank app's InstaPay trace, as on a BPI receipt
    assert gc.reference_key("757427", gc.Receipt(**good)) == "20261015-757427"  # traces repeat, so the index keys them by day
    assert gc.reference_key("757427", gc.Receipt(**{**good, "paid_at": None})) is None
    assert gc.reference_key("1012345678901", gc.Receipt(**good)) == "1012345678901"

    assert run(recipient_name="JU** DE** C.")["recipient_name"] == "pass"  # middle name not shown
    assert run(recipient_name="MA** SA****")["recipient_name"] == "fail"
    assert run(recipient_name="DE** JU**")["recipient_name"] == "fail"  # wrong order
    assert run(recipient_name="*** ***")["recipient_name"] == "fail"  # nothing to compare

    assert run(recipient_mobile="09171231234")["recipient_mobile"] == "pass"
    assert run(recipient_mobile="+63 917 *** 9999")["recipient_mobile"] == "fail"
    assert run(recipient_mobile="+63 918 *** 1234")["recipient_mobile"] == "fail"  # same last four, other number
    assert run(recipient_mobile="+63 917 *** **34")["recipient_mobile"] == "pass"
    assert run(recipient_mobile="+63 9** *** **34")["recipient_mobile"] == "unreadable"  # too little to compare

    # A BPI InstaPay receipt shows the end of the GCash account ID from the seller's QR instead of the number.
    qr_seller = {**seller, "user_id": "DWQM4TK3JDNWF205D"}
    bank = lambda account: {c["check"]: c["result"] for c in gc.check_receipt(gc.Receipt(**{**good, "recipient_mobile": None, "recipient_account": account}), 125037, sent, arrived, qr_seller, set())}["recipient_mobile"]
    assert bank("xxxxxxxxxxxxxx05D") == "pass"
    assert gc.masked("KE**N G** S. (QR Code)") == "KE**N G** S."  # BPI's label after the name
    assert bank("xxxxxxxxxxxxxx06D") == "fail"
    assert bank("xxxxxxxxxxxxxxx5D") == "unreadable"  # too little to compare
    assert run(recipient_mobile=None, recipient_account="xxxxxxxxxxxxxx05D")["recipient_mobile"] == "unreadable"  # no QR saved

    # The format on the seller's real QR card: letters hidden mid-word, last digits hidden with bullets.
    kevin = {"name": "Kevin Gil Soriano", "mobile": "639272561234", "masked_name": "KE**N G** S."}
    receipt = gc.Receipt(**{**good, "recipient_name": "KE**N G** S.", "recipient_mobile": "+63  927 256 ••••"})
    by_check = {c["check"]: c for c in gc.check_receipt(receipt, 125037, sent, arrived, kevin, set())}
    assert by_check["recipient_name"]["detail"].endswith("exactly how GCash shows the seller's name")
    assert by_check["recipient_mobile"]["result"] == "pass"
    assert gc.name_fits("KE**N G** S.", "Kevin Soriano") is False  # the G word is missing
    assert gc.name_fits("KE**N S.", "Kevin Gil Soriano")  # a middle name may be left out
    assert gc.name_fits("KE**N G** S.", "Ken Gil Soriano") is False  # ** hides at least one letter
    assert gc.mobile_fits(gc.mobile("+63 927 256 ••••"), "639172561234") is False

    # Copies: the same file, or the same amount and minute under another reference.
    check = lambda **k: {c["check"]: c for c in gc.check_receipt(gc.Receipt(**good), 125037, sent, arrived, seller, set(), **k)}["duplicate"]
    assert check(image_reused=True)["result"] == "fail"
    assert check(twin_reference="9999999999999")["result"] == "fail"
    assert gc.details_key(gc.Receipt(**good)) == "125037_20261015T1412"
    assert gc.details_key(gc.Receipt(**{**good, "paid_at": None})) is None

    assert gc.check_receipt(gc.Receipt(**{**good, "readable": False}), 125037, sent, arrived, seller, set())[0]["result"] == "unreadable"


def test_gcash_manual_payment_rules():
    from datetime import datetime, timezone

    import pytest

    import orders as rules
    import receipts as gc

    now = datetime.now(timezone.utc)
    assert gc.settings(gc.Settings(name="  Juan  Dela Cruz ", mobile="0917 123 1234")) == {"name": "Juan Dela Cruz", "mobile": "639171231234", "qr": None}
    with pytest.raises(rules.Refused):
        gc.settings(gc.Settings(name="Juan", mobile="1234"))

    pay_to = {"name": "Juan Dela Cruz", "mobile": "639171231234"}
    order = {"status": "draft", "version": 1, "draft": {"total": 125037}}
    with pytest.raises(rules.Refused):
        gc.request(order, pay_to, "me", now)  # not confirmed
    order["status"] = "confirmed"
    with pytest.raises(rules.Refused):
        gc.current(order)  # nothing requested yet
    order |= gc.request(order, pay_to, "me", now)
    m = order["manual_payment"]
    assert (m["status"], m["amount"], m["pay_to"], m["requested_at"]) == ("requested", 125037, pay_to, now)
    assert gc.request(order, pay_to, "me", datetime.now(timezone.utc)) == {}  # the first request time stands
    assert main.order_action(order) == "Waiting for GCash payment"

    passing = [gc.check("amount", "pass", ""), gc.check("time", "pass", "")]
    claim = lambda id, ref, checks=passing: {"id": id, "message_id": f"m_{id}", "reference": ref, "checks": checks}
    order |= gc.add_claims(order, [claim("a", "1012345678901"), claim("b", "2012345678901", [gc.check("time", "fail", "old")])])
    assert order["manual_payment"]["status"] == "claimed" and main.order_action(order) == "Review a GCash receipt"
    assert gc.add_claims(order, [claim("a", "1012345678901")]) == {}  # each message once

    with pytest.raises(rules.Refused):
        gc.confirm(order, "12345", "me", "", now, False)  # not a GCash reference
    with pytest.raises(rules.Refused):
        gc.confirm(order, "1012345678901", "me", "", now, True)  # already confirmed on another order
    with pytest.raises(rules.Refused, match="time"):
        gc.confirm(order, "2012345678901", "me", "", now, False)  # a failed check needs a reason
    with pytest.raises(rules.Refused, match="no screenshot"):
        gc.confirm(order, "3012345678901", "me", "", now, False)
    assert gc.confirm(order, "3012345678901", "me", "Customer paid in person at pickup", now, False)["manual_payment"]["confirmed"]["claim_id"] is None

    # A bank payment: GCash history shows its own reference, and the invno ends in the bank receipt's trace.
    shown = {"readable": True, "outcome": "successful", "amount_php": 83.0, "reference": "757427", "paid_at": "2026-10-15T23:59",
             "recipient_name": "KE**N G** S.", "recipient_mobile": None}
    bank = {**order, "manual_payment": {**order["manual_payment"], "claims": [{**claim("t", "757427"), "receipt": shown}]}}
    matched = lambda invno: gc.confirm(bank, "1046160730748", "me", "", now, False, invno)["manual_payment"]["confirmed"]["claim_id"] == "t"
    assert matched("20261015BOPI PHMMXXXB00019511075 7427")
    assert matched("20261016BOPIPHMMXXXB000195110757427")  # dated by the bank just after midnight
    for other in ("20261015BOPIPHMMXXXB000195110757428", "20260914BOPIPHMMXXXB000195110757427"):  # other digits; an older transfer
        with pytest.raises(rules.Refused):
            gc.confirm(bank, "1046160730748", "me", "", now, False, other)

    # The seller's own GCash history screenshot fills in the reference; a failed check on it needs a reason too.
    entry = gc.HistoryEntry(readable=True, direction="received", amount_php=1250.37, reference="1012 345 678901",
                            paid_at=f"{now.astimezone(gc.PHT):%Y-%m-%dT%H:%M}", invno=None)
    assert {c["result"] for c in gc.check_history(entry, 125037, now)} == {"pass"}
    checks = lambda **k: {c["check"]: c["result"] for c in gc.check_history(entry.model_copy(update=k), 125037, now)}
    assert checks(direction="sent")["direction"] == "fail"
    assert checks(amount_php=1250.0)["amount"] == "fail"
    assert checks(paid_at="2026-01-01T09:00")["time"] == "fail"  # came in before payment was asked for
    assert checks(reference="757427")["reference"] == "fail"  # history always shows GCash's own 13 digits
    read = lambda **k: {"image": {"path": "p"}, "reference": "1012345678901", "checks": gc.check_history(entry.model_copy(update=k), 125037, now)}
    with pytest.raises(rules.Refused):
        gc.confirm(order | gc.add_history(order, read(direction="sent")), "1012345678901", "me", "", now, False)
    with_history = order | gc.add_history(order, read())
    assert gc.confirm(with_history, "1012345678901", "me", "", now, False)["manual_payment"]["confirmed"]["history_image"] == {"path": "p"}

    order |= gc.confirm(order, "1012 3456 78901", "me", "", now, False)
    c = order["manual_payment"]["confirmed"]
    assert order["manual_payment"]["status"] == "seller_confirmed" and (c["reference"], c["claim_id"], c["reason"]) == ("1012345678901", "a", None)
    assert main.order_action(order) is None
    assert gc.add_claims(order, [claim("c", "4012345678901")]) == {}  # nothing changes after confirming
    with pytest.raises(rules.Refused):
        rules.reusable_payment(order)  # no PayMongo request on top of a confirmed GCash payment
    with pytest.raises(rules.Refused):
        gc.request(order, pay_to, "me", now)

    # A new version needs a new request; the old one stays on record and no longer counts.
    order["version"] = 2
    with pytest.raises(rules.Refused):
        gc.current(order)
    assert rules.reusable_payment(order) is None
    order |= gc.request(order, pay_to, "me", now)
    assert order["manual_payment"]["version"] == 2 and order["manual_payment_history"][0]["status"] == "seller_confirmed"

    # A PayMongo-verified payment for this version wins.
    paid = {**order, "payment": {"status": "verified", "version": 2}}
    with pytest.raises(rules.Refused):
        gc.request({**paid, "manual_payment": None}, pay_to, "me", now)
    assert main.order_action(paid) is None


def test_gcash_qr_and_settings():
    import pytest

    import orders as rules
    import receipts as gc

    # Decoded from a real GCash "Receive money" QR.
    qr = "00020101021127830012com.p2pqrpay0111GXCHPHM2XXX02089996440303152170200000006560417DWQM4TK3JDNWF205D5204601653036085802PH5912KE**N G** S.6014BARANGGAY 61-A6104123463048CB7"
    assert gc.crc16(qr[:-4]) == "8CB7"
    assert gc.parse_qr(qr) == {"masked_name": "KE**N G** S.", "user_id": "DWQM4TK3JDNWF205D", "payload": qr}
    with pytest.raises(rules.Refused):
        gc.parse_qr(qr.replace("KE**N", "JO**N"))  # tampered: checksum no longer matches
    other_bank = qr.replace("GXCHPHM2XXX", "BNORPHMMXXX")[:-4]
    with pytest.raises(rules.Refused, match="not for a GCash"):
        gc.parse_qr(other_bank + gc.crc16(other_bank))
    with pytest.raises(rules.Refused):
        gc.parse_qr("https://example.com")

    saved = gc.settings(gc.Settings(name="Kevin Gil Soriano", mobile="0927 256 1234", qr=qr))
    assert saved["qr"]["masked_name"] == "KE**N G** S." and saved["mobile"] == "639272561234"
    with pytest.raises(rules.Refused, match="KE\\*\\*N"):
        gc.settings(gc.Settings(name="Juan Dela Cruz", mobile="0927 256 1234", qr=qr))  # typed name does not fit the QR
    assert gc.settings(gc.Settings(name="Kevin Soriano", mobile="09272561234"))["qr"] is None  # QR is optional


def test_packing_accepts_a_seller_confirmed_gcash_payment():
    import pytest

    import orders as rules
    import packing as pack

    order = {"status": "confirmed", "version": 2, "draft": {"items": [{"name": "mug", "variant": None, "quantity": 1}]}}
    with pytest.raises(rules.Refused):
        pack.require_ready(order)
    assert pack.paid_by({**order, "manual_payment": {"status": "claimed", "version": 2}}) is None  # a screenshot alone is not enough
    assert pack.paid_by({**order, "manual_payment": {"status": "seller_confirmed", "version": 1}}) is None  # an older version
    confirmed = {**order, "manual_payment": {"status": "seller_confirmed", "version": 2}}
    assert pack.paid_by(confirmed) == "gcash_seller_confirmed" and pack.require_ready(confirmed)["version"] == 2
    assert pack.paid_by({**confirmed, "payment": {"status": "verified", "version": 2}}) == "paymongo"


def test_gcash_history_export_is_parsed_exactly_and_matched():
    from datetime import datetime, timezone

    import pytest

    import orders as rules
    import receipts as gc

    # The layout of a real export (pypdf text), with made-up figures.
    text = """GCash Transaction History
2026-10-09 to 2026-10-15
Date and Time Description Reference No. Debit Credit Balance
STARTING BALANCE 1,000.00
2026-10-11 08:52 PM Payment to SHOP, Merchant Transaction Number: P1 0000000000001 100.00 900.00
2026-10-14 04:52 PM Received GCash from BPI / VYBE by BPI with account ending in 1111 andinvno:20261014BOPIPHMMXXXB000165215123456 1046000000001 1,250.37 2,150.37
2026-10-14 05:10 PM Received GCash from JU** DE** C. 1046000000002 83.00 2,233.37
ENDING BALANCE 2,233.37
Total Debit 100.00
Total Credit 1,333.37"""
    st = gc.parse_statement(text)
    assert [(c["reference"], c["amount"], c["invno"]) for c in st["credits"]] == [
        ("1046000000001", 125037, "20261014BOPIPHMMXXXB000165215123456"), ("1046000000002", 8300, None)]  # spending is dropped
    assert st["start"] == datetime(2026, 10, 9, tzinfo=gc.PHT) and st["last_at"] == datetime(2026, 10, 14, 17, 10, tzinfo=gc.PHT)
    for broken in (text.replace("2,150.37", "2,150.73"),  # a digit read wrong breaks the running balance
                   text.replace("2026-10-14 05:10 PM", "2026-10-14 05:10"),  # a row skipped breaks the ending balance
                   "Some other PDF"):
        with pytest.raises(rules.Refused):
            gc.parse_statement(broken)

    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    sent = datetime(2026, 10, 14, 16, 0, tzinfo=gc.PHT)
    passing = [gc.check("amount", "pass", "")]
    bank_claim = {"id": "t", "reference": "123456", "receipt": {"paid_at": "2026-10-14T16:51"}, "checks": passing}
    order = {"status": "confirmed", "version": 1, "manual_payment": {"status": "claimed", "version": 1, "amount": 125037, "requested_at": sent,
                                                                      "claims": [bank_claim]}}
    run = lambda o, taken=frozenset(): gc.reconcile(o, st, "s1", set(taken), "me", now)

    # A bank screenshot whose 6 digits end the invno, with the exact amount: confirmed from the export.
    c = run(order)["manual_payment"]["confirmed"]
    assert (c["reference"], c["claim_id"], c["statement"]) == ("1046000000001", "t", "s1")
    assert run(order, {"1046000000001"}) == {}  # already confirmed on another order
    assert run({**order, "manual_payment": {**order["manual_payment"], "amount": 125000}}) == {}  # amount differs
    assert run({**order, "manual_payment": {**order["manual_payment"], "requested_at": now}}) == {}  # arrived before the request
    assert run({**order, "manual_payment": {**order["manual_payment"], "claims": []}}) == {}  # nothing points to the row
    seller_read = {**order["manual_payment"], "claims": [], "history_read": {"reference": "1046000000001", "image": {"path": "h"}}}
    assert run({**order, "manual_payment": seller_read})["manual_payment"]["confirmed"]["history_image"] == {"path": "h"}

    # Confirmed by hand: found, a different amount, missing although the export covers it, or outside the export.
    done = lambda ref, at=datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc), amount=125037: {**order, "manual_payment": {
        **order["manual_payment"], "status": "seller_confirmed", "amount": amount, "confirmed": {"reference": ref, "at": at}}}
    assert run(done("1046000000001"))["manual_payment"]["history_check"]["status"] == "found"
    assert run(done("1046000000001", amount=8300))["manual_payment"]["history_check"]["status"] == "amount_differs"
    assert run(done("1046999999999"))["manual_payment"]["history_check"]["status"] == "not_found"
    assert run(done("1046999999999", at=now)) == {}  # confirmed after the export ends: it may simply not be in it yet
    assert run({**order, "version": 2}) == {}  # the request is for an older version


def test_profile_pics_batched_cached_and_soft_failing(monkeypatch):
    calls = []
    def fake(path, **k):
        calls.append(k["ids"])
        if k["ids"] == "bad":
            raise main.HTTPException(502, "nope")
        return {"a": {"profile_pic": "https://pic/a"}, "b": {}}
    monkeypatch.setattr(main, "graph", fake)
    monkeypatch.setattr(main, "_pics", {})
    assert main.profile_pics(["a", "b"]) == {"a": "https://pic/a", "b": None}
    assert main.profile_pics(["a", "b"]) == {"a": "https://pic/a", "b": None}
    assert calls == ["a,b"]  # second call served from cache
    assert main.profile_pics(["bad"]) == {"bad": None}
    main._pics["a"] = (0, "old")  # expired
    main.profile_pics(["a"])
    assert calls[-1] == "a" and main._pics["a"][1] == "https://pic/a"
