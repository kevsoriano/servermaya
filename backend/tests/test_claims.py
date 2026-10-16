from datetime import UTC

import pytest

from helpers import ready_order
from routes import conversations


def test_claim_review_citations_decisions_and_outcomes():
    from datetime import datetime

    from domain import claims as cases
    from domain import delivery
    from domain import orders as rules

    now = datetime(2026, 10, 3, tzinfo=UTC)
    order = ready_order()
    order |= delivery.start(order, "f1", "simulator", "s", now)
    order |= delivery.step(order, "picked_up", "demo", "s", now)
    order |= delivery.step(order, "delivered", "demo", "s", now)
    with pytest.raises(rules.Refused):
        cases.open_claim({**order, "status": "draft"}, "c0", cases.ClaimIn(type="damage", narrative="x"), "seller", "s", now)
    order |= cases.open_claim(order, "c1", cases.ClaimIn(type="damage", narrative="The mug arrived cracked"), "customer_link", "customer", now)
    c = cases.claim(order, "c1")
    assert c["fulfillment_id"] == "f1" and c["status"] == "open"

    blocks, images = cases.evidence(order, [{"id": "m", "from_page": False, "created_time": "t", "text": "cracked!"}], c)
    ids = cases.ids(blocks, images)
    assert {"V1", "M1", "PAY", "D1", "D2", "D3", "R", "C"} <= ids
    found = cases.Review(
        findings=[
            cases.Finding(statement="Delivered per the simulator", status="supported", evidence=["D3", "D3", "Z9"], uncertainty="Simulated"),
            cases.Finding(statement="Invented", status="supported", evidence=["NOPE"], uncertainty=""),
        ],
        recommendation=cases.Recommendation(action="partial_refund", amount_php=100.0, explanation="", evidence=["C", "X1"]),
        draft_reply="Sorry",
        questions=[],
    )
    review = cases.checked(found, ids, "m", now)
    assert review["findings"][0]["evidence"] == ["D3"]  # unknown and repeated citations dropped
    assert review["findings"][1]["status"] == "insufficient_data" and review["findings"][1]["withheld"]
    assert review["recommendation"]["evidence"] == ["C"]
    order |= cases.record_review(order, "c1", review, "s", now)
    assert cases.claim(order, "c1")["status"] == "under_review"

    with pytest.raises(rules.Refused):
        cases.decide(order, "c1", cases.DecisionIn(action="partial_refund", amount=60000, reason="r"), "s", now)  # more than was paid
    order |= cases.decide(order, "c1", cases.DecisionIn(action="replacement", reason="Cracked in photo"), "s", now)
    assert cases.claim(order, "c1")["status"] == "resolution_approved" and "refunds" not in order  # approving executes nothing
    with pytest.raises(rules.Refused):
        cases.resolve(order, "c1", "done", "s", now)  # the replacement has not happened

    # The replacement is packed and shipped again; the original stays on record.
    order |= cases.start_replacement(order, "c1", "s", now)
    assert order["packing"] is None and order["fulfillment"] is None and order["fulfillment_history"][0]["id"] == "f1"
    with pytest.raises(rules.Refused):
        delivery.start(order, "f2", "simulator", "s", now)  # not packed again yet
    order["packing"] = {**ready_order()["packing"]}
    order |= delivery.start(order, "f2", "simulator", "s", now)
    assert order["fulfillment"]["replaces"] == "f1" and order["fulfillment"]["claim_id"] == "c1"
    order |= delivery.step(order, "picked_up", "demo", "s", now)
    order |= delivery.step(order, "delivered", "demo", "s", now)
    order |= cases.resolve(order, "c1", "", "s", now)
    assert cases.claim(order, "c1")["resolution"]["how"] == "replacement delivered" and order["replacement"] is None

    # A later claim reopens the case on the same order.
    order |= cases.open_claim(order, "c2", cases.ClaimIn(type="missing_item", narrative="Saucer missing"), "seller", "s", now)
    assert conversations.order_action(order) == "Review a customer claim"


def test_claim_review_agent_fetches_records_with_its_tools_and_cites_only_what_it_read():
    from datetime import datetime

    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    from domain import claim_agent
    from domain import claims as cases

    seen = []

    class Scripted(BaseLlm):
        """Stands in for Gemini: first fetches delivery and packing, then answers citing what it read, what it did not
        read (the chat, M1), and something made up."""

        async def generate_content_async(self, llm_request, stream=False):
            seen.append(llm_request)
            if len(seen) == 1:
                calls = [types.Part(function_call=types.FunctionCall(name=n, args={})) for n in ("read_delivery", "read_packing")]
                yield LlmResponse(content=types.Content(role="model", parts=calls))
                return
            review = cases.Review(
                findings=[
                    cases.Finding(statement="Delivered", status="supported", evidence=["D1", "Z9"], uncertainty="Courier status only"),
                    cases.Finding(statement="She said so in chat", status="supported", evidence=["M1"], uncertainty=""),
                ],
                recommendation=cases.Recommendation(action="refund", amount_php=None, explanation="", evidence=["C", "KP1", "X1"]),
                draft_reply="Sorry",
                questions=["Can you send a photo?"],
            )
            call = types.FunctionCall(name="set_model_response", args=review.model_dump())
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))

    now = datetime(2026, 10, 16, tzinfo=UTC)
    blocks = ["[V1] Order version 1", "[M1] Customer: cracked", "[K1] Packing line 1", "[D1] Delivered", "[C] Claim: cracked"]
    packed, sent = types.Part.from_bytes(data=b"kp", mime_type="image/png"), types.Part.from_bytes(data=b"cp", mime_type="image/png")
    review = claim_agent.review(Scripted(model="canned"), "canned", blocks, [("KP1", packed), ("CP1", sent)], now)

    first, second = seen
    assert first.config.system_instruction.startswith(cases.SYSTEM) and first.config.temperature == 0  # ADK appends the agent's name
    asked = first.contents[-1].parts
    assert asked[0].text.startswith("[C] Claim") and asked[2].inline_data.data == b"cp"  # only the claim and its photo up front
    assert not any("[V1]" in (p.text or "") for p in asked)
    assert {t.name for t in first.config.tools[0].function_declarations} >= {"read_order", "read_chat", "read_packing", "read_delivery"}
    answers = {p.function_response.name: p.function_response for c in second.contents for p in c.parts if p.function_response}
    assert answers["read_delivery"].response["records"] == ["[D1] Delivered"]
    assert answers["read_packing"].response["records"] == ["[K1] Packing line 1", "[KP1] Photo 1 attached to this result."]
    assert answers["read_packing"].parts[0].inline_data.data == b"kp"  # the packing photo reaches the model as an image

    # The after-agent callback ran claims.checked with only what it was given or fetched: M1 was never read, Z9 and X1 never existed.
    assert review["read"] == ["delivery", "packing"] and review["engine"] == "adk"
    assert review["findings"][0]["evidence"] == ["D1"] and review["findings"][1]["withheld"]
    assert review["recommendation"]["evidence"] == ["C", "KP1"]
    assert (review["model"], review["at"], review["evidence_ids"]) == ("canned", now, ["C", "CP1", "D1", "K1", "KP1"])
