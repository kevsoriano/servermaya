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
from seed import catalog, demo
from services import chats

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
    photo = catalog.image("packing-mug-blue")
    upload = lambda: client.post("/api/orders/o1/packing/photos", content=photo, headers={**SELLER, "Content-Type": "image/jpeg"})
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
    claim = client.post(f"/api/public/orders/tok/claims/{cid}/photos", content=photo, headers={"Content-Type": "image/jpeg"}).json()["claims"][0]
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


def test_chat_confirm_payment_link_received_and_claim_from_chat(monkeypatch, seller):
    store, _ = fake_google(monkeypatch)
    put_order(store, status="draft", confirmations=[], payment=None, packing=None, source={"type": "messenger", "conversation_id": "t_1"})
    monkeypatch.setattr(config, "PAGE_ID", "99")
    now = datetime.now(UTC)
    say = lambda mid, text, page=False, **k: {"id": mid, "text": text, "from_page": page, "created_time": now.isoformat(), "attachments": [], **k}
    chat = [say("m1", "2 mugs please")]
    sent = []
    monkeypatch.setattr(messenger, "fetch_messages", lambda cid: chat)
    monkeypatch.setattr(
        messenger,
        "graph",
        lambda path, send=None, **k: (
            {"participants": {"data": [{"id": "99"}, {"id": "psid7"}]}} if "fields" in k else sent.append(send) or {"message_id": "mid"}
        ),
    )
    post = lambda path, **k: client.post(f"/api/orders/o1{path}", headers=SELLER, **k)
    get = lambda: client.get("/api/orders/o1", headers=SELLER).json()

    # The summary goes out with a Confirm button naming the order and version; only a tap with exactly that text confirms.
    order = post("/ask-confirmation").json()
    assert order["status"] == "awaiting_confirmation" and order["asked"]["confirm"]["psid"] == "psid7"  # for the webhook's check
    message = sent[-1]["message"]
    assert "Total: ₱500.00" in message["text"] and message["quick_replies"][0] == {
        "content_type": "text",
        "title": "Confirm #O1 v1",
        "payload": "confirm:o1:1",
    }
    chat += [say("m2", "Confirm #O1 v0"), say("m3", "Confirm #O1 v1", page=True)]  # wrong version; the Page's own echo
    assert get()["status"] == "awaiting_confirmation"
    chat.append(say("m4", "Confirm #O1 v1"))
    order = client.get("/api/conversations/t_1/orders", headers=SELLER).json()[0]
    assert order["status"] == "confirmed" and order["confirmations"][-1] | {"at": None} == {
        "version": 1,
        "kind": "customer",
        "by": "chat",
        "message_id": "m4",
        "at": None,
    }

    # The payment link goes in the chat; PayMongo sends the customer to a page that marks nothing paid.
    def fake_paymongo(method, path, attributes=None):
        assert attributes["success_url"].endswith("/paid?returned=1")
        return {"id": "cs_1", "attributes": {"checkout_url": "https://pay/1", "livemode": False}}

    monkeypatch.setattr(paymongo, "paymongo", fake_paymongo)
    assert post("/payment-link").json()["payment"]["status"] == "pending"
    assert sent[-1]["message"] == {"text": "Thank you for confirming! Pay ₱500.00 with QR Ph here: https://pay/1"}

    # Received: only once it is on its way, and only a tap after the ask.
    assert post("/ask-received").status_code == 409
    saved(store)["fulfillment"] = {"id": "f1", "status": "picked_up", "courier": "simulator", "events": []}
    chat.append(say("m5", "Received #O1"))  # before the ask: does not count
    asked_at = post("/ask-received").json()["asked"]["received"]["at"]
    assert sent[-1]["message"]["quick_replies"][0]["title"] == "Received #O1"
    chat[-1]["created_time"] = (datetime.fromisoformat(asked_at) - timedelta(minutes=5)).isoformat()
    assert get().get("receipt") is None
    chat.append(say("m6", "Received #O1"))
    assert get()["receipt"]["source"] == "customer_chat"

    # A complaint in the chat: the seller picks the messages, and the claim takes the customer's words and photo.
    photo = catalog.image("damage-mug-blue")
    resp = type("R", (), {"content": photo, "headers": {"content-type": "image/jpeg"}, "raise_for_status": lambda self: None})()
    monkeypatch.setattr(chats.requests, "get", lambda url, timeout: resp)
    image = {"kind": "image", "url": "https://scontent.xx.fbcdn.net/c.jpg", "mime_type": "image/jpeg", "name": None, "preview_url": None}
    chat += [say("m7", "One mug arrived cracked", attachments=[image]), say("m8", "So sorry!", page=True)]
    chat.append(say("m9", "", attachments=[{**image, "url": "https://evil.example/x.jpg"}]))
    assert post("/claims/from-chat", json={"type": "damage", "message_ids": ["m9"]}).status_code == 422  # not Meta's CDN
    assert post("/claims/from-chat", json={"type": "damage", "message_ids": ["m8"]}).status_code == 422  # the seller's own message
    claim = post("/claims/from-chat", json={"type": "damage", "message_ids": ["m7", "m8"]}).json()["claims"][0]
    assert (claim["source"], claim["narrative"], claim["message_ids"]) == ("customer_chat", "One mug arrived cracked", ["m7"])
    assert [p["source"] for p in claim["photos"]] == ["customer_chat"]


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
    watched = []  # the videos sent to the per-video check

    def generate_content(self, model, contents, config):
        if getattr(config, "system_instruction", None) != cases.VIDEO_CHECK:
            return type("R", (), {"text": report.model_dump_json()})()
        watched.append(contents[0].file_data.file_uri)
        seen = cases.VideoCheck(
            continuous=True, cut_at=None, sealed_at_start=True, item="cracked", label_code="BEA7Q", label_name="Bea Villanueva", text_on_screen=None
        )
        return type("R", (), {"text": seen.model_dump_json()})()

    gemini = type("G", (), {"models": type("M", (), {"generate_content": generate_content})()})()
    store, _ = fake_google(monkeypatch, gemini)
    assert fake_token(monkeypatch, firebase={"sign_in_provider": "anonymous"}).json()["demo"] is True
    monkeypatch.setattr(messenger, "graph", lambda *a, **k: (_ for _ in ()).throw(AssertionError("demo visitors never reach Messenger")))
    monkeypatch.setattr(claim_agent, "gemini", lambda model, project: Canned(model="canned"))

    chats = {c["customer"]["name"]: c for c in client.get("/api/conversations", headers=SELLER).json()}  # the first visit seeds
    assert len(chats) == 15 and chats["Bea Villanueva"]["order_actions"] == []  # her report waits in the chat
    # Each seeded chat keeps the persona the AI customer plays in it, by the customer's seeded name.
    seeded = {d["name"]: d.get("persona") for p, d in store.docs.items() if "/demoThreads/" in p}
    assert len(seeded) == 15 and all(name == persona for name, persona in seeded.items())
    # The chats behind Patterns have their checks stored, so listing them shows the count and opening them asks no Gemini.
    assert chats["Paolo Garcia"]["open_checks"] == 3 and chats["Kim Navarro"]["open_checks"] == 2 and chats["Carla Villamor"]["open_checks"] == 0
    assert chats["Nico Ramos"]["order_actions"] == ["Ready to pack"] and chats["Rico Bautista"]["order_actions"] == []
    listed = client.get("/api/orders", headers=SELLER).json()  # newest first
    orders = {o["draft"]["recipient_name"]["value"]: o for o in reversed(listed)}  # each customer's newest order
    # Every chat but Ana's has earlier orders: delivered by simulated sandbox riders, some with a claim decided and refunded.
    assert len(listed) == 4 + len(demo.MISFITS) + sum(len(h) for h in demo.HISTORY.values()) and "Ana Reyes" not in orders
    past = [o for o in listed if (o.get("fulfillment") or {}).get("courier") == "lalamove"]
    assert {o["fulfillment"]["driver"]["source"] for o in past} == {"simulated"} and all(o["receipt"] for o in past)
    assert {c["decision"]["cause"] for o in past for c in o.get("claims", [])} == {"courier_transit", "unknown", "seller_error"}
    cards = {c["key"]: c for c in client.get("/api/patterns", headers=SELLER).json()}
    assert cards["loss_concentration:driver:sim-2"]["elevated"] and client.get("/api/patterns/returns", headers=SELLER).json()["causes"]
    # Before the demo's live steps: three receipts that did not fit this week (Mark's makes four), two requests to open an
    # account (Dan's makes the card), two damage reports (Bea's makes the card), two parcels with other contents on Sample Rider 3.
    receipts = cards["rising:theme:receipt_misfit"]
    assert (receipts["numerator"], receipts["with_pressure"]) == (3, 3)
    assert {i["name"] for i in receipts["incidents"]} == {m[0] for m in demo.MISFITS}
    assert "rising:theme:account_request" not in cards and "rising:theme:damage_reports" not in cards
    assert cards["contents_concentration:driver:sim-3"]["numerator"] == 2

    # Bea: the seller opens her report from her two messages, then one click reviews it, with her damage photo and the packing
    # photo in front of the reviewer. That is the third damage report this week.
    bea = orders["Bea Villanueva"]
    assert (bea["fulfillment"]["status"], bea.get("claims"), bea.get("receipt")) == ("delivered", None, None)
    said = [
        m["id"]
        for m in client.get(f"/api/conversations/{chats['Bea Villanueva']['id']}", headers=SELLER).json()
        if m["attachments"] and not m["from_page"]
    ]
    bea = client.post(f"/api/orders/{bea['id']}/claims/from-chat", json={"type": "damage", "message_ids": said[-2:]}, headers=SELLER).json()
    rise = {c["key"]: c for c in client.get("/api/patterns", headers=SELLER).json()}["rising:theme:damage_reports"]
    assert rise["numerator"] == 3 and rise["incidents"][0]["name"] == "Bea Villanueva"
    asked.clear()
    claim = client.post(f"/api/orders/{bea['id']}/claims/{bea['claims'][0]['id']}/review", headers=SELLER).json()["claims"][0]
    assert claim["status"] == "under_review" and claim["review"]["findings"][0]["evidence"] == ["C"]
    up_front = [p for c in asked[0].contents for p in c.parts if p.inline_data]
    fetched = [r for c in asked[-1].contents for p in c.parts if p.function_response for r in p.function_response.parts or []]
    # Her photo comes with the claim, the packing photo when the agent reads packing. Her unboxing video and the packing video
    # are each checked on their own, as gs:// links to the evidence bucket; the agent gets the checks, whose label code is
    # compared with her order's in code.
    assert len(up_front) == 1 and len(fetched) == 1 and not [p for c in asked[0].contents for p in c.parts if p.file_data]
    assert len(watched) == 2 and all(u.startswith(f"gs://{config.EVIDENCE_BUCKET}/stores/main/orders/{bea['id']}/") for u in watched)
    assert bea["id"].startswith("bea7q")
    given = " ".join(p.text for p in asked[0].contents[0].parts if p.text)
    assert "[CV1C]" in given and "checked in code against this order (#BEA7Q, Bea Villanueva): it matches" in given
    assert claim["review"]["read"] == ["order", "chat", "packing", "delivery"]  # Memory Bank is off here: past decisions unavailable
    # Reviewed again: both video checks are kept by the videos' hashes, so neither video is watched a second time, and the
    # agent still gets both checks.
    before = len(asked)
    client.post(f"/api/orders/{bea['id']}/claims/{bea['claims'][0]['id']}/review", headers=SELLER)
    again = " ".join(p.text for p in asked[before].contents[0].parts if p.text)
    assert len(watched) == 2 and "[CV1C]" in again and "it matches" in again

    # Nico: one click checks his packing photo, and code holds it back as too shaky, whatever Gemini read.
    nico = orders["Nico Ramos"]
    assert nico["packing_blocked"] is None and len(nico["packing"]["photos"]) == 1
    packing = client.post(f"/api/orders/{nico['id']}/packing/check", headers=SELLER).json()["packing"]
    assert packing["rows"][0]["auto_result"] == "insufficient_visibility" and packing["suggestions"][0].startswith("Photo 1 is too blurry")

    # Bea's claim was opened from her chat messages: her words and her photo.
    assert (bea["claims"][0]["source"], len(bea["claims"][0]["message_ids"])) == ("customer_chat", 2)

    # Ana, in the demo: the seller's summary carries the Confirm button, and tapping it as the customer confirms at once.
    ana = chats["Ana Reyes"]["id"]
    order = ready_order(status="draft", confirmations=[], payment=None, packing=None, source={"type": "demo", "conversation_id": ana, "owner": "u1"})
    store.collection("stores").document(config.STORE).collection("orders").document("ana1").set(order)
    assert client.post("/api/orders/ana1/ask-confirmation", headers=SELLER).json()["status"] == "awaiting_confirmation"
    last = client.get(f"/api/conversations/{ana}", headers=SELLER).json()[-1]
    assert last["from_page"] and last["quick_replies"] == ["Confirm #ANA1 v1"]
    client.post(f"/api/conversations/{ana}/customer", json={"text": "Confirm #ANA1 v1"}, headers=SELLER)
    assert client.get("/api/orders/ana1", headers=SELLER).json()["confirmations"][-1]["by"] == "chat"
    # Send GCash QR code sends the instructions in the chat itself, with the demo shop's account and the total
    assert client.post("/api/orders/ana1/gcash/request", headers=SELLER).json()["manual_payment"]["status"] == "requested"
    told, qr = client.get(f"/api/conversations/{ana}", headers=SELLER).json()[-2:]
    assert told["from_page"] and told["text"].startswith("To pay by GCash, send ₱500.00 to Juan Dela Cruz, 0917 123 4567")
    # then the demo shop's sample QR: stamped as a sample, never a real account
    assert qr["from_page"] and qr["attachments"][0]["kind"] == "image"
    # A photo she sends in the demo chat becomes the claim's photo, read from our storage.
    photo = catalog.image("damage-mug-blue")
    client.post(f"/api/conversations/{ana}/customer/image", content=photo, headers={**SELLER, "Content-Type": "image/jpeg"})
    mid = client.get(f"/api/conversations/{ana}", headers=SELLER).json()[-1]["id"]
    claim = client.post("/api/orders/ana1/claims/from-chat", json={"type": "damage", "message_ids": [mid]}, headers=SELLER).json()["claims"][0]
    assert claim["narrative"] == "Sent photos only, no text." and claim["photos"][0]["sha256"] == hashlib.sha256(photo).hexdigest()

    # Their payments are in the sample GCash history; a reset deletes all four orders and seeds them again.
    history = client.get("/api/demo/sample-statement", headers=SELLER)
    assert history.status_code == 200 and history.headers["content-type"] == "application/pdf"
    gcash_paid = 3 + sum(len(h) for h in demo.HISTORY.values())
    assert len(store.docs[f"stores/{config.STORE}/demoUsers/u1"]["samples"]) == gcash_paid
    assert client.post("/api/demo/reset", headers=SELLER).json() == {"deleted_chats": 15, "deleted_orders": len(listed) + 1}
    assert len(client.get("/api/orders", headers=SELLER).json()) == len(listed) and len(client.get("/api/conversations", headers=SELLER).json()) == 15


