"""Whole flows through the HTTP routes, on in-memory Firestore and Storage. PayMongo, Lalamove, Messenger and Gemini are stubbed
at the module the routes call, so what is tested is the wiring: routes, services, rules and the transaction around them."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

from core import config
from domain import claim_agent
from domain import claims as cases
from domain import packing as pack
from helpers import client, fake_google, fake_token, ready_order
from integrations import lalamove, memory, messenger, paymongo
from routes import dispatch
from seed import demo, samples

SELLER = {"Authorization": "Bearer x"}
asked = []


def put_order(store, **order):
    store.collection("stores").document(config.STORE).collection("orders").document("o1").set(ready_order(confirm_token="tok", **order))


def saved(store) -> dict:
    return store.docs[f"stores/{config.STORE}/orders/o1"]


def signed(event: dict, secret="whsk") -> dict:
    raw = json.dumps({"data": {"attributes": event}})
    sig = hmac.new(secret.encode(), f"1.{raw}".encode(), hashlib.sha256).hexdigest()
    return {"content": raw, "headers": {"Paymongo-Signature": f"t=1,te={sig},li="}}


@pytest.fixture
def seller(monkeypatch):
    assert fake_token(monkeypatch).status_code == 200


def test_customer_link_confirms_pays_and_the_webhook_verifies(monkeypatch):
    store, _ = fake_google(monkeypatch)
    put_order(store, status="awaiting_confirmation", confirmations=[], payment=None, packing=None)
    monkeypatch.setattr(config, "PAYMONGO_KEY", "sk_test_x")
    monkeypatch.setattr(config, "PAYMONGO_WEBHOOK_SECRET", "whsk")
    calls, paid = [], []

    def fake_paymongo(method, path, attributes=None):
        calls.append((method, path))
        if (method, path) == ("POST", "checkout_sessions"):
            assert attributes["metadata"] == {"order_id": "o1", "order_version": "1"}
            return {"id": "cs_1", "attributes": {"checkout_url": "https://pay/1", "livemode": False}}
        assert (method, path) == ("GET", "checkout_sessions/cs_1")
        payments = [{"id": "pay_1", "attributes": {"status": "paid", "amount": 50000, "currency": "PHP", "paid_at": 1}}] if paid else []
        return {"id": "cs_1", "attributes": {"status": "active", "metadata": {"order_id": "o1"}, "payments": payments}}

    monkeypatch.setattr(paymongo, "paymongo", fake_paymongo)

    assert client.get("/api/public/orders/nope").status_code == 404
    view = client.get("/api/public/orders/tok").json()
    assert view["status"] == "awaiting_confirmation" and view["test_mode"] and "claims" in view and "confirmations" not in view
    assert client.post("/api/public/orders/tok/pay").status_code == 409  # not confirmed yet
    assert client.post("/api/public/orders/tok/confirm").json()["status"] == "confirmed"
    assert saved(store)["confirmations"][0]["kind"] == "customer"

    assert client.post("/api/public/orders/tok/pay").json() == {"checkout_url": "https://pay/1"}
    assert client.post("/api/public/orders/tok/pay").json() == {"checkout_url": "https://pay/1"}  # reused, not a second session
    assert calls.count(("POST", "checkout_sessions")) == 1

    event = {"type": "checkout_session.payment.paid", "data": {"id": "cs_1"}}
    assert client.post("/api/webhooks/paymongo", **signed(event, "forged")).status_code == 401
    assert client.post("/api/webhooks/paymongo", **signed(event)).json() == {"received": True}
    assert saved(store)["payment"]["status"] == "pending"  # the event alone marks nothing paid; PayMongo said no payment yet
    paid.append(True)
    client.post("/api/webhooks/paymongo", **signed(event))
    client.post("/api/webhooks/paymongo", **signed(event))  # a duplicate delivery
    order = saved(store)
    assert order["payment"]["status"] == "verified" and order["payment"]["payment_id"] == "pay_1" and "payment_review" not in order
    assert client.post("/api/public/orders/tok/check-payment").json()["payment"] == "verified"
    assert client.post("/api/public/orders/tok/pay").status_code == 409  # already paid


class Canned(BaseLlm):
    """Stands in for Gemini inside the ADK claim reviewer: fetches every section, then answers. Keeps what it was asked in `asked`."""

    async def generate_content_async(self, llm_request, stream=False):
        asked.append(llm_request)
        if not any(p.function_response for c in llm_request.contents for p in c.parts):
            reads = [
                types.Part(function_call=types.FunctionCall(name=f"read_{n}", args={}))
                for n in ("order", "chat", "packing", "delivery", "past_decisions")
            ]
            yield LlmResponse(content=types.Content(role="model", parts=reads))
            return
        review = cases.Review(
            findings=[cases.Finding(statement="Delivered", status="supported", evidence=["C", "Z9"], uncertainty="")],
            recommendation=cases.Recommendation(action="partial_refund", amount_php=200.0, explanation="", evidence=["C"]),
            draft_reply="Sorry",
            questions=[],
        )
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=review.model_dump_json())]))


def test_packing_delivery_claims_refunds_and_replacement(monkeypatch, seller):
    report = pack.Report(
        lines=[pack.Seen(line=1, visible=True, counts=[pack.Count(variant="blue", quantity=1)], damage=None, photos=[1], note="")], suggestions=[]
    )
    gemini = type("G", (), {"models": type("M", (), {"generate_content": lambda self, **k: type("R", (), {"text": report.model_dump_json()})()})()})()
    store, bucket = fake_google(monkeypatch, gemini)
    put_order(store, packing=None)
    monkeypatch.setattr(config, "PAYMONGO_KEY", "sk_test_x")
    monkeypatch.setattr(config, "PAYMONGO_WEBHOOK_SECRET", "whsk")

    # Packing: the photo is stored once with its hash, Gemini's report is applied, and packing completes.
    photo = samples.packing_png()
    upload = lambda: client.post("/api/orders/o1/packing/photos", content=photo, headers={**SELLER, "Content-Type": "image/png"})
    assert client.post("/api/orders/o1/packing/photos", content=b"x", headers={**SELLER, "Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/orders/o1/dispatch/start", json={"courier": "simulator"}, headers=SELLER).status_code == 409  # not packed
    p = upload().json()["packing"]["photos"][0]
    assert p["sha256"] == hashlib.sha256(photo).hexdigest() and bucket.files[p["path"]] == photo
    assert client.get(f"/api/orders/o1/packing/photos/{p['id']}", headers=SELLER).content == photo
    rows = client.post("/api/orders/o1/packing/check", headers=SELLER).json()["packing"]["rows"]
    assert rows[0]["auto_result"] == "matched"
    assert client.post("/api/orders/o1/packing/complete", headers=SELLER).json()["packing"]["completed"]["by"] == "ok@example.com"

    # Delivery by the simulator, then the customer says it arrived.
    client.post("/api/orders/o1/dispatch/start", json={"courier": "simulator"}, headers=SELLER)
    for status in ("picked_up", "delivered"):
        r = client.post("/api/orders/o1/dispatch/step", json={"status": status}, headers=SELLER)
    assert r.json()["fulfillment"]["status"] == "delivered"
    assert client.post("/api/public/orders/tok/received").json()["received"] is True

    # The customer reports damage with a photo; the ADK reviewer's made-up citation is dropped.
    cid = client.post("/api/public/orders/tok/claims", json={"type": "damage", "narrative": "Cracked"}).json()["claims"][0]["id"]
    claim = client.post(f"/api/public/orders/tok/claims/{cid}/photos", content=photo, headers={"Content-Type": "image/png"}).json()["claims"][0]
    assert claim["photos"] == 1
    chat = [{"id": "m1", "from_page": False, "created_time": "t", "text": "cracked!"}]
    monkeypatch.setattr(messenger, "fetch_messages", lambda conversation_id: chat)
    monkeypatch.setattr(claim_agent, "gemini", lambda model, project: Canned(model="canned"))
    recalled, remembered = [], []
    monkeypatch.setattr(
        memory, "recall", lambda order, query: recalled.append(query) or ["Decided 2026-10-01: Damaged claim on 1 x Mug (blue): replaced."]
    )
    monkeypatch.setattr(memory, "remember", lambda order, fact: remembered.append((order["id"], fact)))
    review = client.post(f"/api/orders/o1/claims/{cid}/review", headers=SELLER).json()["claims"][0]["review"]
    assert review["findings"][0]["evidence"] == ["C"] and set(review["sources"]) == {"C"}
    assert review["read"][-1] == "past" and recalled == ["Damaged claim on 1 x Mug (blue)"]
    decision = {"action": "partial_refund", "amount": 20000, "reason": "Cracked in the photo"}
    assert client.post(f"/api/orders/o1/claims/{cid}/decide", json=decision, headers=SELLER).json()["claims"][0]["status"] == "resolution_approved"
    assert remembered == [
        ("o1", "Damaged claim on 1 x Mug (blue): the seller approved a partial refund of ₱200.00. Seller's reason: Cracked in the photo")
    ]

    # PayMongo does not answer the refund; its refund webhook later settles it through the payment's refund list.
    monkeypatch.setattr(paymongo, "paymongo_refund", lambda *a: {})
    refund = client.post("/api/orders/o1/refunds", json={"claim_id": cid, "amount": 20000}, headers=SELLER).json()["refunds"][0]
    assert refund["status"] == "uncertain"
    listed = {"refunds": [{"id": "ref_1", "attributes": {"amount": 20000, "status": "pending", "metadata": {"operation_id": refund["id"]}}}]}
    monkeypatch.setattr(paymongo, "paymongo", lambda method, path, attributes=None: {"attributes": listed} if path == "payments/pay_1" else {})
    event = {"type": "payment.refunded", "data": {"id": "ref_1", "type": "refund", "attributes": {"payment_id": "pay_1"}}}
    assert client.post("/api/webhooks/paymongo", **signed(event)).status_code == 200
    landed = saved(store)["refunds"][0]
    assert (landed["status"], landed["provider_refund_id"]) == ("pending", "ref_1")

    # The claim closes only once PayMongo reports the refund succeeded.
    post = lambda path, **k: client.post(f"/api/orders/o1{path}", headers=SELLER, **k)
    claim_of = lambda r, cid: next(c for c in r.json()["claims"] if c["id"] == cid)
    assert post(f"/claims/{cid}/resolve", json={}).status_code == 409
    listed["refunds"][0]["attributes"]["status"] = "succeeded"
    client.post("/api/webhooks/paymongo", **signed(event))
    assert claim_of(post(f"/claims/{cid}/resolve", json={}), cid)["resolution"]["how"] == "refund succeeded at PayMongo"

    # A refund the seller made outside PayMongo needs its transfer reference, and closes the claim as seller-reported.
    new_claim = lambda kind: post("/claims", json={"type": kind, "narrative": "Reported by chat"}).json()["claims"][-1]["id"]
    c3 = new_claim("missing_item")
    post(f"/claims/{c3}/decide", json={"action": "partial_refund", "amount": 10000, "reason": "Saucer missing"})
    manual = {"claim_id": c3, "amount": 10000, "method": "outside_paymongo"}
    assert post("/refunds/manual", json=manual).status_code == 409
    assert post("/refunds/manual", json={**manual, "reference": "GC123"}).json()["refunds"][-1]["status"] == "seller_reported"
    assert claim_of(post(f"/claims/{c3}/resolve", json={}), c3)["resolution"]["how"] == "seller-reported refund"

    # Another report waits for evidence, is escalated, then approved as a replacement, which is packed and shipped again.
    c2 = new_claim("wrong_item")
    assert claim_of(post(f"/claims/{c2}/wait"), c2)["status"] == "awaiting_evidence"
    escalated = claim_of(post(f"/claims/{c2}/escalate", json={"owner": "Kevin", "next_action": "Ask for a photo"}), c2)
    assert (escalated["status"], escalated["escalation"]["owner"]) == ("escalated", "Kevin")
    assert post(f"/claims/{c2}/escalate", json={"owner": " ", "next_action": "x"}).status_code == 409
    assert post(f"/claims/{c2}/replacement").status_code == 409  # not approved yet
    post(f"/claims/{c2}/decide", json={"action": "replacement", "reason": "Wrong colour sent"})
    r = post(f"/claims/{c2}/replacement")
    assert claim_of(r, c2)["status"] == "executing" and r.json()["fulfillment"] is None and r.json()["packing"]["photos"] == []
    assert post(f"/claims/{c2}/resolve", json={}).status_code == 409  # the replacement has not arrived
    upload()
    post("/packing/check")
    post("/packing/complete")
    assert post("/dispatch/start", json={"courier": "simulator"}).json()["fulfillment"]["claim_id"] == c2
    for status in ("picked_up", "delivered"):
        post("/dispatch/step", json={"status": status})
    r = post(f"/claims/{c2}/resolve", json={})
    assert claim_of(r, c2)["resolution"]["how"] == "replacement delivered" and r.json()["replacement"] is None
    assert [f["claim_id"] for f in saved(store)["fulfillment_history"]] == [None]  # the first shipment stays on record


def test_lalamove_quote_book_cancel_release_refresh_and_webhook(monkeypatch, seller):
    store, _ = fake_google(monkeypatch)
    put_order(store)
    monkeypatch.setattr(config, "LALAMOVE_KEY", "pk")
    monkeypatch.setattr(config, "LALAMOVE_SECRET", "sk")
    later = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M.00Z")
    calls, answers = [], ["LL0", None, "LL1"]  # None: Lalamove does not answer the booking

    def fake_lalamove(method, path, body=None):
        calls.append((method, path))
        if path == "/v3/quotations":
            return {"quotationId": "q1", "priceBreakdown": {"total": "120.50"}, "expiresAt": later, "stops": [{"stopId": "a"}, {"stopId": "b"}]}
        if path == "/v3/orders":
            assert body["data"]["metadata"]["orderId"] == "o1" and body["data"]["recipients"][0]["phone"] == "+639171234567"
            if not (placed := answers.pop(0)):
                raise lalamove.LalamoveNoAnswer()
            return {"orderId": placed, "shareLink": "https://share"}
        return {"status": "PICKED_UP", "stops": []}

    monkeypatch.setattr(lalamove, "lalamove", fake_lalamove)
    pin = "https://maps.google.com/?q=14.6091,121.0223"
    assert client.post("/api/orders/o1/dispatch/quote", json={"pin": pin}, headers=SELLER).status_code == 409  # no pickup yet
    pickup = {"address": "Unit 1, Cubao", "pin": pin, "name": "Shop", "phone": "0917 000 0000"}
    assert client.put("/api/settings/dispatch", json=pickup, headers=SELLER).json()["phone"] == "+639170000000"

    post = lambda path, **k: client.post(f"/api/orders/o1/dispatch/{path}", headers=SELLER, **k)

    # Cancelled before pickup: cancelled with Lalamove too, and kept on record.
    post("quote", json={"pin": pin})
    post("book")
    assert post("cancel", json={"reason": "Wrong address"}).json()["fulfillment"] is None
    assert ("DELETE", "/v3/orders/LL0") in calls and saved(store)["fulfillment_history"][-1]["status"] == "cancelled"

    # No answer to a booking: it stays locked, and can be released only once the quote has expired.
    post("quote", json={"pin": pin})
    assert post("book").json()["fulfillment"]["booking_uncertain"] is True
    assert post("cancel", json={}).status_code == 409
    assert post("release").status_code == 409  # Lalamove may still create the order

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=10)

    monkeypatch.setattr(dispatch, "datetime", Later)
    assert post("release").json()["fulfillment"]["status"] == "quoted"
    monkeypatch.setattr(dispatch, "datetime", datetime)

    f = post("quote", json={"pin": pin}).json()["fulfillment"]
    assert (f["status"], f["quotation"]["price"]) == ("quoted", 12050)
    assert post("book").json()["fulfillment"]["provider_order_id"] == "LL1"
    assert post("book").status_code == 409  # cannot book twice
    assert calls.count(("POST", "/v3/orders")) == 3
    assert client.post("/api/orders/o1/dispatch/refresh", headers=SELLER).json()["fulfillment"]["status"] == "picked_up"

    # Lalamove's own event finds the order by its Lalamove ID through the real query.
    data = {"order": {"orderId": "LL1", "status": "COMPLETED"}, "updatedAt": later}
    sig = lalamove.lalamove_signature("1", "POST", "/api/webhooks/lalamove", json.dumps(data, separators=(",", ":")))
    event = {"apiKey": "pk", "timestamp": 1, "eventId": "E1", "eventType": "ORDER_STATUS_CHANGED", "data": data, "signature": sig}
    assert client.post("/api/webhooks/lalamove", content=json.dumps(event)).status_code == 200
    assert saved(store)["fulfillment"]["status"] == "delivered" and "webhookEvents/lalamove_E1" in store.docs


def test_demo_seed_puts_a_claim_review_and_a_shaky_photo_one_click_away(monkeypatch):
    report = pack.Report(
        lines=[pack.Seen(line=1, visible=True, counts=[pack.Count(variant="cream", quantity=1)], damage=None, photos=[1], note="")], suggestions=[]
    )
    gemini = type("G", (), {"models": type("M", (), {"generate_content": lambda self, **k: type("R", (), {"text": report.model_dump_json()})()})()})()
    store, _ = fake_google(monkeypatch, gemini)
    assert fake_token(monkeypatch, firebase={"sign_in_provider": "anonymous"}).json()["demo"] is True
    monkeypatch.setattr(messenger, "graph", lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo visitors never reach Messenger")))
    monkeypatch.setattr(claim_agent, "gemini", lambda model, project: Canned(model="canned"))

    chats = {c["customer"]["name"]: c for c in client.get("/api/conversations", headers=SELLER).json()}  # the first visit seeds
    assert len(chats) == 8 and chats["Bea Villanueva"]["order_actions"] == ["Review a customer claim"]
    assert {n: c["hint"] for n, c in chats.items()} == demo.HINTS  # every seeded chat says what to try in it
    assert chats["Nico Ramos"]["order_actions"] == ["Ready to pack"] and chats["Rico Bautista"]["order_actions"] == []
    orders = {o["draft"]["recipient_name"]["value"]: o for o in client.get("/api/orders", headers=SELLER).json()}
    assert set(orders) == {"Leah Cruz", "Rico Bautista", "Bea Villanueva", "Nico Ramos"}

    # Bea: one click reviews her open report, with her damage photo and the packing photo in front of the reviewer.
    bea = orders["Bea Villanueva"]
    assert (bea["fulfillment"]["status"], bea["claims"][0]["status"], bea.get("receipt")) == ("delivered", "open", None)
    asked.clear()
    claim = client.post(f"/api/orders/{bea['id']}/claims/{bea['claims'][0]['id']}/review", headers=SELLER).json()["claims"][0]
    assert claim["status"] == "under_review" and claim["review"]["findings"][0]["evidence"] == ["C"]
    up_front = [p for c in asked[0].contents for p in c.parts if p.inline_data]
    fetched = [r for c in asked[-1].contents for p in c.parts if p.function_response for r in p.function_response.parts or []]
    assert len(up_front) == 1 and len(fetched) == 1  # her photo comes with the claim; the packing photo, when the agent reads packing
    assert claim["review"]["read"] == ["order", "chat", "packing", "delivery"]  # Memory Bank is off here: past decisions unavailable

    # Nico: one click checks his packing photo, and code holds it back as too shaky, whatever Gemini read.
    nico = orders["Nico Ramos"]
    assert nico["packing_blocked"] is None and len(nico["packing"]["photos"]) == 1
    packing = client.post(f"/api/orders/{nico['id']}/packing/check", headers=SELLER).json()["packing"]
    assert packing["rows"][0]["auto_result"] == "insufficient_visibility" and packing["suggestions"][0].startswith("Photo 1 is too blurry")

    # Their payments are in the sample GCash history; a reset deletes all four orders and seeds them again.
    history = client.get("/api/demo/sample-statement", headers=SELLER)
    assert history.status_code == 200 and history.headers["content-type"] == "application/pdf"
    assert len(store.docs[f"stores/{config.STORE}/demoUsers/u1"]["samples"]) == 3
    assert client.post("/api/demo/reset", headers=SELLER).json() == {"deleted_chats": 8, "deleted_orders": 4}
    assert len(client.get("/api/orders", headers=SELLER).json()) == 4 and len(client.get("/api/conversations", headers=SELLER).json()) == 8
