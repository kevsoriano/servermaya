"""Shared by the test modules: the app client, a fake Firebase sign-in, a confirmed, paid, packed order, and in-memory Google services."""

import copy
from datetime import UTC

from fastapi.testclient import TestClient
from google.api_core.exceptions import Conflict, NotFound, PreconditionFailed
from google.oauth2 import id_token

import main
from core import config, db

client = TestClient(main.app)


ISS = f"https://securetoken.google.com/{config.PROJECT_ID}"


def fake_token(monkeypatch, **claims):
    base = {"sub": "u1", "iss": ISS, "email": "ok@example.com", "email_verified": True}
    monkeypatch.setattr(id_token, "verify_firebase_token", lambda *a, **k: {**base, **claims})
    monkeypatch.setattr(config, "ALLOWLIST", {"ok@example.com"})
    return client.get("/api/me", headers={"Authorization": "Bearer x"})


def ready_order(**extra):
    """A confirmed order, paid by PayMongo and packed, for version 1."""
    from datetime import datetime

    at = datetime(2026, 10, 1, tzinfo=UTC)
    item = {"name": "Mug", "variant": "blue", "quantity": 1, "unit_price": 50000, "sources": []}
    field = lambda v: {"value": v, "sources": []}
    return {
        "id": "o1",
        "status": "confirmed",
        "version": 1,
        "created_at": at,
        "source": {"type": "messenger", "conversation_id": "t1"},
        "draft": {
            "items": [item],
            "total": 50000,
            "recipient_name": field("Ana"),
            "phone": field("0917 123 4567"),
            "address": field("Cubao"),
            "delivery_instructions": field(None),
        },
        "confirmations": [{"version": 1, "kind": "customer", "by": "customer", "at": at}],
        "payment": {"status": "verified", "version": 1, "amount": 50000, "paid_amount": 50000, "payment_id": "pay_1", "livemode": False},
        "packing": {"version": 1, "rows": [], "photos": [{"id": "p1"}], "completed": {"by": "s", "at": at}},
        **extra,
    }


class FakeFirestore:
    """In-memory Firestore for route tests: documents, equality queries and transactions, which is all the routes use.
    Reads and writes copy, so a route never shares a dict with the store. Updates are top-level keys, as every rule returns."""

    def __init__(self):
        self.docs = {}  # path -> dict

    def collection(self, name, prefix=""):
        return FakeCollection(self, f"{prefix}{name}")

    def transaction(self):
        return FakeTransaction()


class FakeCollection:
    def __init__(self, db, path, filters=()):
        self.db, self.path, self.filters = db, path, filters

    def document(self, doc_id):
        return FakeDoc(self.db, f"{self.path}/{doc_id}")

    def where(self, filter):
        return FakeCollection(self.db, self.path, self.filters + (filter,))

    def limit(self, n):
        return self  # every query here asks for at most one match of a unique value

    def stream(self):
        def value(d, path):
            for k in path.split("."):
                d = d.get(k) if isinstance(d, dict) else None
            return d

        for path in sorted(self.db.docs):
            parent, _, _ = path.rpartition("/")
            doc = self.db.docs[path]
            if parent == self.path and all(value(doc, f.field_path) == f.value for f in self.filters):
                yield FakeDoc(self.db, path).get()


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path, self.id = db, path, path.rpartition("/")[2]

    def collection(self, name):
        return self.db.collection(name, prefix=self.path + "/")

    def get(self, transaction=None):
        return FakeSnap(self, copy.deepcopy(self.db.docs.get(self.path)))

    def set(self, data, merge=False):
        self.db.docs[self.path] = {**(self.db.docs.get(self.path, {}) if merge else {}), **copy.deepcopy(data)}

    def update(self, data):
        if self.path not in self.db.docs:
            raise NotFound(self.path)
        self.db.docs[self.path].update(copy.deepcopy(data))

    def create(self, data):
        if self.path in self.db.docs:
            raise Conflict(self.path)
        self.set(data)


class FakeSnap:
    def __init__(self, ref, data):
        self.reference, self.id, self._data = ref, ref.id, data
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeTransaction:
    def update(self, ref, data):
        ref.update(data)

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


class FakeBucket:
    def __init__(self):
        self.files = {}

    def blob(self, path):
        bucket = self

        class Blob:
            def upload_from_string(self, data, content_type=None, if_generation_match=None):
                if if_generation_match == 0 and path in bucket.files:
                    raise PreconditionFailed(path)
                bucket.files[path] = data

            def download_as_bytes(self):
                return bucket.files[path]

        return Blob()


def fake_google(monkeypatch, gemini=None) -> tuple[FakeFirestore, FakeBucket]:
    """Swap Firestore, Storage and (optionally) Gemini for in-memory fakes. Transactions run once, with no retries."""
    store, bucket = FakeFirestore(), FakeBucket()
    monkeypatch.setattr(db, "clients", lambda: (gemini, store))
    monkeypatch.setattr(db, "bucket", lambda: bucket)
    monkeypatch.setattr(db.firestore, "transactional", lambda f: f)
    return store, bucket
