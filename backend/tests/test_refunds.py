from datetime import UTC

import pytest

from helpers import ready_order


def test_refund_balance_lock_and_reconcile():
    from datetime import datetime, timedelta

    from domain import claims as cases
    from domain import orders as rules
    from domain import refunds

    now = datetime(2026, 10, 4, tzinfo=UTC)
    order = ready_order()
    order |= cases.open_claim(order, "c1", cases.ClaimIn(type="damage", narrative="cracked"), "seller", "s", now)
    with pytest.raises(rules.Refused):
        refunds.reserve(order, "op1", refunds.RefundIn(claim_id="c1", amount=100), "s", now)  # nothing approved yet
    order |= cases.decide(order, "c1", cases.DecisionIn(action="partial_refund", amount=20000, reason="r"), "s", now)

    order |= refunds.reserve(order, "op1", refunds.RefundIn(claim_id="c1", amount=15000), "s", now)
    assert refunds.refundable(order) == 35000 and cases.claim(order, "c1")["status"] == "executing"
    with pytest.raises(rules.Refused):
        refunds.reserve(order, "op2", refunds.RefundIn(claim_id="c1", amount=1000), "s", now)  # one submission at a time

    # PayMongo refuses QR Ph refunds through the API: the amount is released for the manual route.
    order |= refunds.submitted(order, "op1", {"refused": "Refunds are not allowed for payments with source type qrph."}, now)
    assert order["refunds"][0]["status"] == "api_refused" and refunds.refundable(order) == 50000
    with pytest.raises(rules.Refused):
        refunds.manual(
            order, "op2", refunds.ManualIn(claim_id="c1", amount=25000, method="outside_paymongo", reference="x"), "s", now
        )  # over approval
    with pytest.raises(rules.Refused):
        refunds.manual(order, "op2", refunds.ManualIn(claim_id="c1", amount=20000, method="outside_paymongo"), "s", now)  # needs a reference
    order |= refunds.manual(order, "op2", refunds.ManualIn(claim_id="c1", amount=20000, method="outside_paymongo", reference="GC123"), "s", now)
    assert order["refunds"][-1]["status"] == "seller_reported" and refunds.refundable(order) == 30000
    order |= cases.resolve(order, "c1", "", "s", now)
    assert cases.claim(order, "c1")["resolution"]["how"] == "seller-reported refund"

    # No answer: the attempt is uncertain until PayMongo's list settles it.
    o2 = ready_order()
    o2 |= cases.open_claim(o2, "c1", cases.ClaimIn(type="damage", narrative="x"), "seller", "s", now)
    o2 |= cases.decide(o2, "c1", cases.DecisionIn(action="refund", reason="r"), "s", now)
    o2 |= refunds.reserve(o2, "op1", refunds.RefundIn(claim_id="c1", amount=50000), "s", now)
    o2 |= refunds.submitted(o2, "op1", {}, now)
    assert o2["refunds"][0]["status"] == "uncertain" and refunds.refundable(o2) == 0
    landed = [{"id": "ref_1", "amount": 50000, "status": "pending", "metadata": {"operation_id": "op1"}}]
    o2 |= refunds.reconcile(o2, landed, now)
    assert (o2["refunds"][0]["status"], o2["refunds"][0]["provider_refund_id"]) == ("pending", "ref_1")
    assert refunds.reconcile(o2, landed, now) == {}  # repeating changes nothing
    o3 = ready_order(refunds=[{"id": "op9", "amount": 100, "status": "uncertain", "provider_refund_id": None, "claim_id": None}])
    assert refunds.reconcile(o3, [], now)["refunds"][0]["status"] == "not_found"  # it never landed: the amount is released
    stalled = ready_order(refunds=[{"id": "op8", "amount": 100, "status": "submitting", "provider_refund_id": None, "claim_id": None, "at": now}])
    assert refunds.reconcile(stalled, [], now + timedelta(seconds=30)) == {}  # may still be in flight
    assert refunds.reconcile(stalled, [], now + timedelta(minutes=5))["refunds"][0]["status"] == "not_found"
