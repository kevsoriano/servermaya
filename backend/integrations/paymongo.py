import hashlib
import hmac

import requests
from fastapi import HTTPException

from core import config


# PayMongo. It owns payment truth: an order is only marked paid from a checkout session we fetched ourselves.
def paymongo(method: str, path: str, attributes: dict | None = None) -> dict:
    if not config.PAYMONGO_KEY:
        raise HTTPException(503, "Payments are not configured")
    try:
        r = requests.request(
            method,
            f"https://api.paymongo.com/v1/{path}",
            auth=(config.PAYMONGO_KEY, ""),
            json={"data": {"attributes": attributes}} if attributes else None,
            timeout=15,
        )
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach PayMongo. Payment status is unknown; try again.")
    if not r.ok:
        raise HTTPException(502, "PayMongo: " + "; ".join(e.get("detail", "") for e in body.get("errors", [])))
    return body["data"]


def valid_signature(header: str, raw: bytes, secret: str) -> bool:
    """Paymongo-Signature is 't=<timestamp>,te=<test sig>,li=<live sig>'; the signature is HMAC-SHA256 of '<t>.<raw body>'."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    expected = hmac.new(secret.encode(), f"{parts.get('t', '')}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, parts.get(k, "")) for k in ("te", "li"))


# Refunds. PayMongo first; its refusal or silence leads to the manual route, never to a guess.
def paymongo_refund(payment_id: str, amount: int, reason: str, op_id: str, order_id: str) -> dict:
    """{"refund": {id, status}} if accepted, {"refused": detail} if PayMongo said no, {} if no answer came."""
    try:
        r = requests.post(
            "https://api.paymongo.com/v1/refunds",
            auth=(config.PAYMONGO_KEY, ""),
            timeout=20,
            json={
                "data": {
                    "attributes": {
                        "amount": amount,
                        "payment_id": payment_id,
                        "reason": reason,
                        "metadata": {"operation_id": op_id, "order_id": order_id},
                    }
                }
            },
        )
        body = r.json()
    except (requests.RequestException, ValueError):
        return {}
    if r.status_code >= 500:
        return {}
    if not r.ok:
        return {"refused": "; ".join(e.get("detail", "") for e in body.get("errors", [])) or f"HTTP {r.status_code}"}
    return {"refund": {"id": body["data"]["id"], "status": body["data"]["attributes"]["status"]}}


def provider_refunds(payment_id: str) -> list[dict]:
    attrs = paymongo("GET", f"payments/{payment_id}")["attributes"]
    out = []
    for x in attrs.get("refunds") or []:
        a = x.get("attributes", x)
        out.append({"id": x.get("id") or a.get("id"), "amount": a["amount"], "status": a["status"], "metadata": a.get("metadata") or {}})
    return out
