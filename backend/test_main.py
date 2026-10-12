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

    monkeypatch.setattr(main.requests, "get", lambda url, **k: calls.append((url, k)) or Resp())
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
