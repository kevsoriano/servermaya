import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

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


def proofs(order_data: dict) -> dict:
    """Pickup (POP) and drop-off (POD) proofs from an order's stops, as Lalamove reports them: {"pop": ..., "pod": ...}, each
    {status, image_url, at} or missing. Missing means unavailable, not failed. Lalamove documents POD on each stop; POP's
    fields are not documented, so it is read the same way, from the pickup stop, and left out when it is not there."""
    stops = order_data.get("stops") or []
    out = {}
    for key, stop, when in (("pop", stops[0] if stops else {}, "pickedUpAt"), ("pod", stops[-1] if len(stops) > 1 else {}, "deliveredAt")):
        p = (stop or {}).get(key.upper()) or {}
        if p:
            out[key] = {"status": p.get("status"), "image_url": p.get("image"), "at": lalamove_time(p.get(when) or p.get("updatedAt"))}
    return out


def lalamove_proofs(provider_order_id: str) -> dict:
    try:
        return proofs(lalamove("GET", f"/v3/orders/{provider_order_id}"))
    except (HTTPException, LalamoveNoAnswer):
        return {}


def lalamove_driver(provider_order_id: str, driver_id: str) -> dict | None:
    """The driver's name and masked plate, kept as they were for this order. Lalamove serves them only while the order is
    active (403 afterwards), so they are fetched when the driver is matched. The phone and live location are not kept."""
    try:
        d = lalamove("GET", f"/v3/orders/{provider_order_id}/drivers/{driver_id}")
    except (HTTPException, LalamoveNoAnswer):
        return None
    return {"name": d.get("name") or None, "plate": d.get("plateNumber") or None}


IMAGE_HOSTS = ("lalamove.com",)  # the sandbox's proof images came from sg-oimg-pre.lalamove.com, over plain http
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def proof_image(url: str) -> tuple[bytes, str] | None:
    """A proof photo's bytes and type, downloaded only from Lalamove's own hosts. None if it cannot be had."""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    if parsed.scheme not in ("https", "http") or not (host == IMAGE_HOSTS[0] or host.endswith("." + IMAGE_HOSTS[0])):
        return None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not kind.startswith("image/") or len(r.content) > MAX_IMAGE_BYTES:
        return None
    return r.content, kind
