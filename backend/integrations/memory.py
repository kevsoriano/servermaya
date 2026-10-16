"""Memory Bank (Gemini Enterprise Agent Platform): the shop's past claim decisions, recalled by the claim reviewer.

Each decision is written as one fact, as given: no AI extraction or rewriting (claims.decision_fact says what goes in, and
keeps the customer's words out). Memories are scoped to the store, or to one demo visitor, who also sees a few shared
examples. Memory is a convenience: when Memory Bank is off or does not answer, reviews and decisions go on without it.

Run `make memory-bank` once: it creates the bank (print the name into backend/.env as MEMORY_BANK) and writes the demo examples.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import agentplatform

from core import config

APP = "claim_decisions"
DEMO_SHOP = "demo_shop"  # the shared examples every demo visitor can recall; written by setup, never by a visitor
DEMO_TTL = "2592000s"  # 30 days: a demo visitor's decisions are kept that long, even across a demo reset
TOP_K = 3
SPARE = 5  # asked for beyond TOP_K, so an order's own decisions (up to its 5 open claims) can be left out and still leave TOP_K

# ponytail: three made-up decisions so a new demo visitor's first review has a shop history; real stores start empty
DEMO_EXAMPLES = {
    "demo-mug-cracked": "Damaged claim on 1 x Ceramic mug (Blue): the seller approved a replacement. Seller's reason: crack visible in the "
    "customer's photo; the packing photo showed the mug whole, so it broke in transit.",
    "demo-mug-missing": "Missing item claim on 2 x Ceramic mug (Blue): the seller asked the customer for clarification. Seller's reason: "
    "the packing photo shows both mugs; asked for a photo of the opened parcel first.",
    "demo-tote-stain": "Damaged claim on 1 x Tote bag (Cream): the seller approved a partial refund of ₱100.00. Seller's reason: a small "
    "stain the customer chose to keep; agreed in chat.",
}


@lru_cache
def client():
    location = config.MEMORY_BANK.split("/locations/")[1].split("/")[0]  # the bank's own location
    return agentplatform.Client(project=config.PROJECT_ID, location=location)


def scope(order: dict) -> dict:
    source = order.get("source") or {}
    user = f"demo_{source['owner']}" if source.get("type") == "demo" else f"store_{config.STORE}"
    return {"app_name": APP, "user_id": user}


def remember(order: dict, fact: str) -> None:
    """Best effort, after the decision is saved. A failure loses one memory, never the decision."""
    if not config.MEMORY_BANK:
        return
    demo = (order.get("source") or {}).get("type") == "demo"
    tags = {"metadata": {"order_id": {"string_value": order["id"]}}} | ({"ttl": DEMO_TTL} if demo else {})
    try:
        client().memory_banks.memories.create(name=config.MEMORY_BANK, fact=fact, scope=scope(order), config=tags)
    except Exception as e:
        print(f"Memory Bank write failed: {e}")


def same_order(m, order: dict) -> bool:
    """A decision on this very order is its own history, not the shop's practice with other customers."""
    tag = (m.memory.metadata or {}).get("order_id")
    return bool(tag) and tag.string_value == order.get("id")


def recall(order: dict, query: str) -> list[str] | None:
    """Up to TOP_K past decisions on other orders most like this claim, dated, closest first. None when Memory Bank is off or
    did not answer."""
    if not config.MEMORY_BANK:
        return None
    scopes = [scope(order)] + ([{"app_name": APP, "user_id": DEMO_SHOP}] if (order.get("source") or {}).get("type") == "demo" else [])
    search = lambda s: list(
        client().memory_banks.memories.retrieve(
            name=config.MEMORY_BANK, scope=s, similarity_search_params={"search_query": query, "top_k": TOP_K + SPARE}
        )
    )
    try:
        with ThreadPoolExecutor(len(scopes)) as pool:  # about 3 s each from here: a demo visitor's two scopes at once
            found = [m for got in pool.map(search, scopes) for m in got if not same_order(m, order)]
    except Exception as e:
        print(f"Memory Bank search failed: {e}")
        return None
    found.sort(key=lambda m: m.distance if m.distance is not None else float("inf"))
    return [f"Decided {m.memory.update_time:%Y-%m-%d}: {m.memory.fact}" if m.memory.update_time else m.memory.fact for m in found[:TOP_K]]


def setup() -> None:
    """Create the bank if MEMORY_BANK is unset, then write the shared demo examples. Safe to run again."""
    if not config.MEMORY_BANK:
        bank = agentplatform.Client(project=config.PROJECT_ID, location="global").memory_banks.create(config={"display_name": "Claim decisions"})
        print(f"Created. Add to backend/.env, then run this again to write the demo examples:\nMEMORY_BANK={bank.name}")
        return
    for memory_id, fact in DEMO_EXAMPLES.items():
        try:
            client().memory_banks.memories.create(
                name=config.MEMORY_BANK, fact=fact, scope={"app_name": APP, "user_id": DEMO_SHOP}, config={"memory_id": memory_id}
            )
            print(f"Wrote {memory_id}")
        except Exception as e:  # already there from an earlier run
            print(f"Skipped {memory_id}: {str(e)[:120]}")


if __name__ == "__main__" and sys.argv[1:] == ["setup"]:
    setup()
