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
