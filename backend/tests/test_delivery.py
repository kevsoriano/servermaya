from datetime import UTC

import pytest

from core import config, db
from helpers import client, ready_order
from integrations import lalamove
from routes import webhooks


def test_dispatch_gate_and_delivery_events():
    from datetime import datetime, timedelta

    from domain import delivery
    from domain import orders as rules

    now = datetime(2026, 10, 2, tzinfo=UTC)
    order = ready_order()
    assert delivery.can_dispatch(order)
    for blocked in (
        {"payment": {"status": "pending", "version": 1}},
        {"status": "draft"},
        {"packing": {**order["packing"], "completed": None}},
        {"packing": {**order["packing"], "version": 0}},
    ):  # unpaid, unconfirmed, not packed, packed for an older version
        with pytest.raises(rules.Refused):
            delivery.start({**order, **blocked}, "f1", "simulator", "s", now)

    # Lalamove: a quote, locked while booking, refused once expired.
    quote = {"id": "q1", "expires_at": now + timedelta(minutes=10), "stop_ids": ["a", "b"]}
    order |= delivery.start(order, "f1", "lalamove", "s", now, quotation=quote)
    assert order["fulfillment"]["status"] == "quoted"
    order |= delivery.begin_booking(order, now)
    with pytest.raises(rules.Refused):
        delivery.begin_booking(order, now)  # a second click cannot book twice
    with pytest.raises(rules.Refused):
        delivery.release_booking(order, now)  # the quote could still turn into an order
    order |= delivery.booked(order, "LL1", "https://share", now)
    assert delivery.booked(order, "LL1", None, now) == {}  # the webhook and the API answer both arrive

    ev = lambda eid, status: delivery.event(eid, "provider", status.upper(), status, now, now)
    order |= delivery.apply_event(order, ev("e2", "picked_up"), driver_id="D9")
    assert delivery.apply_event(order, ev("e2", "picked_up")) == {}  # duplicate callback
    order |= delivery.apply_event(order, ev("e1", "driver_assigned"))  # late, out of order: kept, status stays
    f = order["fulfillment"]
    assert f["status"] == "picked_up" and [e["id"] for e in f["events"]] == ["lalamove:LL1:created", "e2", "e1"] and f["driver_id"] == "D9"
    with pytest.raises(rules.Refused):
        delivery.cancel(order, "s", "", now)  # already picked up
    with pytest.raises(rules.Refused):
        delivery.step(order, "delivered", "seller_reported", "s", now)  # Lalamove's status comes from Lalamove
    order |= delivery.apply_event(order, ev("e3", "delivered"))
    assert "receipt" not in order  # delivered is not received
    order |= delivery.acknowledge(order, "customer_link", now)
    assert order["receipt"]["status"] == "customer_confirmed"
    with pytest.raises(rules.Refused):
        rules.edit(order, rules.Edit(items=[]))  # a shipped order cannot change

    # The simulator labels every event as demo; another courier's are seller-reported.
    sim = ready_order() | delivery.start(ready_order(), "f2", "simulator", "s", now)
    sim |= delivery.step(sim, "picked_up", "demo", "s", now)
    assert {e["source"] for e in sim["fulfillment"]["events"]} == {"demo"} and sim["fulfillment"]["status"] == "picked_up"
    with pytest.raises(rules.Refused):
        delivery.step(sim, "driver_assigned", "demo", "s", now)  # never backwards

    assert delivery.parse_pin("https://maps.google.com/?q=14.6091,121.0223") == (14.6091, 121.0223)
    assert delivery.e164("0917 123 4567") == "+639171234567"
    with pytest.raises(rules.Refused):
        delivery.parse_pin("40.7, -74.0")  # not in the Philippines


def test_lalamove_webhook_verifies_and_links_by_quotation(monkeypatch):
    import json

    monkeypatch.setattr(config, "LALAMOVE_KEY", "pk")
    monkeypatch.setattr(config, "LALAMOVE_SECRET", "sk")
    stored, applied = [], []

    class Doc:
        def create(self, doc):
            if any(d is self for d, _ in stored):
                raise Exception("exists")
            stored.append((self, doc))

    docs = {}
    monkeypatch.setattr(
        db,
        "clients",
        lambda: (None, type("DB", (), {"collection": lambda self, n: type("C", (), {"document": lambda self, k: docs.setdefault(k, Doc())})()})()),
    )
    monkeypatch.setattr(webhooks, "shipment_ref", lambda oid, qid: "ref" if qid == "q1" or oid == "LL1" else None)
    monkeypatch.setattr(db, "change", lambda ref, rule: applied.append(ref))
    data = {"order": {"orderId": "LL1", "quotationId": "q1", "status": "PICKED_UP"}, "updatedAt": "2026-10-13T19:05.00Z"}
    signed = lambda d, sig=None: json.dumps(
        {
            "apiKey": "pk",
            "timestamp": 1,
            "eventId": "E1",
            "eventType": "ORDER_STATUS_CHANGED",
            "data": d,
            "signature": sig or lalamove.lalamove_signature("1", "POST", "/api/webhooks/lalamove", json.dumps(d, separators=(",", ":"))),
        }
    )
    assert client.post("/api/webhooks/lalamove", content=signed(data, "bad")).status_code == 200 and applied == []  # forged: ignored
    assert client.post("/api/webhooks/lalamove", content=signed(data)).status_code == 200 and applied == ["ref"]
    client.post("/api/webhooks/lalamove", content=signed(data))
    assert applied == ["ref"]  # the same event again changes nothing


