"""Shared by the test modules: the app client, a fake Firebase sign-in, and a confirmed, paid, packed order."""

from datetime import UTC

from fastapi.testclient import TestClient
from google.oauth2 import id_token

import main
from core import config

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
        "payment": {"status": "verified", "version": 1, "amount": 50000, "paid_amount": 50000, "payment_id": "pay_1"},
        "packing": {"version": 1, "rows": [], "photos": [{"id": "p1"}], "completed": {"by": "s", "at": at}},
        **extra,
    }
