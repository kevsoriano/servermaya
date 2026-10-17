"""The custody check: one claim's evidence chain, from packing to return, read in code.

Each step records the parcel's state at one moment: ok, problem, unclear, missing, or noted (in transit, where nothing is
judged, only flags). The first step with a problem suggests where the problem started, and so a cause. Code suggests; the
seller decides the cause. Missing evidence is never held against anyone: no pickup or drop-off photo is never counted
against the courier, just as no unboxing video is never counted against the customer. Driver names never appear here, so
they never reach Gemini through the [COC] record.
"""

from datetime import timedelta

from . import returns
from .extraction import inline

CAUSES = {"seller_error": "Seller error", "courier_transit": "Courier, in transit", "customer": "Customer", "unknown": "Unknown"}
STEPS = {
    "packed": "Packed",
    "picked_up": "Picked up",
    "in_transit": "In transit",
    "dropped_off": "Dropped off",
    "opened": "Opened",
    "returned": "Returned",
}
DAMAGE = ("minor", "major")
# Weak: Metro Manila riders average well under 30 km/h; a trip over twice this estimate is worth a look, nothing more.
TRANSIT_KMH = 15
TRANSIT_SLACK = timedelta(minutes=30)


def step(name: str, status: str, note: str, evidence: list[str] = ()) -> dict:
    return {"step": name, "label": STEPS[name], "status": status, "note": note, "evidence": list(evidence)}


def shipment(order: dict, fid: str | None) -> dict | None:
    return next((f for f in [order.get("fulfillment")] + order.get("fulfillment_history", []) if f and f["id"] == fid), None)


def packed_step(order: dict, c: dict) -> dict:
    sessions = [p for p in [order.get("packing")] + order.get("packing_history", [])[::-1] if p and p.get("completed")]
    if not sessions:
        return step("packed", "missing", "No completed packing record.", ["K"])
    p = sessions[0]
    lines = [f"K{r['line']}" for r in p["rows"]]
    overridden = [r for r in p["rows"] if r["manual"] and r["auto_result"] == "conflict"]
    extra = "".join(
        [
            f" Weighed at {p['weight_g']} g." if p.get("weight_g") else "",
            f" Seal {inline(p['seal_id'])}." if p.get("seal_id") else "",
        ]
    )
    cut = [label for label, v in (c.get("video_checks") or {}).items() if label.startswith("KV") and v.get("continuous") is False]
    if cut:
        extra += " The packing video has a cut, so it shows less about what was packed; that never counts against the customer."
    if overridden and c["type"] in ("wrong_item", "missing_item", "damage"):
        names = ", ".join(f"line {r['line']}" for r in overridden)
        return step("packed", "problem", f"The photo check found a conflict on {names}, and the seller confirmed it by hand.{extra}", lines)
    return step("packed", "ok", f"Packed and checked, {len(p['rows'])} line(s).{extra}", lines + [f"{label}C" for label in cut])


def proof_step(name: str, p: dict | None, prefix: str) -> dict:
    where = "pickup" if name == "picked_up" else "drop-off"
    if not p:
        return step(name, "missing", f"No {where} proof from the courier.")
    if name == "dropped_off" and p.get("status") == "FAILED":
        return step(name, "problem", "The courier reported a failed delivery.", [prefix])
    chk = p.get("check")
    if not chk:
        has_photo = p.get("image") or p.get("image_url")
        return step(
            name,
            "unclear",
            "Photo not checked yet: it is checked when the claim is reviewed." if has_photo else f"A {where} status, no photo.",
            [prefix],
        )
    seen = []
    if chk.get("outer_damage") in DAMAGE:
        seen.append(f"{chk['outer_damage']} outer damage")
    if chk.get("seal_intact") is False:
        seen.append("the seal looks broken")
    if chk.get("label_match") is False:
        seen.append("the label does not match this order")
    ids = [f"{prefix}C"]
    if seen:
        return step(name, "problem", f"The {where} photo shows " + " and ".join(seen) + ".", ids)
    if chk.get("parcel_visible") and chk.get("outer_damage") == "none":
        return step(name, "ok", f"The {where} photo shows the parcel intact.", ids)
    return step(name, "unclear", f"The {where} photo does not clearly show the parcel.", ids)


