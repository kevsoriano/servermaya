"""The claim review as a Google ADK agent.

The agent starts with the claim and the customer's photos only. It fetches the order's records with four read-only tools
(the order and payment, the chat, packing with its photos, delivery), and with a fifth recalls how the shop decided similar
claims before, from Memory Bank. It chooses what the claim needs. Nothing it can call changes anything. Its after-agent callback runs claims.checked in code with only the IDs it was given or fetched, so a
citation to a record it never saw is dropped before the review leaves the agent, and nothing it recommends is carried out.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import datetime

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import claims as cases

APP = "claim_review"
USER = "seller"

# Which tool returns a record, by the letters of its evidence ID. C and CP (the claim and its photos) come with the request.
SECTIONS = {
    "V": "order",
    "PAY": "order",
    "M": "chat",
    "K": "packing",
    "KP": "packing",
    "KV": "packing",
    "D": "delivery",
    "POD": "delivery",
    "R": "delivery",
}

TOOLS = """
You start with the claim [C], any photos the customer sent with it [CP1, CP2, ...] and the check of each video they sent [CV1C, ...]. The order's records are not in the
message: fetch them with the tools read_order, read_chat, read_packing and read_delivery. Call every tool whose records could
support or contradict the claim; when unsure, call it. You can cite only IDs you were given or fetched; any other is dropped.
read_past_decisions recalls how this shop decided similar claims from other customers [P1, ...]. Use it to suggest an outcome
consistent with the shop's practice and cite it when you do. Past decisions say nothing about this customer or this order:
never use them as evidence for or against the claim."""


def kind(part: types.Part) -> str:
    """Photo or Video, as the agent is told what each attached part is."""
    mime = (part.inline_data or part.file_data).mime_type or ""
    return "Video" if mime.startswith("video/") else "Photo"


def section(label: str) -> str:
    return SECTIONS.get(re.match(r"[A-Z]+", label)[0], "claim")


def agent(llm: BaseLlm, records: dict[str, list], read: list[str], recall: Callable[[], list[str] | None] = lambda: []) -> LlmAgent:
    """An agent for one review. records: section -> [(evidence ID, text or image Part)]. read: filled with the sections it
    fetches. recall: the shop's past decisions like this claim, asked only if the agent calls for them; None if unavailable."""

    def fetch(name: str) -> dict:
        if name not in read:
            read.append(name)
        got = records.get(name, [])
        texts = [r for _, r in got if isinstance(r, str)]
        photos = [(label, r) for label, r in got if not isinstance(r, str)]
        # Images go back as Parts, which ADK hands to Gemini as media; each one's label is in the texts, in the same order.
        texts += [f"[{label}] {kind(p)} {n} attached to this result." for n, (label, p) in enumerate(photos, 1)]
        return {"records": texts or [f"No {name} records."], "images": [p for _, p in photos]}

    def read_order() -> dict:
        """The order as confirmed: items, total, recipient and address [V], and how it was paid [PAY]."""
        return fetch("order")

    def read_chat() -> dict:
        """The customer's chat with the seller, oldest first [M1, M2, ...]."""
        return fetch("chat")

    def read_packing() -> dict:
        """The packing record: each line's photo check and the seller's inspection [K1, ...], the packing photos [KP1, ...] and the
        check of each packing video [KV1C, ...]."""
        return fetch("packing")

    def read_delivery() -> dict:
        """The shipment's status events [D1, ...], any courier proof of delivery [POD], and whether the customer confirmed receipt [R]."""
        return fetch("delivery")

    def read_past_decisions() -> dict:
        """How this shop decided earlier claims like this one, from other customers [P1, ...]: the seller's own decisions and reasons."""
        if "past" not in records:
            past = recall()
            records["past"] = [(f"P{n}", f"[P{n}] {fact}") for n, fact in enumerate(past or [], 1)]
            if past is None:
                return {"records": ["Past decisions are unavailable right now."], "images": []}
        return fetch("past")

    def guard(callback_context) -> None:
        """Code, not the model, decides which citations stand: claims.checked keeps only IDs the agent was given or fetched."""
        state = callback_context.state
        raw = state.get("review")
        if raw is None:
            return None  # no answer; the caller reports the failure
        found = cases.Review.model_validate_json(raw) if isinstance(raw, str) else cases.Review.model_validate(raw)
        seen = set(state["given"]) | {i for name in read for i, _ in records.get(name, [])}
        state["checked"] = cases.checked(found, seen, state["model"], state["now"])
        return None

    return LlmAgent(
        name="claim_reviewer",
        description="Fetches an order's records, compares the customer's claim with them, and cites them by evidence ID.",
        model=llm,
        instruction=lambda _ctx: cases.SYSTEM + TOOLS,  # a provider, so the brackets in the evidence IDs are never read as state placeholders
        tools=[read_order, read_chat, read_packing, read_delivery, read_past_decisions],
        output_schema=cases.Review,
        output_key="review",
        # 90 s: one Vertex request once hung for over 7 minutes; the route turns the timeout into a 502. LOW thinking: on the
        # eval's claim cases it was as accurate as MEDIUM and the default, at a median of 7 s against 36 s and 52 s.
        generate_content_config=types.GenerateContentConfig(
            temperature=0, thinking_config=types.ThinkingConfig(thinking_level="LOW"), http_options=types.HttpOptions(timeout=90_000)
        ),
        after_agent_callback=guard,
    )


def gemini(model: str, project: str) -> Gemini:
    """A new model per review: each review runs in its own event loop, and a client kept from an earlier one holds
    connections bound to a loop that is already closed."""
    return Gemini(model=model, client_kwargs={"vertexai": True, "project": project, "location": "global"})


def review(
    llm: BaseLlm,
    model: str,
    blocks: list[str],
    images: list[tuple[str, types.Part]],
    now: datetime,
    recall: Callable[[], list[str] | None] = lambda: [],
) -> dict:
    """Run one review in its own session and return the checked review. Raises if the agent gave no valid answer.

    blocks: claims.evidence's text records. images: (evidence ID, loaded image) for the photos that could be loaded.
    recall: past decisions like this claim (memory.recall), called only if the agent asks."""
    records: dict[str, list] = {}
    for label, item in [(b[1 : b.index("]")], b) for b in blocks] + images:
        records.setdefault(section(label), []).append((label, item))
    given = records.pop("claim", [])
    parts = []
    for label, item in given:
        parts += [types.Part.from_text(text=item)] if isinstance(item, str) else [types.Part.from_text(text=f"{kind(item)} {label}:"), item]
    read: list[str] = []
    reviewer = agent(llm, records, read, recall)

    async def run() -> dict | None:
        sessions = InMemorySessionService()
        runner = Runner(app_name=APP, agent=reviewer, session_service=sessions)
        state = {"given": [label for label, _ in given], "model": model, "now": now}
        session = await sessions.create_session(app_name=APP, user_id=USER, state=state)
        async for _ in runner.run_async(user_id=USER, session_id=session.id, new_message=types.Content(role="user", parts=parts)):
            pass
        done = await sessions.get_session(app_name=APP, user_id=USER, session_id=session.id)
        return done.state.get("checked")

    checked = asyncio.run(run())  # the route runs in FastAPI's thread pool, where no event loop is running
    if checked is None:
        raise RuntimeError("The claim review agent returned no review")
    texts = {label: item for got in records.values() for label, item in got if isinstance(item, str)}
    return {**checked, "engine": "adk", "read": read, "recalled": {k: v for k, v in texts.items() if re.fullmatch(r"P\d+", k)}}
