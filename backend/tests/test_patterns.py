from datetime import UTC


def test_patterns_counts_denominators_and_counterexamples():
    from datetime import datetime, timedelta

    from domain import patterns

    now = datetime(2026, 10, 30, tzinfo=UTC)
    day = lambda n: now - timedelta(days=n)

    def order(oid, customer, courier, product, claim_types=(), driver=None, refs=()):
        f = {"id": f"f_{oid}", "status": "delivered", "courier": courier, "driver_id": driver, "created_at": day(5), "events": []}
        claims = [
            {"id": f"c_{oid}_{n}", "type": t, "fulfillment_id": f["id"], "created_at": day(2), "status": "open", "decision": None}
            for n, t in enumerate(claim_types)
        ]
        manual = {"claims": [{"reference": r, "image": None, "received_at": day(3)} for r in refs]} if refs else None
        return {
            "id": oid,
            "created_at": day(6),
            "source": {"conversation_id": customer},
            "fulfillment": f,
            "claims": claims,
            "manual_payment": manual,
            "draft": {"items": [{"name": product, "variant": None}]},
        }

    # 20 Lalamove parcels (high volume, ordinary rate: 2 losses); 6 "Rush" parcels with 4 losses, 3 of them the fragile vase.
    orders = [order(f"l{n}", f"cust{n}", "lalamove", "Tote", ["damage"] if n < 2 else ()) for n in range(20)]
    orders += [
        order(f"r{n}", f"buyer{n}", "rush", "Vase" if n < 3 else "Tote", ["damage"] if n < 4 else (), driver="D1" if n < 4 else None)
        for n in range(6)
    ]
    # One customer with two claims on three parcels, one reported twice (counts once).
    orders += [
        order("a1", "ana", "lalamove", "Tote", ["missing_item", "missing_item"]),
        order("a2", "ana", "lalamove", "Tote", ["wrong_item"]),
        order("a3", "ana", "lalamove", "Tote"),
    ]
    # A reference on two orders, and an old claim outside the window.
    orders += [order("p1", "x", "lalamove", "Tote", refs=["1046000000001"]), order("p2", "y", "lalamove", "Tote", refs=["1046000000001"])]
    old = order("z1", "old", "rush", "Tote", ["damage"])
    old["claims"][0]["created_at"] = day(60)
    orders.append(old)

    cards = {c["key"]: c for c in patterns.analyse(orders, now)}
    rush = cards["loss_concentration:courier:rush"]
    assert (rush["numerator"], rush["denominator"], rush["elevated"]) == (4, 7, True)  # the old claim is outside the window
    assert (rush["baseline"]["numerator"], rush["baseline"]["denominator"]) == (2, 25)
    assert "loss_concentration:courier:lalamove" not in cards  # busy courier, ordinary rate: no card
    vase = cards["loss_concentration:product:Vase"]
    assert (vase["numerator"], vase["denominator"]) == (3, 3) and not vase["elevated"]  # too few parcels to compare
    assert any("too little history" in line for line in vase["limitations"])
    assert any("no driver ID" in line for line in cards["loss_concentration:driver:D1"]["limitations"])
    ana = cards["recurring_customer_claims:customer:ana"]
    assert (ana["numerator"], ana["denominator"], ana["elevated"]) == (2, 3, False)  # the duplicate report counts once
    reuse = cards["payment_reference_reuse:reference:1046000000001"]
    assert {i["order_id"] for i in reuse["incidents"]} == {"p1", "p2"} and reuse["rate"] is None

    # A review holds while the linked cases are the same, and is superseded when they change.
    reviewed = {rush["key"]: {"status": "explained_operationally", "reason": "fragile stock", "incidents_hash": rush["incidents_hash"]}}
    assert patterns.with_review([rush], reviewed)[0]["review"]["status"] == "explained_operationally"
    changed = {**rush, "incidents_hash": "other"}
    shown = patterns.with_review([changed], reviewed)[0]
    assert shown["review"]["status"] == "new" and shown["previous_review"]["status"] == "superseded"


