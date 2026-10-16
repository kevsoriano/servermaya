from datetime import UTC


def test_signals_keep_only_cited_customer_messages_and_resolve_on_payment():
    from domain import signals as risk

    msgs = [{"id": "c1", "from_page": False}, {"id": "s1", "from_page": True}, {"id": "c2", "from_page": False}]
    found = risk.Found(
        signals=[
            risk.Raw(kind="payment_claim", summary="Says paid via GCash", sources=[3, 3]),
            risk.Raw(kind="pressure", summary="Asks to ship today", sources=[2, 3]),  # seller message dropped from citations
            risk.Raw(kind="inconsistent_details", summary="Seller changed price", sources=[2]),  # only seller: dropped
            risk.Raw(kind="receipt_image", summary="Invented", sources=[9]),  # no such message: dropped
        ]
    )
    got = risk.finalize(found, msgs)
    assert [(s["kind"], s["sources"]) for s in got] == [("payment_claim", ["c2"]), ("pressure", ["c2"])]
    assert all(s["check"] for s in got)

    order = lambda ids, **k: {"status": "confirmed", "messages": [{"id": i} for i in ids], **k}
    assert all(s["resolved"] is None for s in risk.resolve(got, []))
    paid = order(["c1", "s1", "c2"], payment={"status": "verified", "amount": 89000})
    extra = [{"kind": k, "summary": "", "sources": ["c1"], "check": ""} for k in ("changed_payment_details", "account_request")]
    done = risk.resolve(got + extra, [paid])
    # A verified payment does not settle changed refund details, nor make sharing a code or opening someone's link safe.
    assert [bool(s["resolved"]) for s in done] == [True, True, False, False]
    # A GCash payment the seller found in their own history settles the payment claim only.
    gcash = order(["c1", "s1", "c2"], manual_payment={"status": "seller_confirmed", "amount": 89000, "confirmed": {"reference": "1234567890123"}})
    assert [bool(s["resolved"]) for s in risk.resolve(got, [gcash])] == [True, False]

    # A seller review closes a check without claiming it was settled; it survives re-analysis while the evidence is the same.
    reviewed = risk.resolve(got, [], {"pressure:c2": {"by": "s", "note": "Regular customer, paid via link before"}})
    assert [(risk.is_open(s), bool(s["reviewed"]), s["resolved"]) for s in reviewed] == [(True, False, None), (False, True, None)]
    moved = [{**got[1], "sources": ["c1"]}]  # re-analysis now rests on a different message
    assert risk.is_open(risk.resolve(moved, [], {"pressure:c2": {"by": "s", "note": "x"}})[0])