def test_a_sandbox_booking_moves_on_by_itself_one_simulated_step_a_second():
    from datetime import UTC, datetime, timedelta

    from domain import delivery

    t0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    booked = delivery.event("lalamove:L1:created", "provider", "ORDER_CREATED", "booked", t0, t0)
    shipment = {"id": "f1", "courier": "lalamove", "status": "booked", "quotation": {"livemode": False}, "events": [booked], "driver_id": None}
    order = {"fulfillment": shipment}
    at = lambda s: t0 + timedelta(seconds=s)
    status = lambda o: (o.get("fulfillment") or order["fulfillment"])["status"]

    assert delivery.sandbox_progress(order, at(0.5)) == {}  # nothing due yet
    two = delivery.sandbox_progress(order, at(2))
    assert status(two) == "picked_up" and [e["source"] for e in two["fulfillment"]["events"][1:]] == ["demo", "demo"]
    done = {**order, **delivery.sandbox_progress(order, at(10))}
    assert status(done) == "delivered" and len(done["fulfillment"]["events"]) == 4
    assert delivery.sandbox_progress(done, at(20)) == {}  # applying again adds nothing
    assert done["fulfillment"]["events"][-1]["note"] == delivery.SANDBOX_NOTE  # labelled as simulated

    # never a live booking; and a real update further along is not moved back
    assert delivery.sandbox_progress({"fulfillment": {**shipment, "quotation": {"livemode": True}}}, at(10)) == {}
    real = delivery.event("lalamove:L1:poll:PICKED_UP", "provider", "PICKED_UP", "picked_up", at(0.2), at(0.2))
    ahead = {"fulfillment": {**shipment, "status": "picked_up", "events": [booked, real]}}
    after = delivery.sandbox_progress(ahead, at(1.5))  # driver_assigned is due, but the shipment is already picked up
    assert after["fulfillment"]["status"] == "picked_up"


def test_a_sandbox_booking_can_play_out_a_failed_or_cancelled_delivery_and_the_next_shipment_keeps_it():
    from datetime import UTC, datetime, timedelta

    from domain import delivery

    t0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    booked = delivery.event("lalamove:L1:created", "provider", "ORDER_CREATED", "booked", t0, t0)
    base = {"id": "f1", "courier": "lalamove", "status": "booked", "quotation": {"livemode": False}, "events": [booked], "driver_id": None}
    later = t0 + timedelta(seconds=10)
    failed = delivery.sandbox_progress({"fulfillment": {**base, "sandbox_outcome": "failed"}}, later)["fulfillment"]
    assert [e["status"] for e in failed["events"]] == ["booked", "driver_assigned", "picked_up", "failed"] and failed["status"] == "failed"
    assert failed["events"][-1]["note"].startswith("The rider could not deliver")
    cancelled = delivery.sandbox_progress({"fulfillment": {**base, "sandbox_outcome": "cancelled"}}, later)["fulfillment"]
    assert [e["status"] for e in cancelled["events"]] == ["booked", "driver_assigned", "cancelled"]

    # The outcome is kept only on a sandbox booking
    quote = {"expires_at": later, "livemode": True}
    live = {**base, "status": "quoted", "quotation": quote, "events": []}
    paid = {"status": "confirmed", "version": 1, "payment": {"status": "verified", "version": 1}, "packing": {"version": 1, "completed": {"at": t0}}}
    assert "sandbox_outcome" not in delivery.begin_booking({**paid, "fulfillment": live}, t0, "failed")["fulfillment"]

    # After a failed delivery a new one can start, and the failed one stays on record
    after = delivery.start({**paid, "fulfillment": failed}, "f2", "simulator", "seller", later)
    assert after["fulfillment"]["id"] == "f2" and after["fulfillment_history"] == [failed]
