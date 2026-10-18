import pytest
from fastapi import HTTPException

from core import auth, config, db
from helpers import client, fake_token
from integrations import messenger
from services import chats


def test_healthz_is_public():
    assert client.get("/api/healthz").status_code == 200


def test_missing_and_garbage_tokens_rejected():
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_only_anonymous_visitors_with_the_access_code_get_in(monkeypatch):
    from helpers import fake_google

    fake_google(monkeypatch, None)
    monkeypatch.setattr(auth, "_admitted", {})
    monkeypatch.setattr(config, "ACCESS_CODE", "judges-2026-orchid")
    h = {"Authorization": "Bearer x"}
    # No accounts: a Google sign-in, or a token from another project, never gets in.
    assert fake_token(monkeypatch, admitted=False, firebase={"sign_in_provider": "google.com"}).status_code == 401
    assert fake_token(monkeypatch, admitted=False, iss="https://securetoken.google.com/other").status_code == 401

    # An anonymous session that has not entered the code may only ask who it is and enter it.
    r = fake_token(monkeypatch, admitted=False)
    assert r.status_code == 200 and r.json()["admitted"] is False
    assert client.get("/api/conversations", headers=h).status_code == 403
    wrong = client.post("/api/access", json={"code": "guess"}, headers=h)
    assert wrong.status_code == 403 and "doesn't work" in wrong.json()["detail"]
    ok = client.post("/api/access", json={"code": " judges-2026-orchid "}, headers=h)  # pasted with spaces
    assert ok.status_code == 200 and ok.json()["admitted"] is True
    assert client.get("/api/me", headers=h).json()["admitted"] is True
    auth._admitted.clear()  # another Cloud Run instance: the stored admission still counts
    assert client.get("/api/me", headers=h).json()["admitted"] is True

    # A new code shuts out every session admitted with the old one, until it enters the new code.
    monkeypatch.setattr(config, "ACCESS_CODE", "judges-2026-lotus")
    assert client.get("/api/me", headers=h).json()["admitted"] is False
    assert client.get("/api/orders", headers=h).status_code == 403

    # Guessing stops after MAX_ATTEMPTS wrong codes; with no code set, nobody gets in.
    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/api/access", json={"code": "guess"}, headers=h)
    assert client.post("/api/access", json={"code": "judges-2026-lotus"}, headers=h).status_code == 429
    monkeypatch.setattr(config, "ACCESS_CODE", "")
    assert client.post("/api/access", json={"code": "anything"}, headers=h).status_code == 503