def transit_step(f: dict) -> tuple[dict, list[str]]:
    flags = []
    if len({d["driver_id"] for d in f.get("drivers", [])}) > 1:
        flags.append("The driver changed during the order.")
    if any(e.get("raw_status") == "ORDER_REPLACED" for e in f["events"]):
        flags.append("Lalamove replaced the order while it was active.")
    at = {e["status"]: e["at"] for e in f["events"]}
    km = ((f.get("quotation") or {}).get("distance_m") or 0) / 1000
    if km and "picked_up" in at and "delivered" in at:
        took, expected = at["delivered"] - at["picked_up"], timedelta(hours=km / TRANSIT_KMH) + TRANSIT_SLACK
        if took > 2 * expected:
            flags.append(f"Pickup to drop-off took {int(took.total_seconds() // 60)} minutes for {km:.1f} km (a weak signal).")
    ids = [f"D{n}" for n, _ in enumerate(f["events"], 1)]
    if flags:
        return step("in_transit", "noted", " ".join(flags), ids), flags
    return step("in_transit", "ok", "Nothing unusual recorded in transit.", ids), flags


def opened_step(c: dict) -> dict:
    videos = {label: v for label, v in (c.get("video_checks") or {}).items() if label.startswith("CV")}
    if not videos:
        return step("opened", "missing", "No unboxing video.")
    ids = [f"{label}C" for label in videos]
    if any(v.get("continuous") is False for v in videos.values()):
        return step("opened", "problem", "The unboxing video has a cut, so it cannot show the item came out of that parcel.", ids)
    if any(v.get("label_match") is False for v in videos.values()):
        return step("opened", "problem", "The label in the unboxing video does not match this order.", ids)
    if all(v.get("sealed_at_start") for v in videos.values()):
        return step("opened", "ok", "One continuous shot from a sealed parcel.", ids)
    return step("opened", "unclear", "One continuous shot, but the video does not show the parcel sealed at the start.", ids)


def returned_step(order: dict, c: dict) -> dict:
    r = returns.for_claim(order, c["id"])
    if not r:
        return step("returned", "missing", "The item was not asked back.")
    if r["status"] in returns.WAITING:
        return step("returned", "missing", "On its way back.", ["RET"])
    i = r.get("inspection")
    if not i:
        return step("returned", "unclear", "Received, not inspected yet.", ["RET"])
    status = {"same_item": "ok", "damaged_as_reported": "ok", "different_item": "problem", "unclear": "unclear"}[i["result"]]
    return step("returned", status, f"Inspected: {returns.RESULTS[i['result']].lower()}. {inline(i['condition'])}", ["RET"])


def suggest(c: dict, by: dict[str, dict]) -> tuple[str, str]:
    st = lambda name: by[name]["status"]
    if c["type"] in ("wrong_item", "missing_item"):
        if st("packed") == "problem":
            return "seller_error", "The packing record shows a conflict the seller confirmed by hand."
        if st("returned") == "problem":
            return "unknown", "A different item came back: possibly swapped after packing. The other steps narrow down where."
        return "unknown", "Nothing in the records shows where the item went wrong."
    if c["type"] == "damage":
        if st("picked_up") == "problem":
            return "seller_error", "The parcel already looked damaged in the pickup photo, before the courier carried it."
        if st("dropped_off") == "problem":
            if st("picked_up") == "ok":
                return "courier_transit", "Intact at pickup, damaged at drop-off."
            return "unknown", "Damaged at drop-off, but no clear pickup photo shows it left intact."
        if st("dropped_off") == "ok":
            return "unknown", "The box was intact at drop-off: transit or packaging, the box cannot show which."
        return "unknown", "No drop-off photo shows when the damage happened."
    return "unknown", "This kind of claim has no step that points to a cause."


def check(order: dict, c: dict) -> dict:
    f = shipment(order, c.get("fulfillment_id"))
    steps = [packed_step(order, c)]
    flags = []
    if f:
        transit, flags = transit_step(f)
        steps += [proof_step("picked_up", f.get("pop"), "POP"), transit, proof_step("dropped_off", f.get("pod"), "POD")]
    else:
        steps += [step(name, "missing", "No shipment on record.", ["D"]) for name in ("picked_up", "in_transit", "dropped_off")]
    steps += [opened_step(c), returned_step(order, c)]
    by = {s["step"]: s for s in steps}
    cause, reason = suggest(c, by)
    first = next((s["step"] for s in steps if s["status"] == "problem"), None)
    return {"steps": steps, "first_break": first, "suggested_cause": cause, "reason": reason, "flags": flags}


def record(result: dict) -> str:
    """The custody check as a record the claim review can cite: [COC]."""
    parts = "; ".join(f"{s['label'].lower()}: {s['status']} ({s['note'].rstrip('.')})" for s in result["steps"])
    return f"[COC] Custody check, decided in code from the records: {parts}. Suggested cause: {CAUSES[result['suggested_cause']].lower()}, because {result['reason'][0].lower() + result['reason'][1:]}"
