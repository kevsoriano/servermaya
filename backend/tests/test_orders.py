from datetime import UTC

import pytest


def test_order_versioning_and_confirmation():
    from datetime import datetime

    from domain import orders as rules

    src = lambda v: {"value": v, "sources": ["m1"]}
    item = {"name": "mug", "variant": "blue", "quantity": 2, "unit_price": 18000, "sources": ["m1"]}
    draft = {
        "items": [item],
        "total": 36000,
        "claims": [],
        "clarification_questions": [],
        "recipient_name": src("Ana"),
        "phone": src("0917"),
        "address": {"value": None, "sources": []},
        "delivery_instructions": {"value": None, "sources": []},
    }
    order = {"status": "draft", "version": 1, "draft": draft, "confirmations": [], "confirm_token": None}
    now = datetime.now(UTC)
    body = lambda **k: rules.Edit(
        **{"items": [{k2: item[k2] for k2 in ("name", "variant", "quantity", "unit_price")}], "recipient_name": "Ana", "phone": "0917", **k}
    )

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
