"""Returns analysis: driver records, courier proofs, the custody check, causes, returns and the refund they hold."""

from datetime import UTC, datetime, timedelta

import pytest

from domain import claims as cases
from domain import custody, delivery, patterns, refunds, returns
from domain import orders as rules
from helpers import ready_order
from integrations import lalamove

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def sandbox(outcome: str = "delivered") -> dict:
    booked = delivery.event("lalamove:L1:created", "provider", "ORDER_CREATED", "booked", T0, T0)
    return {
        "id": "f1",
        "courier": "lalamove",
        "status": "booked",
        "quotation": {"livemode": False, "service": "MOTORCYCLE", "price": 12000},
        "events": [booked],
        "driver_id": None,
        "driver": None,
        "sandbox_outcome": outcome,
        "created_at": T0,
    }


def test_drivers_say_where_they_came_from_and_lalamoves_is_never_replaced():
    done = delivery.sandbox_steps(sandbox(), T0 + timedelta(seconds=10))
    sim = done["driver"]
    assert sim["source"] == "simulated" and sim["driver_id"].startswith("sim-") and sim["name"].startswith("Sample Rider")
    assert delivery.sandbox_steps(sandbox(), T0 + timedelta(seconds=10))["driver"]["driver_id"] == sim["driver_id"]  # same every time

    # A real Lalamove match replaces the simulated driver, and both stay in the history
    real = delivery.driver_record("provider", "80557", "Juan D.", "NAB ***4", T0)
    order = {"fulfillment": done}
    ev = delivery.event("e9", "provider", "ON_GOING", "driver_assigned", T0, T0)
    order |= delivery.apply_event(order, ev, driver=real)
    f = order["fulfillment"]
    assert f["driver"]["source"] == "provider" and f["driver_id"] == "80557"
    assert [d["source"] for d in f["drivers"]] == ["simulated", "provider"]

    # Nothing else replaces it, and the seller cannot rename a driver Lalamove named
    assert delivery.with_driver(f, delivery.sandbox_driver("x", T0)) == f
    with pytest.raises(rules.Refused):
        delivery.name_driver(order, "f1", "Someone", "", "seller", T0)

    # Another courier's rider, named by the seller, is grouped on what they typed
    other = ready_order() | delivery.start(ready_order(), "f2", "other", "s", T0, tracking="Shop rider")
    other |= delivery.name_driver(other, "f2", "Mang Ben", "XYZ 123", "seller", T0)
    d = other["fulfillment"]["driver"]
    assert d["source"] == "seller_reported" and d["driver_id"] == "seller:mangben|xyz123"


def test_sandbox_outcomes_leave_sample_pickup_and_drop_off_photos():
    later = T0 + timedelta(seconds=10)
    damaged = delivery.sandbox_steps(sandbox("damaged"), later)
    assert damaged["status"] == "delivered"
    assert damaged["pop"]["image"] == {"sample": "courier-pickup-intact"} and damaged["pop"]["source"] == "demo"
    assert damaged["pod"]["image"] == {"sample": "courier-dropoff-crushed"}
    assert delivery.sandbox_steps(sandbox(), later)["pod"]["image"] == {"sample": "courier-dropoff-intact"}
    failed = delivery.sandbox_steps(sandbox("failed"), later)
    assert failed["pod"]["status"] == "FAILED" and failed["pod"]["image"] is None
    assert delivery.sandbox_steps(sandbox("cancelled"), later).get("pop") is None


def test_end_reasons_are_the_sellers_and_only_for_ended_shipments():
    order = ready_order() | delivery.start(ready_order(), "f1", "other", "s", T0, tracking="J&T 1")
    with pytest.raises(rules.Refused):
        delivery.end_reason(order, "f1", "driver", "driver_no_show", "", "s", T0)  # still active
    order |= delivery.step(order, "failed", "seller_reported", "s", T0)
    with pytest.raises(rules.Refused):
        delivery.end_reason(order, "f1", "customer", "other", "  ", "s", T0)  # "other" needs a note
    order |= delivery.end_reason(order, "f1", "customer", "customer_unreachable", "", "s", T0)
    assert order["fulfillment"]["end_reason"]["who"] == "customer"


