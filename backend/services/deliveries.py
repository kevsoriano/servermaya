import hashlib
from datetime import UTC, datetime

from google.api_core.exceptions import PreconditionFailed

from core import config, db
from domain import delivery
from integrations import lalamove


def advance_sandbox(order_id: str, order: dict) -> bool:
    """Apply the simulated sandbox steps now due, if any. Read paths call this, so nothing runs in the background."""
    if not config.SANDBOX_AUTO_DELIVERY or not delivery.sandbox_progress(order, datetime.now(UTC)):
        return False
    db.change(db.orders().document(order_id), lambda o: delivery.sandbox_progress(o, datetime.now(UTC)))
    return True


def keep_proof(provider_order_id: str, found: dict | None) -> dict | None:
    """A Lalamove proof with its photo copied into the evidence bucket: Lalamove serves orders for 60 days and its links may
    expire. A photo that cannot be downloaded leaves the proof with Lalamove's link only."""
    if not found:
        return None
    image = None
    if found.get("image_url") and (got := lalamove.proof_image(found["image_url"])):
        data, kind = got
        sha = hashlib.sha256(data).hexdigest()
        path = f"stores/{config.STORE}/courier/{provider_order_id}/{sha}"
        try:
            db.bucket().blob(path).upload_from_string(data, content_type=kind, if_generation_match=0)
        except PreconditionFailed:
            pass  # the same photo again
        except Exception:
            path = None
        if path:
            image = {"path": path, "sha256": sha, "content_type": kind}
    return delivery.proof(found.get("status"), found.get("at") or datetime.now(UTC), "provider", image, found.get("image_url"))


def lalamove_rule(provider_order_id: str, raw_status: str | None, eid: str, at: datetime, order_data: dict, proofs: bool = False):
    """Link the Lalamove order to its shipment if this is the first we hear of it, then record the status event, the driver
    (with the name and plate Lalamove gives while the order is active) and any pickup or drop-off proof."""
    now = datetime.now(UTC)
    status = delivery.LALAMOVE.get(raw_status or "") or ("driver_assigned" if raw_status == "DRIVER_ASSIGNED" else None)
    driver_id = order_data.get("driverId") or None
    driver = None
    if driver_id:
        details = (lalamove.lalamove_driver(provider_order_id, driver_id) if status in ("driver_assigned", "picked_up") else None) or {}
        driver = delivery.driver_record("provider", driver_id, details.get("name"), details.get("plate"), at)
    found = lalamove.proofs(order_data)
    if not found and (proofs or status in ("picked_up", "delivered")):
        found = lalamove.lalamove_proofs(provider_order_id)
    pop, pod = keep_proof(provider_order_id, found.get("pop")), keep_proof(provider_order_id, found.get("pod"))
    replaced = raw_status == "ORDER_REPLACED"  # kept as an event on the shipment: a flag on any later claim

    def rule(order: dict) -> dict:
        updates = {}
        if not (order.get("fulfillment") or {}).get("provider_order_id"):
            updates = delivery.booked(order, provider_order_id, order_data.get("shareLink"), now)
            order = {**order, **updates}
        if status or pop or pod or driver or replaced:
            current = (order.get("fulfillment") or {}).get("status")
            ev = delivery.event(eid, "provider", raw_status or "PROOF", status or current, at, now)
            updates.update(delivery.apply_event(order, ev, share_link=order_data.get("shareLink"), pod=pod, pop=pop, driver=driver))
        return updates

    return rule