def test_catalog_edit_photos_demo_read_only_and_notes_on_drafts(monkeypatch, seller):
    store, bucket = fake_google(monkeypatch)
    post = lambda path, headers=SELLER, **k: client.post(f"/api/catalog{path}", headers=headers, **k)
    mug = {"name": "Ceramic mug", "variants": [{"name": "Blue", "price": 18000, "stock": 2}, {"name": "White", "price": 18000}]}
    assert post("", json={"name": "", "variants": []}).status_code == 422
    pid = post("", json=mug).json()["id"]
    image = catalog.image("mug-blue")
    assert post(f"/{pid}/photos/1", content=b"x", headers={**SELLER, "Content-Type": "text/plain"}).status_code == 415
    assert post(f"/{pid}/photos/5", content=image, headers={**SELLER, "Content-Type": "image/jpeg"}).status_code == 404
    saved = post(f"/{pid}/photos/0", content=image, headers={**SELLER, "Content-Type": "image/jpeg"}).json()
    assert bucket.files[saved["variants"][0]["photo"]["path"]] == image
    assert client.get(f"/api/catalog/{pid}/photos/0", headers=SELLER).content == image
    assert client.get(f"/api/catalog/{pid}/photos/1", headers=SELLER).status_code == 404  # no photo for white
    # An edit keeps each variant's photo by name, and the list is the store's.
    edited = client.put(f"/api/catalog/{pid}", json={**mug, "variants": [{"name": "Blue", "price": 19000, "stock": 0}]}, headers=SELLER).json()
    assert edited["variants"][0]["photo"] and edited["variants"][0]["price"] == 19000
    assert [p["name"] for p in client.get("/api/catalog", headers=SELLER).json()] == ["Ceramic mug"]

    # Notes from the catalog snapshot come back with the order.
    snap = {"product_id": pid, "name": "Ceramic mug", "variant": "Blue", "price": 19000, "stock": 0, "photo": None}
    item = {"name": "Ceramic mug", "variant": "Blue", "quantity": 1, "unit_price": 18000, "price_from": "chat", "sources": [], "catalog": snap}
    put_order(store, status="draft", draft={**ready_order()["draft"], "items": [item]})
    notes = client.get("/api/orders/o1", headers=SELLER).json()["catalog_notes"]
    assert notes == [
        "Ceramic mug (Blue): the chat says ₱180.00, your catalog says ₱190.00. The chat price is what the customer agreed to.",
        "Ceramic mug (Blue): out of stock in your catalog.",
    ]
    assert client.delete(f"/api/catalog/{pid}", headers=SELLER).json() == {"deleted": pid}

    # A demo visitor sees the demo shop's catalog and its photos, and cannot change anything.
    assert fake_token(monkeypatch, firebase={"sign_in_provider": "anonymous"}).status_code == 200
    shown = client.get("/api/catalog", headers=SELLER).json()
    assert [p["id"] for p in shown] == ["mug", "tote", "candle", "pouch"]
    assert client.get("/api/catalog/mug/photos/0", headers=SELLER).content == catalog.image("mug-blue")
    assert post("", json=mug).status_code == 403 and client.delete("/api/catalog/mug", headers=SELLER).status_code == 403


