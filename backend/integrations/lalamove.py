import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime

import requests
from fastapi import HTTPException

from core import config


class LalamoveNoAnswer(Exception):
    """No answer, or a server error: the request may or may not have taken effect."""


def lalamove_signature(timestamp: str, method: str, path: str, body: str) -> str:
    """Lalamove v3: HMAC-SHA256 of "<timestamp>\\r\\n<METHOD>\\r\\n<path>\\r\\n\\r\\n<body>" with the API secret.
    Webhooks are signed the same way, over the compact JSON of their data. Checked against real sandbox events."""
    return hmac.new(config.LALAMOVE_SECRET.encode(), f"{timestamp}\r\n{method}\r\n{path}\r\n\r\n{body}".encode(), hashlib.sha256).hexdigest()


def lalamove(method: str, path: str, body: dict | None = None) -> dict:
    if not config.LALAMOVE_KEY or not config.LALAMOVE_SECRET:
        raise HTTPException(503, "Lalamove is not configured")
    raw = json.dumps(body, separators=(",", ":")) if body else ""
    ts = str(int(time.time() * 1000))
    headers = {
        "Authorization": f"hmac {config.LALAMOVE_KEY}:{ts}:{lalamove_signature(ts, method, path, raw)}",
        "Market": "PH",
        "Content-Type": "application/json",
    }
    try:
        r = requests.request(method, config.LALAMOVE_URL + path, data=raw or None, headers=headers, timeout=15)
    except requests.RequestException:
        raise LalamoveNoAnswer()
    if r.status_code >= 500:
        raise LalamoveNoAnswer()
    try:
        payload = r.json() if r.content else {}
    except ValueError:
        payload = {}
    if not r.ok:
        problems = "; ".join(e.get("message") or e.get("id", "") for e in payload.get("errors", [])) or f"HTTP {r.status_code}"
        raise HTTPException(502, f"Lalamove: {problems}")
    return payload.get("data") or {}


def lalamove_time(text: str | None) -> datetime:
    """Lalamove sends times like 2026-10-13T19:05.00Z; the minute is what is reliable."""
    found = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", text or "")
    return datetime.fromisoformat(found[1]).replace(tzinfo=UTC) if found else datetime.now(UTC)


def lalamove_pod(provider_order_id: str) -> dict | None:
    """Proof of delivery for the drop-off stop, as Lalamove reports it. Missing POD means unavailable, not failed."""
    try:
        stops = lalamove("GET", f"/v3/orders/{provider_order_id}").get("stops", [])
    except (HTTPException, LalamoveNoAnswer):
        return None
    pod = (stops[-1].get("POD") or {}) if stops else {}
    if not pod:
        return None
    # ponytail: keeps Lalamove's image link, which may expire; copy the image into the evidence bucket when retention lands
    return {
        "status": pod.get("status"),
        "image_url": pod.get("image"),
        "delivered_at": pod.get("deliveredAt"),
        "source": "lalamove",
        "fetched_at": datetime.now(UTC),
    }