def test_an_earlier_orders_payment_does_not_settle_a_new_claim():
    from domain import signals as risk

    order = lambda ids, **k: {"status": "confirmed", "messages": [{"id": i} for i in ids], **k}
    claim = lambda source: {"kind": "payment_claim", "summary": "", "sources": [source], "check": ""}
    checks = [claim("c1"), claim("c9")]  # "already paid" on the first order, then "paid na ulit" in the new messages
    first = order(["c1", "s1"], payment={"status": "verified", "amount": 50000}, receipt={"status": "customer_confirmed"})

    old, new = risk.resolve(checks, [first])
    assert old["resolved"] and old["earlier"] and not risk.is_open(old)  # history, settled on its own order
    assert not new["resolved"] and not new["earlier"] and risk.is_open(new)  # the old payment says nothing about the new claim
    # Drafted into a new order, the claim waits for that order's own payment.
    assert risk.is_open(risk.resolve(checks, [order(["c9"]), first])[1])
    assert risk.resolve(checks, [order(["c9"], payment={"status": "verified", "amount": 50000}), first])[1]["resolved"]
    # A follow-up while the first order is still open is about that order.
    assert risk.resolve(checks, [{**first, "receipt": None}])[1]["resolved"]
    # A cancelled first order is finished too.
    assert risk.is_open(risk.resolve(checks, [{**first, "receipt": None, "status": "cancelled"}])[1])

    # By time: "already paid" sent after the first order was drafted but before it was received is about the first order;
    # the same words after it was received are about the next one.
    from datetime import datetime, timedelta

    from domain import orders as rules

    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    iso = lambda minutes: (t0 + timedelta(minutes=minutes)).isoformat()
    drafted = {
        "status": "confirmed",
        "messages": [{"id": "c1", "created_time": iso(0)}],
        "payment": {"status": "verified", "amount": 25000},
        "receipt": {"status": "customer_confirmed", "at": t0 + timedelta(minutes=30)},
    }
    follow_up, next_order = [{**claim(c), "times": [iso(m)]} for c, m in (("c5", 10), ("c9", 60))]
    settled, waiting = risk.resolve([follow_up, next_order], [drafted])
    assert settled["resolved"] and settled["earlier"] and risk.is_open(waiting) and not waiting["resolved"]
    # One check citing both: split, so the new claim is not hidden in the first order's history.
    both = {**claim("c5"), "sources": ["c5", "c9"], "times": [iso(10), iso(60)]}
    assert [(c["sources"], c["earlier"], risk.is_open(c)) for c in risk.resolve([both], [drafted])] == [(["c5"], True, False), (["c9"], False, True)]

    # The next draft leaves out the first order's follow-ups ("already paid", "received, thanks") too.
    chat = [{"id": c, "created_time": iso(m)} for c, m in (("c1", 0), ("c5", 10), ("c8", 30), ("c9", 60))]
    assert [m["id"] for m in rules.since_last_order(chat, [drafted])] == ["c9"]
    assert [m["id"] for m in rules.since_last_order(chat, [{**drafted, "receipt": None}])] == ["c5", "c8", "c9"]  # still going


def test_payment_claim_says_whether_payment_details_were_shared_first():
    from datetime import datetime

    from domain import signals as risk
    from seed import demo

    now = datetime.now(UTC)
    claim = lambda m: {"kind": "payment_claim", "summary": "", "sources": [m["id"]], "check": "", "times": [m["created_time"]]}

    # Mark: asks the price, orders, and says he already paid. The seller never gave a number, link or request.
    mark = demo.thread("Mark Dizon", demo.SEED[1][1], "u1", now)["messages"]
    [check] = risk.resolve([claim(mark[-1])], [])
    assert risk.payment_context(check, mark, [])[0].startswith("No payment link, GCash request or payment details from you")

    # The seller typed their GCash number first: that message is named.
    told = demo.thread("Mark Dizon", [(True, "Send to GCash 0917 123 4567 po"), (False, "Already paid, ref 1234567890123")], "u1", now)["messages"]
    [check] = risk.resolve([claim(told[-1])], [])
    assert "your message “Send to GCash 0917 123 4567 po”" in risk.payment_context(check, told, [])[0]

    # Rico: his first order had a GCash request and a payment the seller found. Both facts show; the new claim stays open.
    chat, old = demo.repeat(
        "u1",
        "demo_bbbbbbbbbbbb",
        "o1",
        now,
        {"kind": "image"},
        {"id": "p1"},
        {"name": "Juan Dela Cruz", "mobile": "639171234567", "qr": None, "demo": True},
        "1012345678901",
    )
    [check] = risk.resolve([claim(chat["messages"][-1])], [old])
    lines = risk.payment_context(check, chat["messages"], [old])
    assert lines[0].startswith("Payment details were shared before this message: a GCash request with your number")
    assert "GCash reference 1012345678901" in lines[1] and "does not show this payment" in lines[1] and risk.is_open(check)
    # A check on the first order is history and gets no line.
    [first] = risk.resolve([claim(chat["messages"][4])], [old])
    assert risk.payment_context(first, chat["messages"], [old]) == []