def test_messenger_webhook_applies_a_tap_from_the_customer_it_was_sent_to(monkeypatch):
    store, _ = fake_google(monkeypatch)
    now = datetime.now(UTC)
    asked = {"confirm": {"text": "Confirm #O1 v1", "version": 1, "at": now - timedelta(minutes=1), "psid": "psid7"}}
    put_order(store, status="awaiting_confirmation", confirmations=[], payment=None, packing=None, asked=asked)
    monkeypatch.setattr(config, "MESSENGER_VERIFY_TOKEN", "verify-me")
    monkeypatch.setattr(config, "MESSENGER_APP_SECRET", "appsecret")

    # Meta's one-time check when the webhook is set up
    hub = lambda token: client.get(f"/api/webhooks/messenger?hub.mode=subscribe&hub.verify_token={token}&hub.challenge=42")
    assert hub("wrong").status_code == 403 and hub("verify-me").text == "42"

    def post(sender: str, payload: str, secret="appsecret", **message):
        event = {
            "sender": {"id": sender},
            "timestamp": int(now.timestamp() * 1000),
            "message": {"mid": "m_9", "quick_reply": {"payload": payload}, **message},
        }
        raw = json.dumps({"object": "page", "entry": [{"messaging": [event]}]})
        sig = "sha256=" + hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return client.post("/api/webhooks/messenger", content=raw, headers={"X-Hub-Signature-256": sig})

    assert post("psid7", "confirm:o1:1", secret="forged").status_code == 401
    post("psid8", "confirm:o1:1")  # someone else tapping a forwarded button
    post("psid7", "confirm:o1:0")  # an older version
    post("psid7", "confirm:o1:1", is_echo=True)  # the Page's own message
    post("psid7", "confirm:nope:1")  # no such order: ignored, not an error
    assert saved(store)["status"] == "awaiting_confirmation"
    assert post("psid7", "confirm:o1:1").json() == {"received": True}
    assert saved(store)["status"] == "confirmed" and saved(store)["confirmations"][-1]["message_id"] == "m_9"


