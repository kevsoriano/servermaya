from fastapi import HTTPException
from google.cloud import firestore

from core import config, db
from domain import claims as cases
from domain import orders as rules
from domain import packing as pack


# Customer link routes. No sign-in: the unguessable token in the link is the credential.
def by_token(token: str):
    # ponytail: token stored as-is so the seller can re-copy the link; store a hash if the DB ever gets other readers
    found = list(db.orders().where(filter=firestore.FieldFilter("confirm_token", "==", token)).limit(1).stream()) if token else []
    if not found:
        raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
    return found[0]


def customer_view(order: dict) -> dict:
    """Only what the customer needs to check. No chat, claims or seller details."""
    d = order["draft"]
    shipment = order.get("fulfillment") or (order.get("fulfillment_history") or [None])[-1]
    return {
        "status": order["status"],
        "items": [{k: i[k] for k in ("name", "variant", "quantity", "unit_price")} for i in d["items"]],
        "total": d["total"],
        **{f: d[f]["value"] for f in rules.FIELDS},
        "payment": (order.get("payment") or {}).get("status"),
        # Before the first payment request, the key decides: PayMongo's test-mode QR codes are real and must not be scanned.
        "test_mode": order["payment"]["livemode"] is False if order.get("payment") else config.PAYMONGO_KEY.startswith("sk_test_"),
        "gcash_confirmed": pack.paid_by(order) == "gcash_seller_confirmed",
        "shipment": shipment
        and {
            "status": shipment["status"],
            "courier": shipment["courier"],
            "simulated": shipment["courier"] == "simulator",
            "share_link": shipment.get("share_link"),  # only the customer holding this order's link sees it
        },
        "received": (order.get("receipt") or {}).get("fulfillment_id") == (shipment or {}).get("id") and bool(shipment),
        "claims": [
            {"id": c["id"], "type": cases.TYPES[c["type"]], "status": c["status"], "created_at": c["created_at"], "photos": len(c["photos"])}
            for c in order.get("claims", [])
        ],
    }