def test_lalamove_proofs_are_read_from_the_order_stops():
    # The shape of the POD_STATUS_CHANGED event the sandbox sent on 2026-10-13
    data = {
        "stops": [
            {"name": "Shop", "POD": None},
            {"name": "Ana", "POD": {"status": "SIGNED", "image": "http://sg-oimg-pre.lalamove.com/x.png", "deliveredAt": "2026-10-13T19:11.00Z"}},
        ]
    }
    found = lalamove.proofs(data)
    assert "pop" not in found and found["pod"]["status"] == "SIGNED" and found["pod"]["at"] == datetime(2026, 10, 13, 19, 11, tzinfo=UTC)
    assert lalamove.proof_image("https://evil.example.com/x.png") is None  # only Lalamove's hosts
    assert lalamove.proof_image("ftp://sg-oimg-pre.lalamove.com/x.png") is None


def shipped(pop_check=None, pod_check=None, claim_type="damage", **claim_extra) -> tuple[dict, dict]:
    f = {
        **sandbox(),
        "status": "delivered",
        "pop": delivery.proof("PICKED_UP", T0, "demo", {"sample": "a"}) | ({"check": pop_check} if pop_check else {}),
        "pod": delivery.proof("DELIVERED", T0, "demo", {"sample": "b"}) | ({"check": pod_check} if pod_check else {}),
    }
    rows = [{"line": 1, "expected": {}, "auto_result": "matched", "manual": None}]
    order = ready_order(fulfillment=f, packing={"version": 1, "rows": rows, "photos": [{"id": "p1"}], "completed": {"by": "s", "at": T0}})
    c = {"id": "c1", "type": claim_type, "fulfillment_id": "f1", "created_at": T0, "status": "open", "review": None, "decision": None, "history": []}
    c |= claim_extra
    return {**order, "claims": [c]}, c


INTACT = {"parcel_visible": True, "outer_damage": "none", "seal_intact": True, "label_match": None}
CRUSHED = {"parcel_visible": True, "outer_damage": "major", "seal_intact": True, "label_match": None}


def test_custody_check_suggests_a_cause_from_the_first_break_and_never_from_missing_evidence():
    order, c = shipped(INTACT, CRUSHED)
    result = custody.check(order, c)
    assert result["first_break"] == "dropped_off" and result["suggested_cause"] == "courier_transit"
    assert "[COC]" in custody.record(result) and "Sample Rider" not in custody.record(result)  # no driver names for Gemini

    # No pickup photo check: damaged at drop-off is not held against the courier
    order, c = shipped(None, CRUSHED)
    assert custody.check(order, c)["suggested_cause"] == "unknown"
    # Already damaged at pickup: before the courier carried it
    order, c = shipped(CRUSHED, CRUSHED)
    assert custody.check(order, c)["suggested_cause"] == "seller_error"
    # Intact box at drop-off: transit or packaging, the seller decides
    order, c = shipped(INTACT, INTACT)
    assert custody.check(order, c)["suggested_cause"] == "unknown"
    # A conflict the seller confirmed by hand at packing, then a wrong item: the seller's error
    order, c = shipped(INTACT, INTACT, "wrong_item")
    order["packing"]["rows"][0] |= {"auto_result": "conflict", "manual": {"by": "s", "reason": "looked fine"}}
    assert custody.check(order, c)["suggested_cause"] == "seller_error"
    # A cut in the unboxing video is a problem with that evidence, not with the parcel
    order, c = shipped(INTACT, INTACT, video_checks={"CV1": {"continuous": False, "sealed_at_start": True, "label_match": True}})
    opened = next(s for s in custody.check(order, c)["steps"] if s["step"] == "opened")
    assert opened["status"] == "problem"


def test_the_decision_keeps_the_sellers_cause_beside_the_suggested_one():
    order, c = shipped(INTACT, CRUSHED)
    body = cases.DecisionIn(action="refund", reason="cracked in transit", cause="courier_transit")
    order |= cases.decide(order, "c1", body, "seller", T0, suggested="courier_transit")
    d = order["claims"][0]["decision"]
    assert (d["cause"], d["suggested_cause"]) == ("courier_transit", "courier_transit")
    order |= cases.set_cause(order, "c1", "unknown", "the box was double-wrapped", "seller", T0)
    d = order["claims"][0]["decision"]
    assert d["cause"] == "unknown" and d["cause_history"][0]["from"] == "courier_transit"