def test_dropoff_pin_is_suggested_once_per_address_and_read_from_a_pasted_link(monkeypatch, seller):
    from integrations import maps

    store, _ = fake_google(monkeypatch)
    put_order(store)
    looked_up = []
    monkeypatch.setattr(maps, "geocode", lambda address: looked_up.append(address) or {"address": address, "lat": 14.62, "lng": 121.05})
    get = lambda: client.get("/api/orders/o1/dispatch/suggest", headers=SELLER).json()
    assert get()["lat"] == 14.62 and get()["lat"] == 14.62 and looked_up == ["Cubao"]  # kept on the order: one lookup
    saved(store)["draft"]["address"]["value"] = "Cubao, Quezon City"
    assert get()["address"] == "Cubao, Quezon City" and len(looked_up) == 2  # a changed address is looked up again

    # Not found: no pin, a map around the pickup to click on, and a plain warning
    monkeypatch.setattr(maps, "geocode", lambda address: None)
    monkeypatch.setattr(config, "MAPS_SERVER_KEY", "k")
    store.collection("stores").document(config.STORE).set({"dispatch": {"lat": 14.55, "lng": 121.02}})
    saved(store)["draft"]["address"]["value"] = "Somewhere unknown"
    assert get() | {"warning": ""} == {"address": "Somewhere unknown", "not_found": True, "lat": 14.55, "lng": 121.02, "warning": ""}

    pin = lambda text: client.post("/api/orders/o1/dispatch/pin", json={"text": text}, headers=SELLER)
    assert pin("https://www.google.com/maps/@14.6191,121.0510,18z").json() == {"lat": 14.6191, "lng": 121.051}
    assert pin("not a pin at all").status_code == 422