def test_visitors_reach_their_own_demo_chats_and_the_shops_messenger_inbox(monkeypatch):
    from seed import demo

    r = fake_token(monkeypatch)
    assert r.status_code == 200 and r.json()["demo"] is True

    mine, theirs = demo.new_id(), demo.new_id()
    threads = {mine: {"owner": "u1", "name": "Ana", "messages": []}, theirs: {"owner": "u2", "name": "Ben", "messages": []}}
    monkeypatch.setattr(chats, "demo_doc", lambda cid: threads[cid] if cid in threads else (_ for _ in ()).throw(HTTPException(404, "x")))
    found = {
        "o_mine": {"source": {"type": "demo", "owner": "u1"}},
        "o_theirs": {"source": {"type": "demo", "owner": "u2"}},
        "o_real": {"source": {"type": "messenger"}},
    }

    class Snap:
        def __init__(self, oid):
            self.exists, self._d = oid in found, found.get(oid)

        def to_dict(self):
            return self._d

    monkeypatch.setattr(db, "orders", lambda: type("C", (), {"document": lambda self, oid: type("D", (), {"get": lambda self: Snap(oid)})()})())
    monkeypatch.setattr(messenger, "fetch_messages", lambda cid: [])
    h = {"Authorization": "Bearer x"}
    assert client.get(f"/api/conversations/{mine}", headers=h).status_code == 200
    assert client.get(f"/api/conversations/{theirs}", headers=h).status_code == 404  # another visitor's demo chat
    assert client.get("/api/conversations/t_123", headers=h).status_code == 200  # the shop's Messenger thread
    monkeypatch.setattr(db, "loaded", lambda oid: (None, found[oid]))
    monkeypatch.setattr(db, "present", lambda snap: snap.to_dict())
    assert client.get("/api/orders/o_theirs", headers=h).status_code == 404
    assert client.get("/api/orders/o_mine", headers=h).status_code == 200
    assert client.get("/api/orders/o_real", headers=h).status_code == 200  # the shop's Messenger order
    # The settings are read-only for everyone, and show the demo shop's.
    assert client.put("/api/settings/gcash", headers=h, json={"name": "X", "mobile": "09171234567"}).status_code == 403
    assert client.put("/api/settings/dispatch", headers=h, json={}).status_code == 403
    assert client.post("/api/catalog", headers=h, json={}).status_code == 403
    assert client.post("/api/settings/gcash/qr", headers=h, content=b"x").status_code == 403
    assert client.post("/api/patterns/review", headers=h, json={}).status_code == 403
    assert client.get("/api/settings/gcash", headers=h).json() == {**config.DEMO_GCASH, "qr_image": True}
    qr = client.get("/api/settings/gcash/qr", headers=h)  # the demo shop's sample QR, stamped as one
    assert qr.status_code == 200 and qr.headers["content-type"] == "image/jpeg"
    assert client.get("/api/settings/dispatch", headers=h).json() == config.DEMO_PICKUP
    assert [auth.visible(o, {"uid": "u1", "demo": True}) for o in found.values()] == [True, False, True]


def test_demo_ai_budget_limits_each_visitor_and_everyone(monkeypatch):
    from seed import demo

    monkeypatch.setattr(demo, "AI_PER_VISITOR", 250)
    assert demo.over_budget(0, 0, 1) is None
    assert demo.over_budget(demo.AI_PER_VISITOR - 3, 0, 3) is None  # a receipt read of 3 images that just fits
    assert "limit of AI reads" in demo.over_budget(demo.AI_PER_VISITOR - 2, 0, 3)
    assert "all visitors" in demo.over_budget(0, demo.AI_PER_DAY, 1)
    assert "all visitors" in demo.over_budget(demo.AI_PER_VISITOR, demo.AI_PER_DAY, 1)  # the shared limit is the one to report
    monkeypatch.setattr(demo, "AI_PER_VISITOR", None)  # switched off: only the shared limit applies
    assert demo.over_budget(10_000, 0, 1) is None and "all visitors" in demo.over_budget(10_000, demo.AI_PER_DAY, 1)


def test_demo_ai_budget_counts_per_visitor_and_for_everyone_without_transactions(monkeypatch):
    from helpers import fake_google
    from seed import demo

    store, _ = fake_google(monkeypatch, None)
    monkeypatch.setattr(db.clients()[1], "transaction", lambda: (_ for _ in ()).throw(AssertionError("no transaction")), raising=False)
    monkeypatch.setattr(demo, "AI_PER_VISITOR", 5)
    a, b = {"uid": "a", "demo": True}, {"uid": "b", "demo": True}
    auth.ai_budget(a, 3)
    auth.ai_budget(a, 2)  # exactly at the visitor's limit
    with pytest.raises(HTTPException) as over:
        auth.ai_budget(a, 1)
    assert over.value.status_code == 429 and "limit of AI reads" in over.value.detail
    auth.ai_budget(b, 4)  # another visitor has their own count
    counts = {p.rsplit("/", 1)[1]: d["count"] for p, d in store.docs.items() if "/demoAi" in p}
    day = next(k for k in counts if "_" not in k)
    assert counts == {f"a_{day}": 5, f"b_{day}": 4, day: 9}  # a refused call is not counted
