"""The claim review as a Google ADK agent.

One LlmAgent reads the evidence blocks and images and answers in claims.Review. It has no tools: it can only read what
it is given and cite it. Its after-agent callback runs claims.checked in code, so a citation to a record that does not
exist is dropped before the review leaves the agent, and nothing it recommends is carried out.
"""

import asyncio
from datetime import datetime
from functools import lru_cache

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import claims as cases

APP = "claim_review"
USER = "seller"


def guard(callback_context) -> None:
    """Code, not the model, decides which citations stand: claims.checked keeps only IDs that were given to the agent."""
    state = callback_context.state
    raw = state.get("review")
    if raw is None:
        return None  # no answer; the caller reports the failure
    found = cases.Review.model_validate_json(raw) if isinstance(raw, str) else cases.Review.model_validate(raw)
    state["checked"] = cases.checked(found, set(state["evidence_ids"]), state["model"], state["now"])
    return None


def agent(model: str | BaseLlm, project: str = "") -> LlmAgent:
    llm = model if isinstance(model, BaseLlm) else Gemini(model=model, client_kwargs={"vertexai": True, "project": project, "location": "global"})
    return LlmAgent(
        name="claim_reviewer",
        description="Compares a customer's claim with the order's records and cites them by evidence ID.",
        model=llm,
        instruction=lambda _ctx: cases.SYSTEM,  # a provider, so the brackets in the evidence IDs are never read as state placeholders
        output_schema=cases.Review,
        output_key="review",
        # 90 s: one Vertex request once hung for over 7 minutes; the route turns the timeout into a 502.
        generate_content_config=types.GenerateContentConfig(temperature=0, http_options=types.HttpOptions(timeout=90_000)),
        after_agent_callback=guard,
    )


@lru_cache
def gemini_agent(model: str, project: str) -> LlmAgent:
    return agent(model, project)


def review(reviewer: LlmAgent, model: str, parts: list[types.Part], ids: set[str], now: datetime) -> dict:
    """Run one review in its own session and return the checked review. Raises if the agent gave no valid answer."""

    async def run() -> dict | None:
        sessions = InMemorySessionService()
        runner = Runner(app_name=APP, agent=reviewer, session_service=sessions)
        session = await sessions.create_session(app_name=APP, user_id=USER, state={"evidence_ids": sorted(ids), "model": model, "now": now})
        async for _ in runner.run_async(user_id=USER, session_id=session.id, new_message=types.Content(role="user", parts=parts)):
            pass
        done = await sessions.get_session(app_name=APP, user_id=USER, session_id=session.id)
        return done.state.get("checked")

    checked = asyncio.run(run())  # the route runs in FastAPI's thread pool, where no event loop is running
    if checked is None:
        raise RuntimeError("The claim review agent returned no review")
    return {**checked, "engine": "adk"}
