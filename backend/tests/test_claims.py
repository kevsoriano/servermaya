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


def test_claim_review_runs_as_an_adk_agent_with_the_citation_guard():
    from datetime import datetime

    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    from domain import claim_agent
    from domain import claims as cases

    seen = []

    class Canned(BaseLlm):
        """Stands in for Gemini: answers with a review that cites one real record, one made-up one, and nothing."""

        async def generate_content_async(self, llm_request, stream=False):
            seen.append(llm_request)
            review = cases.Review(
                findings=[
                    cases.Finding(statement="Delivered", status="supported", evidence=["D1", "Z9"], uncertainty="Courier status only"),
                    cases.Finding(statement="Invented", status="supported", evidence=["NOPE"], uncertainty=""),
                ],
                recommendation=cases.Recommendation(action="refund", amount_php=None, explanation="", evidence=["C", "X1"]),
                draft_reply="Sorry",
                questions=["Can you send a photo?"],
            )
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=review.model_dump_json())]))

    now = datetime(2026, 10, 16, tzinfo=UTC)
    reviewer = claim_agent.agent(Canned(model="canned"))
    assert reviewer.output_schema is cases.Review and not reviewer.tools  # it can only read what it is given and cite it
    parts = [types.Part.from_text(text="Records:\n[C] Claim\n[D1] Delivered")]
    review = claim_agent.review(reviewer, "canned", parts, {"C", "D1"}, now)

    request = seen[0]
    assert request.config.system_instruction.startswith(cases.SYSTEM) and request.config.temperature == 0  # ADK appends the agent's name
    assert request.contents[-1].parts[0].text.startswith("Records:")
    # The after-agent callback ran claims.checked: made-up citations are gone and the uncited finding is withheld.
    assert review["findings"][0]["evidence"] == ["D1"]
    assert review["findings"][1]["withheld"] and review["recommendation"]["evidence"] == ["C"]
    assert (review["engine"], review["model"], review["at"], review["evidence_ids"]) == ("adk", "canned", now, ["C", "D1"])