def test_a_refund_on_a_returned_item_waits_for_the_item():
    order, c = shipped(INTACT, CRUSHED)
    with pytest.raises(rules.Refused):
        returns.request(order, "r1", "c1", "seller", T0)  # nothing approved yet
    order |= cases.decide(order, "c1", cases.DecisionIn(action="refund", reason="cracked"), "seller", T0)
    order |= returns.request(order, "r1", "c1", "seller", T0)
    with pytest.raises(rules.Refused):
        returns.request(order, "r2", "c1", "seller", T0)  # one return per claim

    manual = refunds.ManualIn(claim_id="c1", amount=50000, method="outside_paymongo", reference="GC1")
    with pytest.raises(rules.Refused, match="waits for the returned item"):
        refunds.manual(order, "op1", manual, "seller", T0)
    with pytest.raises(rules.Refused):
        returns.inspect(order, "r1", returns.InspectionIn(result="same_item", condition="ok"), "seller", T0)  # not back yet

    # The customer sends it back themselves and says so: still held until the seller has it in hand
    order |= returns.sent(order, "r1", "J&T", "JT123", "seller", T0)
    assert order["returns"][0]["sent"]["source"] == "customer_reported"
    assert returns.holds_refund(order, "c1")
    order |= returns.receive(order, "r1", "seller", T0)
    assert returns.holds_refund(order, "c1") is None
    order |= refunds.manual(order, "op1", manual, "seller", T0)
    assert order["refunds"][-1]["status"] == "seller_reported"

    # Inspected: a different item is a problem at the last step, and the cause stays the seller's call
    with pytest.raises(rules.Refused):
        returns.inspect(order, "r1", returns.InspectionIn(result="different_item", condition="a white mug"), "seller", T0)  # no photo
    order |= returns.add_photo(order, "r1", {"id": "rp1"})
    order |= returns.inspect(order, "r1", returns.InspectionIn(result="different_item", condition="a white mug", weight_g=310), "seller", T0)
    returned = next(s for s in custody.check(order, order["claims"][0])["steps"] if s["step"] == "returned")
    assert returned["status"] == "problem" and order["returns"][0]["status"] == "inspected"


def test_the_customer_sends_a_return_and_the_seller_can_receive_it_without_tracking():
    order, _ = shipped(INTACT, CRUSHED)
    order |= cases.decide(order, "c1", cases.DecisionIn(action="replacement", reason="cracked"), "seller", T0)
    order |= returns.request(order, "r1", "c1", "seller", T0)
    with pytest.raises(rules.Refused):
        returns.sent(order, "r1", " ", "", "seller", T0)  # the courier is needed
    order |= returns.sent(order, "r1", "LBC", "", "seller", T0)
    order |= returns.sent(order, "r1", "LBC", "LBC-555", "seller", T0)  # corrected: the first stays on record
    r = order["returns"][0]
    assert r["sent"]["tracking"] == "LBC-555" and r["sent_history"][0]["tracking"] is None
    order |= returns.receive(order, "r1", "seller", T0)
    with pytest.raises(rules.Refused):
        returns.sent(order, "r1", "LBC", "x", "seller", T0)  # already back
    assert "fulfillment" in order and order["fulfillment"]["status"] == "delivered"  # the delivery is untouched


def test_patterns_leave_seller_errors_out_of_courier_and_driver_rates():
    now = T0 + timedelta(days=1)
    rows = []
    for n in range(12):
        f = {"id": f"f{n}", "status": "delivered", "courier": "lalamove", "created_at": T0, "events": [], "quotation": {"service": "MOTORCYCLE"}}
        f |= {
            "driver": delivery.driver_record("simulated", "sim-2" if n < 6 else "sim-1", "Sample Rider", None, T0),
            "driver_id": "sim-2" if n < 6 else "sim-1",
        }
        cause = "seller_error" if n < 3 else None
        claims = (
            [
                {
                    "id": f"c{n}",
                    "type": "damage",
                    "fulfillment_id": f["id"],
                    "created_at": T0,
                    "status": "resolved",
                    "decision": {"action": "refund", "cause": cause},
                }
            ]
            if n < 3
            else []
        )
        rows.append(
            {
                "id": f"o{n}",
                "created_at": T0,
                "source": {},
                "fulfillment": f,
                "claims": claims,
                "draft": {"items": [{"name": "Mug", "variant": None}]},
            }
        )
    cards = {c["key"]: c for c in patterns.analyse(rows, now)}
    assert "loss_concentration:driver:sim-2" not in cards  # all three were the seller's errors
    for r in rows[:3]:
        r["claims"][0]["decision"]["cause"] = "courier_transit"
    driver = {c["key"]: c for c in patterns.analyse(rows, now)}["loss_concentration:driver:sim-2"]
    assert driver["numerator"] == 3 and any("Simulated sandbox drivers" in line for line in driver["limitations"])
    assert driver["entity"]["label"].endswith("(simulated)")

    summary = patterns.returns_summary(rows, now, 30)
    assert summary["delivered"] == 12 and summary["causes"]["courier_transit"]["count"] == 3
