from datetime import UTC

import pytest

from integrations import paymongo


def test_payment_reconciliation():
    from datetime import datetime

    from domain import orders as rules

    now = datetime.now(UTC)
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
    assert paymongo.valid_signature(f"t=1700000000,te={sig},li=", raw, "whsk")
    assert not paymongo.valid_signature(f"t=1700000000,te={sig},li=", raw + b" ", "whsk")
    assert not paymongo.valid_signature(f"t=1700000001,te={sig},li=", raw, "whsk")
    assert not paymongo.valid_signature("", raw, "whsk") and not paymongo.valid_signature("t=1,te=,li=", raw, "whsk")
