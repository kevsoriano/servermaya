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


def test_allowlisted_user_passes(monkeypatch):
    r = fake_token(monkeypatch, email="OK@example.com")
    assert r.status_code == 200 and r.json()["email"] == "ok@example.com"


def test_rejections(monkeypatch):
    assert fake_token(monkeypatch, email="other@example.com").status_code == 403
    assert fake_token(monkeypatch, email_verified=False).status_code == 403
    assert fake_token(monkeypatch, iss="https://securetoken.google.com/other").status_code == 401


def test_demo_visitors_only_reach_their_own_demo_chats_and_orders(monkeypatch):
    from seed import demo

    anon = {"firebase": {"sign_in_provider": "anonymous"}, "email": None, "email_verified": False}
    assert fake_token(monkeypatch, email="ok@example.com").json()["demo"] is False
    r = fake_token(monkeypatch, **anon)
    assert r.status_code == 200 and r.json()["demo"] is True  # no allowlist needed

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
    monkeypatch.setattr(messenger, "graph", lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo visitors never reach Messenger")))
    h = {"Authorization": "Bearer x"}
    assert client.get(f"/api/conversations/{mine}", headers=h).status_code == 200
    assert client.get(f"/api/conversations/{theirs}", headers=h).status_code == 404
    assert client.get("/api/conversations/t_123", headers=h).status_code == 404  # a real Messenger thread
    monkeypatch.setattr(db, "loaded", lambda oid: (None, found[oid]))
    assert client.get("/api/orders/o_theirs/patterns", headers=h).status_code == 404
    assert client.get("/api/orders/o_real/patterns", headers=h).status_code == 404
    monkeypatch.setattr(db, "present", lambda snap: snap.to_dict())
    assert client.get("/api/orders/o_mine", headers=h).status_code == 200
    assert client.get("/api/orders/o_real", headers=h).status_code == 404
    assert client.post("/api/orders/o_real/refunds", headers=h, json={}).status_code == 404
    assert client.put("/api/settings/gcash", headers=h, json={"name": "X", "mobile": "09171234567"}).status_code == 403
    assert client.put("/api/settings/dispatch", headers=h, json={}).status_code == 403
    assert client.post("/api/patterns/review", headers=h, json={}).status_code == 403
    assert client.get("/api/settings/gcash", headers=h).json() == {**config.DEMO_GCASH, "qr_image": True}
    qr = client.get("/api/settings/gcash/qr", headers=h)  # the demo shop's sample QR, stamped as one
    assert qr.status_code == 200 and qr.headers["content-type"] == "image/jpeg"
    assert client.get("/api/settings/dispatch", headers=h).json() == config.DEMO_PICKUP

    me, seller = {"uid": "u1", "demo": True}, {"uid": "s1", "demo": False}
    assert [auth.visible(o, me) for o in found.values()] == [True, False, False]
    assert [auth.visible(o, seller) for o in found.values()] == [False, False, True]


def test_demo_ai_budget_limits_visitors_and_the_whole_demo_but_never_sellers(monkeypatch):
    from seed import demo

    monkeypatch.setattr(demo, "AI_PER_VISITOR", 250)
    assert demo.over_budget(0, 0, 1) is None
    assert demo.over_budget(demo.AI_PER_VISITOR - 3, 0, 3) is None  # a receipt read of 3 images that just fits
    assert "limit of AI reads" in demo.over_budget(demo.AI_PER_VISITOR - 2, 0, 3)
    assert "all visitors" in demo.over_budget(0, demo.AI_PER_DAY, 1)
    assert "all visitors" in demo.over_budget(demo.AI_PER_VISITOR, demo.AI_PER_DAY, 1)  # the shared limit is the one to report
    monkeypatch.setattr(demo, "AI_PER_VISITOR", None)  # switched off: only the shared limit applies
    assert demo.over_budget(10_000, 0, 1) is None and "all visitors" in demo.over_budget(10_000, demo.AI_PER_DAY, 1)

    # A seller is never counted: no Firestore read or write at all.
    monkeypatch.setattr(db, "store_doc", lambda: (_ for _ in ()).throw(AssertionError("sellers are not counted")))
    auth.ai_budget({"uid": "s", "email": "s@example.com"}, 5)


def test_demo_ai_budget_retries_when_two_calls_take_the_counters_at_once(monkeypatch):
    from google.api_core.exceptions import Aborted

    reads, writes = [], []

    class Doc:
        def __init__(self, key="store"):
            self.key = key

        def collection(self, name):
            return self

        def document(self, key):
            return Doc(key)

        def get(self, transaction):
            reads.append(self.key)
            if len(reads) == 1:
                raise Aborted("cross-transaction contention")  # the other call holds the counters
            return type("Snap", (), {"to_dict": lambda self: {}})()

    tx = type("Tx", (), {"set": lambda self, ref, data: writes.append((ref.key, data["count"]))})()
    monkeypatch.setattr(auth.firestore, "transactional", lambda f: f)
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    monkeypatch.setattr(db, "store_doc", lambda: Doc())
    monkeypatch.setattr(db, "clients", lambda: (None, type("Client", (), {"transaction": lambda self: tx})()))
    auth.ai_budget({"uid": "v1", "demo": True}, 2)
    assert writes[0] == ("v1", 2) and len(writes) == 2  # counted once, after the retry

    reads.clear()
    monkeypatch.setattr(Doc, "get", lambda self, transaction: (_ for _ in ()).throw(Aborted("still busy")))
    with pytest.raises(HTTPException) as busy:
        auth.ai_budget({"uid": "v1", "demo": True}, 1)
    assert busy.value.status_code == 503