def test_loading_an_order_moves_a_sandbox_booking_on_unless_switched_off(monkeypatch, seller):
    from domain import delivery

    store, _ = fake_google(monkeypatch)
    past = datetime.now(UTC) - timedelta(seconds=5)
    booked = delivery.event("lalamove:L1:created", "provider", "ORDER_CREATED", "booked", past, past)
    shipment = {"id": "f1", "courier": "lalamove", "status": "booked", "quotation": {"livemode": False}, "events": [booked], "driver_id": None}
    put_order(store, fulfillment=shipment)
    monkeypatch.setattr(config, "SANDBOX_AUTO_DELIVERY", False)
    assert client.get("/api/orders/o1", headers=SELLER).json()["fulfillment"]["status"] == "booked"
    monkeypatch.setattr(config, "SANDBOX_AUTO_DELIVERY", True)
    f = client.get("/api/orders/o1", headers=SELLER).json()["fulfillment"]
    assert f["status"] == "delivered" and [e["source"] for e in f["events"]] == ["provider", "demo", "demo", "demo"]


def test_the_sellers_own_qr_is_saved_and_sent_after_the_gcash_instructions(monkeypatch, seller):
    store, _ = fake_google(monkeypatch)
    store.collection("stores").document(config.STORE).set({"gcash": {"name": "Ana Seller", "mobile": "639171112222", "qr": None}})
    put_order(store, payment=None, packing=None, source={"type": "messenger", "conversation_id": "t_1"})
    qr = catalog.image("gcash-qr-sample")  # any JPEG stands in for the seller's screenshot
    assert client.post("/api/settings/gcash/qr", content=b"x", headers={**SELLER, "Content-Type": "image/gif"}).status_code == 415
    assert client.post("/api/settings/gcash/qr", content=qr, headers={**SELLER, "Content-Type": "image/jpeg"}).json() == {"qr_image": True}
    assert client.get("/api/settings/gcash/qr", headers=SELLER).content == qr
    assert client.get("/api/settings/gcash", headers=SELLER).json()["qr_image"] is True

    monkeypatch.setattr(config, "PAGE_ID", "99")
    now = datetime.now(UTC).isoformat()
    monkeypatch.setattr(messenger, "fetch_messages", lambda cid: [{"id": "m1", "text": "hi", "from_page": False, "created_time": now}])
    sent = []
    monkeypatch.setattr(
        messenger,
        "graph",
        lambda path, send=None, files=None, **k: (
            {"participants": {"data": [{"id": "99"}, {"id": "psid7"}]}} if "fields" in k else sent.append((send, files)) or {"message_id": "mid"}
        ),
    )
    assert client.post("/api/orders/o1/gcash/request", headers=SELLER).json()["manual_payment"]["status"] == "requested"
    (text, _), (image, files) = sent
    assert text["message"]["text"].startswith("To pay by GCash, send ₱500.00 to Ana Seller, 0917 111 2222")
    assert image["message"]["attachment"]["type"] == "image" and files["filedata"][1] == qr  # the very screenshot the seller saved