def test_rising_compares_the_last_week_with_the_three_before():
    from datetime import datetime, timedelta

    from domain import patterns

    now = datetime(2026, 10, 30, tzinfo=UTC)
    day = lambda n: now - timedelta(days=n)

    def receipt_order(oid, chat, days, check="recipient_name", result="fail"):
        claim = {"id": f"r_{oid}", "received_at": day(days), "checks": [{"check": check, "result": result, "detail": ""}]}
        return {
            "id": oid,
            "created_at": day(days),
            "source": {"conversation_id": chat},
            "manual_payment": {"claims": [claim]},
            "claims": [],
            "draft": {"items": []},
        }

    def damage_order(oid, days):
        claim = {"id": f"c_{oid}", "type": "damage", "created_at": day(days), "status": "open", "decision": None}
        return {"id": oid, "created_at": day(days + 2), "source": {"conversation_id": f"chat_{oid}"}, "claims": [claim], "draft": {"items": []}}

    def asked(chat, days):
        at = day(days).isoformat()
        return {"conversation_id": chat, "name": chat.title(), "signals": [{"kind": "account_request", "sources": [f"m_{chat}"], "times": [at]}]}

    # Receipts: 3 this week, 1 before; one more fail on a check that is not a fit with the order does not count.
    orders = [receipt_order(f"o{n}", f"r{n}", d) for n, d in enumerate([1, 3, 5, 12])]
    orders += [receipt_order("ok", "fine", 2, result="pass"), receipt_order("out", "screened", 2, check="screening")]
    # Damage: 2 this week is below the minimum, so no card however low the weeks before.
    orders += [damage_order("d1", 2), damage_order("d2", 4)]
    pressured = {"conversation_id": "r0", "name": "R0", "signals": [{"kind": "pressure", "sources": ["m"], "times": [day(1).isoformat()]}]}
    # Account requests: 3 this week but 6 in the 3 weeks before (2 a week), so not a rise.
    chats = [asked(f"a{n}", d) for n, d in enumerate([1, 2, 3, 9, 10, 15, 16, 22, 23])] + [pressured]

    cards = {c["entity"]["value"]: c for c in patterns.analyse(orders, now, chats=chats) if c["rule"] == "rising"}
    receipts = cards["receipt_misfit"]
    assert (receipts["numerator"], receipts["weeks"], receipts["weekly_before"]) == (3, [0, 0, 1, 3], 0.3)
    assert [i["order_id"] for i in receipts["incidents"]] == ["o0", "o1", "o2"] and receipts["with_pressure"] == 1
    assert "damage_reports" not in cards and "account_request" not in cards

    # One more account request this week makes it a rise: 4 against 2 a week.
    chats.append(asked("a9", 0.5))
    cards = {c["entity"]["value"]: c for c in patterns.analyse(orders, now, chats=chats) if c["rule"] == "rising"}
    assert cards["account_request"]["numerator"] == 4 and cards["account_request"]["incidents"][0]["name"] == "A9"


def test_missing_and_replaced_contents_count_against_the_driver_only_when_the_cause_could_be_theirs():
    from datetime import datetime, timedelta

    from domain import patterns

    now = datetime(2026, 10, 30, tzinfo=UTC)
    day = lambda n: now - timedelta(days=n)

    def order(oid, driver, claim=None, cause=None):
        f = {"id": f"f_{oid}", "status": "delivered", "courier": "lalamove", "driver_id": driver, "created_at": day(5), "events": []}
        claims = [
            {"id": f"c_{oid}", "type": claim, "fulfillment_id": f["id"], "created_at": day(2), "status": "resolved", "decision": {"cause": cause}}
        ]
        return {
            "id": oid,
            "created_at": day(6),
            "source": {},
            "fulfillment": f,
            "claims": claims if claim else [],
            "draft": {"items": [{"name": "Candle"}]},
        }

    orders = [order(f"a{n}", "D3", ("missing_item", "wrong_item")[n] if n < 2 else None) for n in range(6)]
    # Another driver's wrong item that was the seller's own packing error never counts against a driver.
    orders += [order(f"b{n}", "D1", "wrong_item" if n < 2 else None, "seller_error" if n < 2 else None) for n in range(6)]
    cards = {c["key"]: c for c in patterns.analyse(orders, now)}
    d3 = cards["contents_concentration:driver:D3"]
    assert (d3["numerator"], d3["denominator"], d3["elevated"]) == (2, 6, True)
    assert "contents_concentration:driver:D1" not in cards
    assert not any(k.startswith("contents_concentration:courier") for k in cards)  # only drivers are compared for contents
    assert "loss_concentration:driver:D3" not in cards  # missing contents are not damage
