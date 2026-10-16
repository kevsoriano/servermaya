"""Pattern analysis over the store's own orders. Pure and deterministic: counts, rates and reuse checks only.

Cards describe concentrations for the seller to look into. They never establish intent, label anyone, or block an action.
Every card has its numerator and denominator, the rest of the store to compare with, the linked cases, what the data
lacks, and other explanations. Thresholds are review heuristics, not calibrated fraud probabilities.
"""

import hashlib
from datetime import datetime, timedelta

RULE_VERSION = 1
MIN_SHIPMENTS = 5  # below this, a rate is labelled insufficient history and never called elevated
MIN_INCIDENTS = 2
ELEVATED = 2.0  # a group's rate at least this many times the rest of the store's
LOSS = ("damage", "nonreceipt")

ALTERNATIVES = {
    "payment_reference_reuse": [
        "One payment may have been agreed to cover two orders; record it as explained if so",
        "The customer may have resent an old screenshot by mistake",
    ],
    "recurring_customer_claims": [
        "The customer may simply order often, or have had genuinely bad deliveries",
        "Earlier claims may have been resolved as the store's or courier's error",
        "A shared Messenger account can be several people",
    ],
    "loss_concentration": [
        "This group may carry more fragile or high-value items",
        "Volume differs between groups; compare the rates, not the counts",
        "An assigned driver is not proof of who handled the parcel when it was damaged",
        "Packing quality may differ between the parcels",
    ],
}


def shipments(order: dict) -> list[dict]:
    return [f for f in [order.get("fulfillment")] + order.get("fulfillment_history", []) if f]


def incidents(orders: list[dict], since: datetime) -> list[dict]:
    """Claims in the window, one per shipment and type: repeated reports of the same problem count once."""
    seen, out = set(), []
    for o in orders:
        for c in o.get("claims", []):
            key = (c.get("fulfillment_id") or o["id"], c["type"])
            if c["created_at"] < since or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "order_id": o["id"],
                    "claim_id": c["id"],
                    "fulfillment_id": c.get("fulfillment_id"),
                    "type": c["type"],
                    "at": c["created_at"],
                    "outcome": (c.get("decision") or {}).get("action"),
                    "status": c["status"],
                }
            )
    return out


def rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def card(rule: str, kind: str, value: str, label: str, linked: list[dict], **fields) -> dict:
    key = f"{rule}:{kind}:{value}"
    digest = hashlib.sha256("|".join(sorted(i["claim_id"] if "claim_id" in i else i["order_id"] for i in linked)).encode()).hexdigest()[:16]
    return {
        "key": key,
        "rule": rule,
        "rule_version": RULE_VERSION,
        "entity": {"kind": kind, "value": value, "label": label},
        "incidents": linked,
        "incidents_hash": digest,
        "alternatives": ALTERNATIVES[rule],
        **fields,
    }


def reference_reuse(orders: list[dict], since: datetime) -> list[dict]:
    """The same GCash reference or the same screenshot file on different orders. A verification task, not a rate."""
    seen: dict[tuple[str, str], dict[str, dict]] = {}
    for o in orders:
        if o.get("created_at") and o["created_at"] < since:
            continue
        requests = [o.get("manual_payment")] + o.get("manual_payment_history", [])
        for m in filter(None, requests):
            for c in m.get("claims", []):
                for kind, value in (("reference", c.get("reference")), ("screenshot", (c.get("image") or {}).get("sha256"))):
                    if value:
                        seen.setdefault((kind, value), {}).setdefault(o["id"], {"order_id": o["id"], "at": c.get("received_at")})
    out = []
    for (kind, value), by_order in seen.items():
        if len(by_order) > 1:
            label = f"GCash reference {value}" if kind == "reference" else f"Screenshot file {value[:12]}…"
            out.append(
                card(
                    "payment_reference_reuse",
                    kind,
                    value,
                    label,
                    list(by_order.values()),
                    numerator=len(by_order),
                    denominator=None,
                    rate=None,
                    baseline=None,
                    elevated=False,
                    limitations=["Counts orders that received the same reference or file; it is not a rate."],
                    suggested_review=f"Check your GCash history for {label.lower()}: which order did that payment pay for?",
                )
            )
    return out