def test_a_return_the_customer_sends_holds_the_refund_until_the_item_is_received(monkeypatch, seller):
    from domain import delivery

    report = pack.Report(
        lines=[pack.Seen(line=1, visible=True, counts=[pack.Count(variant="blue", quantity=1)], damage="a crack", photos=[1], note="")],
        suggestions=[],
    )
    gemini = type("G", (), {"models": type("M", (), {"generate_content": lambda self, **k: type("R", (), {"text": report.model_dump_json()})()})()})()
    store, _ = fake_google(monkeypatch, gemini)
    past = datetime.now(UTC) - timedelta(days=1)
    shipped = {
        "id": "f1",
        "courier": "lalamove",
        "status": "delivered",
        "quotation": {"livemode": False},
        "events": [delivery.event("e1", "provider", "COMPLETED", "delivered", past, past)],
        "driver_id": None,
        "pod": delivery.proof("DELIVERED", past, "demo", {"sample": "courier-dropoff-crushed"}),
    }
    put_order(store, fulfillment=shipped, manual_payment=None)
    post = lambda path, **k: client.post(f"/api/orders/o1{path}", headers=SELLER, **k)

    cid = post("/claims", json={"type": "damage", "narrative": "Box crushed, mug cracked"}).json()["claims"][0]["id"]
    custody = client.get("/api/orders/o1", headers=SELLER).json()["claims"][0]["custody"]
    assert [s["step"] for s in custody["steps"]][:4] == ["packed", "picked_up", "in_transit", "dropped_off"]
    assert client.get("/api/orders/o1/shipments/f1/proof/pod", headers=SELLER).content == catalog.image("courier-dropoff-crushed")
    assert post(f"/claims/{cid}/return").status_code == 409  # nothing approved yet
    decided = post(f"/claims/{cid}/decide", json={"action": "refund", "reason": "Crushed in transit", "cause": "courier_transit"}).json()
    assert decided["claims"][0]["decision"]["cause"] == "courier_transit"
    rid = post(f"/claims/{cid}/return").json()["returns"][0]["id"]

    r = post(f"/returns/{rid}/sent", json={"courier": "J&T", "tracking": "JT123"}).json()["returns"][0]
    assert (r["status"], r["sent"]["source"]) == ("shipping", "customer_reported")
    manual = {"claim_id": cid, "amount": 50000, "method": "outside_paymongo", "reference": "GC1"}
    assert "waits for the returned item" in post("/refunds/manual", json=manual).json()["detail"]
    post(f"/returns/{rid}/received")
    assert post("/refunds/manual", json=manual).json()["refunds"][-1]["status"] == "seller_reported"

    photo = catalog.image("damage-mug-blue")
    upload = client.post(f"/api/orders/o1/returns/{rid}/photos", content=photo, headers={**SELLER, "Content-Type": "image/jpeg"})
    assert upload.json()["returns"][0]["photos"]
    check = post(f"/returns/{rid}/check").json()["returns"][0]["check"]
    assert check["rows"][0]["result"] == "conflict"  # code decides: the item is damaged
    inspection = {"result": "damaged_as_reported", "condition": "Cracked, the same blue mug", "weight_g": 320}
    done = post(f"/returns/{rid}/inspect", json=inspection).json()
    assert done["returns"][0]["status"] == "inspected"
    assert done["claims"][0]["custody"]["steps"][-1]["status"] == "ok"


