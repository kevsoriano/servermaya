from datetime import UTC, datetime

from domain import delivery
from integrations import lalamove


def lalamove_rule(provider_order_id: str, raw_status: str | None, eid: str, at: datetime, order_data: dict):
    """Link the Lalamove order to its shipment if this is the first we hear of it, then record the status event."""
    now = datetime.now(UTC)
    status = delivery.LALAMOVE.get(raw_status or "") or ("driver_assigned" if raw_status == "DRIVER_ASSIGNED" else None)
    pod = lalamove.lalamove_pod(provider_order_id) if status == "delivered" else None

    def rule(order: dict) -> dict:
        updates = {}
        if not (order.get("fulfillment") or {}).get("provider_order_id"):
            updates = delivery.booked(order, provider_order_id, order_data.get("shareLink"), now)
            order = {**order, **updates}
        if status:
            ev = delivery.event(eid, "provider", raw_status, status, at, now)
            updates.update(
                delivery.apply_event(order, ev, driver_id=order_data.get("driverId") or None, share_link=order_data.get("shareLink"), pod=pod)
            )
        return updates

    return rule
