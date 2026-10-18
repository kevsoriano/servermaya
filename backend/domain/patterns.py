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
CONTENTS = ("missing_item", "wrong_item")  # the parcel arrived, but not with what was packed
# Only these causes count against a courier, vehicle or driver. A seller error never does; a customer cause never does.
CARRIER_CAUSES = (None, "unknown", "courier_transit")
CARRIER_KINDS = ("courier", "vehicle", "driver", "area")

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
    "contents_concentration": [
        "An item may have been left out at packing; compare the packing photo with the order",
        "Someone else at the address may have opened the parcel first",
        "An assigned driver is not proof of who handled the parcel",
        "Lalamove assigns its drivers, so one driver carrying several of these parcels can be chance",
    ],
    "loss_concentration": [
        "This group may carry more fragile or high-value items",
        "Volume differs between groups; compare the rates, not the counts",
        "An assigned driver is not proof of who handled the parcel when it was damaged",
        "Lalamove assigns its drivers, so one driver carrying several of these parcels can be chance",
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
                    "cause": (c.get("decision") or {}).get("cause"),
                    "status": c["status"],
                    "conversation_id": (o.get("source") or {}).get("conversation_id"),
                    "name": ((o.get("draft") or {}).get("recipient_name") or {}).get("value"),
                }
            )
    return out


def rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def card(rule: str, kind: str, value: str, label: str, linked: list[dict], **fields) -> dict:
    key = f"{rule}:{kind}:{value}"
    digest = hashlib.sha256("|".join(sorted(i.get("claim_id") or i.get("id") or i["order_id"] for i in linked)).encode()).hexdigest()[:16]
    return {
        "key": key,
        "rule": rule,
        "rule_version": RULE_VERSION,
        "entity": {"kind": kind, "value": value, "label": label},
        "incidents": linked,
        "incidents_hash": digest,
        "alternatives": ALTERNATIVES.get(rule, []),
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
                next((f"{i['name']}'s chat" for i in mine if i.get("name")), "One customer's Messenger thread"),
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


def area(order: dict) -> str | None:
    """The city or area: the last part of the order's address, as written."""
    text = (((order.get("draft") or {}).get("address") or {}).get("value") or "").strip()
    return text.rsplit(",", 1)[-1].strip() or None if text else None


DRIVER_LIMITS = {
    "provider": "Lalamove assigns drivers, so a repeat driver is chance unless you book them as a Favourite Driver.",
    "simulated": "Simulated sandbox drivers: sample data for the demo, never a real rider.",
    "seller_reported": "Grouped on the rider's name and plate as you typed them; a typo makes two riders of one.",
}


def loss_concentration(
    orders: list[dict], since: datetime, found: list[dict], types=LOSS, rule="loss_concentration", kinds: tuple[str, ...] | None = None
) -> list[dict]:
    """Damage and non-receipt reports (types) per delivered shipment, grouped by courier, vehicle, area, driver and product,
    or only the given kinds. Only claims whose recorded cause is transit or unknown count against a courier, vehicle, area or driver."""
    rows = []  # one per delivered shipment: its groups, whether it had a loss, and whether that loss can count against its carrier
    by_shipment = {i["fulfillment_id"]: i for i in found if i["type"] in types}
    labels, sources = {}, {}
    for o in orders:
        items = [f"{i['name']}" + (f" ({i['variant']})" if i.get("variant") else "") for i in o["draft"]["items"]]
        for f in shipments(o):
            if f["status"] != "delivered" or f["created_at"] < since:
                continue
            groups = [("courier", f["courier"])] + [("product", p) for p in items]
            if (vehicle := (f.get("quotation") or {}).get("service")) is not None:
                groups.append(("vehicle", vehicle))
            if place := area(o):
                groups.append(("area", place))
            driver = f.get("driver") or ({"source": "provider", "driver_id": f["driver_id"]} if f.get("driver_id") else None)
            if driver:
                groups.append(("driver", driver["driver_id"]))
                sources[driver["driver_id"]] = driver["source"]
                if driver.get("name"):
                    labels[driver["driver_id"]] = driver["name"] + (f" · {driver['plate']}" if driver.get("plate") else "")
            incident = by_shipment.get(f["id"])
            rows.append(
                {
                    "id": f["id"],
                    "groups": groups,
                    "lost": bool(incident),
                    "carrier_lost": bool(incident) and incident.get("cause") in CARRIER_CAUSES,
                    "has_driver": bool(driver),
                }
            )
    total_d = len(rows)
    without_driver = sum(1 for r in rows if not r["has_driver"])
    out = []
    for kind, value in {g for r in rows for g in r["groups"]}:
        if kinds and kind not in kinds:
            continue
        key = "carrier_lost" if kind in CARRIER_KINDS else "lost"
        total_n = sum(r[key] for r in rows)
        mine = [r for r in rows if (kind, value) in r["groups"]]
        n, d = sum(r[key] for r in mine), len(mine)
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
        if kind in CARRIER_KINDS:
            limitations.append("Claims you recorded as the seller's or the customer's cause are left out of this rate.")
        if kind == "area":
            limitations.append("The area is the last part of the address as written, not a checked location.")
        if kind == "driver":
            limitations.append(DRIVER_LIMITS.get(sources.get(value), DRIVER_LIMITS["provider"]))
        if kind == "driver" or without_driver:
            limitations.append(f"{without_driver} of {total_d} delivered shipment(s) have no driver ID and are left out of driver groups.")
        names = {"courier": "Courier", "vehicle": "Vehicle", "area": "Area", "driver": "Driver", "product": "Product"}
        label = f"{names[kind]} {labels.get(value, value)}"
        if kind == "driver" and sources.get(value) == "simulated":
            label += " (simulated)"
        out.append(
            card(
                rule,
                kind,
                value,
                label,
                [by_shipment[r["id"]] for r in mine if r[key]],
                numerator=n,
                denominator=d,
                rate=rate(n, d),
                baseline={"numerator": rest_n, "denominator": rest_d, "rate": rate(rest_n, rest_d), "of": "other delivered shipments"},
                elevated=elevated,
                limitations=limitations,
                suggested_review=SUGGESTED_REVIEW[rule],
            )
        )
    return out


SUGGESTED_REVIEW = {
    "loss_concentration": "Compare the packing records, pickup and drop-off photos of these parcels, and what they had in common.",
    "contents_concentration": "Compare the packing photos and pickup photos of these parcels: were they complete and sealed when they left you?",
}


RETURN_ACTIONS = ("refund", "partial_refund", "replacement", "redelivery")


def returns_summary(orders: list[dict], now: datetime, days: int) -> dict:
    """Returns by cause: claims whose approved outcome sent money or goods back, counted once per claim, with the rate per
    delivered shipment and what they cost (refunds made, plus the courier price of replacement shipments; the customer sends
    a return back themselves)."""
    since = now - timedelta(days=days)
    delivered = sum(1 for o in orders for f in shipments(o) if f["status"] == "delivered" and f["created_at"] >= since)
    causes: dict[str, dict] = {}
    by: dict[str, dict[str, dict]] = {"courier": {}, "vehicle": {}, "product": {}}
    for o in orders:
        for c in o.get("claims", []):
            d = c.get("decision") or {}
            if c["created_at"] < since or d.get("action") not in RETURN_ACTIONS:
                continue
            cause = d.get("cause") or "unknown"
            refunded = sum(
                r["amount"] for r in o.get("refunds", []) if r.get("claim_id") == c["id"] and r["status"] in ("succeeded", "seller_reported")
            )
            shipping = sum((f.get("quotation") or {}).get("price") or 0 for f in shipments(o) if f.get("claim_id") == c["id"])
            row = causes.setdefault(cause, {"count": 0, "refunded": 0, "shipping": 0})
            row["count"] += 1
            row["refunded"] += refunded
            row["shipping"] += shipping
            f = next((x for x in shipments(o) if x["id"] == c.get("fulfillment_id")), None) or {}
            keys = {
                "courier": [f.get("courier") or "none"],
                "vehicle": [(f.get("quotation") or {}).get("service") or "none"],
                "product": [i["name"] + (f" ({i['variant']})" if i.get("variant") else "") for i in o["draft"]["items"]],
            }
            for group, values in keys.items():
                for v in values:
                    by[group].setdefault(v, {}).setdefault(cause, 0)
                    by[group][v][cause] += 1
    for row in causes.values():
        row["rate"] = rate(row["count"], delivered)
        row["cost"] = row["refunded"] + row["shipping"]
    return {"days": days, "delivered": delivered, "causes": causes, "by": by}


# Rising: one kind of case in the last 7 days against the 3 weeks before. Counted from orders and from the chat checks.
TREND_WEEKS = 4
TREND_MIN = 3  # cases in the last 7 days before a rise is shown
# Receipt checks that show the screenshot does not fit the order or the seller's account. "outcome" (a failed or pending
# transfer on screen) and "screening" (text aimed at the AI) are other matters.
RECEIPT_MISFITS = ("amount", "time", "reference", "recipient_name", "recipient_mobile", "duplicate")
THEMES = {
    "receipt_misfit": {
        "label": "Receipts that don't fit the order or your GCash account",
        "unit": "chats",
        "suggested_review": "Ship only once you find the payment in your own GCash history, or PayMongo verifies it. A screenshot is a claim, not proof.",
        "limitations": ["Counts receipts whose checks did not fit, one per order; a receipt sent to an old number by mistake counts too."],
        "alternatives": [
            "A customer may have paid an old or saved number by mistake",
            "A receipt can be resent from an earlier order by mistake",
            "A partial payment may have been agreed in the chat",
        ],
    },
    "account_request": {
        "label": "Asked you to open an account or share a code",
        "unit": "chats",
        "suggested_review": "You never need a new account, app, link or code to receive payment. Never share a code, OTP or PIN; reply with your usual GCash or PayMongo link.",
        "limitations": ["Counts chats where the checks found this request; a request written another way can be missed."],
        "alternatives": [
            "A customer may honestly not have GCash and be asking what else works",
            "Several of these chats may be the same person on different accounts",
        ],
    },
    "damage_reports": {
        "label": "Damage reports",
        "unit": "reports",
        "suggested_review": "Look at what these parcels had in common: product, packing, courier and driver. The concentration cards below may show it.",
        "limitations": ["Counts reports made, not damage found: see each report's outcome."],
        "alternatives": [
            "A batch of stock may be more fragile than usual",
            "Packing may have changed this month",
            "Busier weeks bring more reports; compare with your order count",
        ],
    },
}


def when(value) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def trend_cases(orders: list[dict], chats: list[dict], since: datetime) -> dict[str, list[dict]]:
    """Each theme's cases since `since`. chats: [{conversation_id, name, signals}], the stored checks of the seller's chats."""
    names = {c["conversation_id"]: c.get("name") for c in chats}
    pressure = {c["conversation_id"] for c in chats if any(s["kind"] == "pressure" for s in c.get("signals") or [])}
    who = lambda o: ((o.get("draft") or {}).get("recipient_name") or {}).get("value")
    out: dict[str, list[dict]] = {k: [] for k in THEMES}
    for o in orders:
        cid = (o.get("source") or {}).get("conversation_id")
        name = names.get(cid) or who(o)
        misfits = [
            c
            for m in filter(None, [o.get("manual_payment")] + o.get("manual_payment_history", []))
            for c in m.get("claims", [])
            if any(k["check"] in RECEIPT_MISFITS and k["result"] == "fail" for k in c.get("checks", []))
        ]
        if misfits and (at := when(misfits[0]["received_at"])) and at >= since:  # one per order: its first receipt that did not fit
            out["receipt_misfit"].append(
                {"id": f"receipt:{o['id']}", "order_id": o["id"], "conversation_id": cid, "name": name, "at": at, "pressure": cid in pressure}
            )
        for c in o.get("claims", []):
            if c["type"] == "damage" and when(c["created_at"]) >= since:
                out["damage_reports"].append(
                    {"id": c["id"], "claim_id": c["id"], "order_id": o["id"], "conversation_id": cid, "name": name, "at": when(c["created_at"])}
                )
    for chat in chats:
        for s in chat.get("signals") or []:
            at = when((s.get("times") or [None])[0])
            if s["kind"] == "account_request" and at and at >= since:
                out["account_request"].append(
                    {
                        "id": f"{chat['conversation_id']}:{s['sources'][0]}",
                        "conversation_id": chat["conversation_id"],
                        "name": chat.get("name"),
                        "at": at,
                    }
                )
    return out


def rising(orders: list[dict], chats: list[dict], now: datetime) -> list[dict]:
    """A theme with at least TREND_MIN cases in the last 7 days, and at least ELEVATED times its weekly average before."""
    since = now - timedelta(weeks=TREND_WEEKS)
    week = lambda at: min(int((now - at) / timedelta(weeks=1)), TREND_WEEKS - 1)
    volume = [0] * TREND_WEEKS  # orders made each week, oldest first, for scale
    for o in orders:
        if (at := when(o.get("created_at"))) and since <= at <= now:
            volume[TREND_WEEKS - 1 - week(at)] += 1
    out = []
    for theme, cases in trend_cases(orders, chats, since).items():
        weeks = [0] * TREND_WEEKS
        for c in cases:
            weeks[TREND_WEEKS - 1 - week(c["at"])] += 1
        recent, before = weeks[-1], sum(weeks[:-1]) / (TREND_WEEKS - 1)
        if recent < TREND_MIN or recent < ELEVATED * before:
            continue
        t = THEMES[theme]
        linked = sorted((c for c in cases if week(c["at"]) == 0), key=lambda c: c["at"], reverse=True)
        out.append(
            card(
                "rising",
                "theme",
                theme,
                t["label"],
                linked,
                numerator=recent,
                denominator=None,
                rate=None,
                baseline=None,
                elevated=True,
                weeks=weeks,
                weekly_before=round(before, 1),
                volume=volume,
                unit=t["unit"],
                with_pressure=sum(1 for c in linked if c.get("pressure")) if theme == "receipt_misfit" else None,
                limitations=t["limitations"]
                + [
                    f"Counts, not rates: you had {volume[-1]} new order(s) this week against {sum(volume[:-1]) / (TREND_WEEKS - 1):.1f} a week before."
                ],
                alternatives=t["alternatives"],
                suggested_review=t["suggested_review"],
            )
        )
    return out


def analyse(orders: list[dict], now: datetime, days: int = 30, chats: list[dict] | None = None) -> list[dict]:
    """chats: the stored checks of the seller's chats, for the rising cards. Those always compare the last 7 days with the
    3 weeks before, whatever the window."""
    since = now - timedelta(days=days)
    found = incidents(orders, since)
    return (
        rising(orders, chats or [], now)
        + reference_reuse(orders, since)
        + recurring_claims(orders, since, found)
        + loss_concentration(orders, since, found)
        + loss_concentration(orders, since, found, CONTENTS, "contents_concentration", ("driver",))
    )


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