def recurring_claims(orders: list[dict], since: datetime, found: list[dict]) -> list[dict]:
    """Claims per delivered shipment for each customer (their Messenger thread), against the rest of the store."""
    delivered = {o["id"]: sum(1 for f in shipments(o) if f["status"] == "delivered" and f["created_at"] >= since) for o in orders}
    customer = {o["id"]: (o.get("source") or {}).get("conversation_id") for o in orders}
    total_n, total_d = len(found), sum(delivered.values())
    out = []
    for who in {c for c in customer.values() if c}:
        mine = [i for i in found if customer[i["order_id"]] == who]
        d = sum(n for oid, n in delivered.items() if customer[oid] == who)
        if len(mine) < MIN_INCIDENTS:
            continue
        rest_n, rest_d = total_n - len(mine), total_d - d
        elevated = d >= MIN_SHIPMENTS and rest_d > 0 and rate(len(mine), d) >= ELEVATED * (rest_n / rest_d)
        outcomes = {k: sum(1 for i in mine if i["outcome"] == k) for k in {i["outcome"] for i in mine}}
        out.append(
            card(
                "recurring_customer_claims",
                "customer",
                who,
                "One customer's Messenger thread",
                mine,
                numerator=len(mine),
                denominator=d,
                rate=rate(len(mine), d),
                baseline={"numerator": rest_n, "denominator": rest_d, "rate": rate(rest_n, rest_d), "of": "the rest of the store"},
                elevated=elevated,
                outcomes=outcomes,
                limitations=([f"Only {d} delivered shipment(s): too little history to compare rates."] if d < MIN_SHIPMENTS else [])
                + ["Claims made are not findings of error or abuse; see each claim's outcome."],
                suggested_review="Compare these claims and how each was resolved. Do not refuse a genuine claim because of history.",
            )
        )
    return out


def loss_concentration(orders: list[dict], since: datetime, found: list[dict]) -> list[dict]:
    """Damage and non-receipt reports per delivered shipment, grouped by courier, driver and product."""
    rows = []  # one per delivered shipment: its groups and whether it had a loss incident
    lost = {i["fulfillment_id"] for i in found if i["type"] in LOSS}
    by_shipment = {i["fulfillment_id"]: i for i in found if i["type"] in LOSS}
    for o in orders:
        items = [f"{i['name']}" + (f" ({i['variant']})" if i.get("variant") else "") for i in o["draft"]["items"]]
        for f in shipments(o):
            if f["status"] != "delivered" or f["created_at"] < since:
                continue
            groups = [("courier", f["courier"])] + [("product", p) for p in items]
            if f.get("driver_id"):
                groups.append(("driver", f["driver_id"]))
            rows.append({"id": f["id"], "groups": groups, "lost": f["id"] in lost, "has_driver": bool(f.get("driver_id"))})
    total_n, total_d = sum(r["lost"] for r in rows), len(rows)
    without_driver = sum(1 for r in rows if not r["has_driver"])
    out = []
    for kind, value in {g for r in rows for g in r["groups"]}:
        mine = [r for r in rows if (kind, value) in r["groups"]]
        n, d = sum(r["lost"] for r in mine), len(mine)
        if n < MIN_INCIDENTS:
            continue
        rest_n, rest_d = total_n - n, total_d - d
        enough = d >= MIN_SHIPMENTS and rest_d >= MIN_SHIPMENTS
        elevated = enough and rate(n, d) >= ELEVATED * (rest_n / rest_d) if rest_n else enough
        if not elevated and enough:
            continue  # a busy courier or product with an ordinary rate is not a pattern
        limitations = []
        if not enough:
            limitations.append(f"{d} delivered shipment(s) here and {rest_d} elsewhere: too little history to compare rates.")
        if kind == "product":
            limitations.append("A claim names the parcel, not the item: every product in a reported parcel is counted.")
        if kind == "driver" or without_driver:
            limitations.append(f"{without_driver} of {total_d} delivered shipment(s) have no driver ID and are left out of driver groups.")
        label = {"courier": "Courier", "driver": "Lalamove driver", "product": "Product"}[kind] + f" {value}"
        out.append(
            card(
                "loss_concentration",
                kind,
                value,
                label,
                [by_shipment[r["id"]] for r in mine if r["lost"]],
                numerator=n,
                denominator=d,
                rate=rate(n, d),
                baseline={"numerator": rest_n, "denominator": rest_d, "rate": rate(rest_n, rest_d), "of": "other delivered shipments"},
                elevated=elevated,
                limitations=limitations,
                suggested_review="Compare the packing photos and delivery records of these parcels, and the products they had in common.",
            )
        )
    return out


def analyse(orders: list[dict], now: datetime, days: int = 30) -> list[dict]:
    since = now - timedelta(days=days)
    found = incidents(orders, since)
    return reference_reuse(orders, since) + recurring_claims(orders, since, found) + loss_concentration(orders, since, found)


def with_review(cards: list[dict], reviews: dict[str, dict]) -> list[dict]:
    """Attach the seller's review. A review made when the linked cases were different is superseded, and the card is new again."""
    out = []
    for c in cards:
        r = reviews.get(c["key"])
        if r and r.get("incidents_hash") != c["incidents_hash"]:
            out.append({**c, "review": {"status": "new"}, "previous_review": {**r, "status": "superseded", "was": r["status"]}})
        else:
            out.append({**c, "review": r or {"status": "new"}})
    return out