def test_the_customer_brief_reads_history_only_and_is_shared_by_chats_with_the_same_history(monkeypatch, seller):
    from domain import brief

    asked, failing = [], []

    def generate_content(self, model, contents, config):
        asked.append(contents)
        if failing:
            failing.pop()
            raise RuntimeError("Vertex did not answer")
        return type("R", (), {"text": brief.Brief(summary=f"Summary {len(asked)}.").model_dump_json()})()

    gemini = type("G", (), {"models": type("M", (), {"generate_content": generate_content})()})()
    store, _ = fake_google(monkeypatch, gemini)
    old = {"id": "m0", "from_page": False, "created_time": "2026-10-01T01:00:00+00:00", "text": "2 mugs please"}
    chats = {"t_1": [old], "t_2": [dict(old)]}
    monkeypatch.setattr(messenger, "fetch_messages", lambda conversation_id: chats[conversation_id])
    get = lambda cid="t_1": client.get(f"/api/conversations/{cid}/brief", headers=SELLER).json()

    # No finished order: a fixed first-order line, no Gemini call.
    assert get() == {"summary": brief.FIRST, "first": True} and asked == []

    # A received order is history: the brief is written from it and the messages about it.
    received = {"status": "customer_confirmed", "fulfillment_id": "f1", "at": datetime(2026, 10, 2, tzinfo=UTC)}
    put_order(store, receipt=received, messages=[old], source={"type": "messenger", "conversation_id": "t_1"})
    assert get() == {"summary": "Summary 1."} and "received by the customer" in asked[0] and "2 mugs please" in asked[0]

    # Today's messages are not history: the brief stays, with no new call.
    chats["t_1"].append({"id": "m1", "from_page": False, "created_time": "2026-10-18T01:00:00+00:00", "text": "Already paid, ship today!"})
    assert get() == {"summary": "Summary 1."} and len(asked) == 1

    # Another chat with the same history shares the brief: no call.
    order = store.docs[f"stores/{config.STORE}/orders/o1"]
    store.collection("stores").document(config.STORE).collection("orders").document("o2").set(
        {**order, "source": {"type": "messenger", "conversation_id": "t_2"}}
    )
    assert get("t_2") == {"summary": "Summary 1."} and len(asked) == 1

    # A finished order's claim changes the history: written again, retried once quietly when a call fails.
    claim = {"id": "c1", "type": "damage", "status": "resolved", "message_ids": [], "decision": {"action": "refund", "cause": "courier_transit"}}
    store.docs[f"stores/{config.STORE}/orders/o1"]["claims"] = [claim]
    failing.append(1)
    assert get() == {"summary": "Summary 3."} and "cause recorded: the courier, in transit" in asked[-1]
    # Both tries fail: nothing is kept, so asking again (Try again) writes it.
    store.docs[f"stores/{config.STORE}/orders/o1"]["claims"] = [{**claim, "decision": {**claim["decision"], "cause": "unknown"}}]
    failing.extend([1, 1])
    assert get() == {"summary": None, "problem": "Could not write the brief right now."}
    assert get() == {"summary": "Summary 6."}
