import hashlib
import hmac
import json
import time

import requests
from fastapi import HTTPException

from core import config


def graph(path: str, send: dict | None = None, files: dict | None = None, **params) -> dict:
    """GET from the Graph API as the Page, or POST `send` as JSON. With `files`, POST `send` as a multipart form instead, objects
    JSON-encoded, as the Send API wants for uploads. Token goes in a header so it never lands in a URL or log."""
    if not config.PAGE_ID or not config.PAGE_TOKEN:
        raise HTTPException(503, "Messenger is not configured")
    form = {k: v if isinstance(v, str) else json.dumps(v) for k, v in (send or {}).items()}
    body = {"data": form, "files": files} if files else {"json": send}
    try:
        r = requests.request(
            "POST" if send else "GET",
            f"{config.GRAPH}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {config.PAGE_TOKEN}"},
            timeout=30 if files else 10,
            **body,
        )
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach Messenger")
    if not r.ok:
        raise HTTPException(502, body.get("error", {}).get("message", "Messenger request failed"))
    return body


def valid_signature(header: str, raw: bytes, secret: str) -> bool:
    """X-Hub-Signature-256 is 'sha256=' and the HMAC-SHA256 of the raw body, keyed by the app secret."""
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected, header or "")


_pics: dict[str, tuple[float, str | None]] = {}


PIC_TTL = 3600


# ponytail: in-process cache, so each Cloud Run instance fetches its own; fine at one store
def profile_pics(psids: list[str]) -> dict[str, str | None]:
    """Profile photo links by customer PSID, in one Graph call. Meta's links expire, so each is refetched after an hour.
    On any failure the photo is just missing and the app shows initials."""
    now = time.time()
    stale = [p for p in psids if p not in _pics or now - _pics[p][0] > PIC_TTL]
    if stale:
        try:
            data = graph("", ids=",".join(stale), fields="profile_pic")
            for p in stale:
                _pics[p] = (now, (data.get(p) or {}).get("profile_pic"))
        except HTTPException:
            pass
    return {p: _pics[p][1] if p in _pics else None for p in psids}


def attachments(message: dict) -> list[dict]:
    """Normalise Messenger attachments to {kind, mime_type, name, url, preview_url}. Anything without an https link is dropped."""
    out = []
    for a in message.get("attachments", {}).get("data", []):
        mime = a.get("mime_type") or ""
        media = a.get("image_data") or a.get("video_data") or {}
        kind = mime.split("/")[0] if mime.split("/")[0] in ("image", "video", "audio") else "file"
        out.append(
            {
                "kind": kind,
                "mime_type": mime,
                "name": a.get("name"),
                "url": media.get("url") or a.get("file_url"),
                "preview_url": media.get("preview_url"),
            }
        )
    if message.get("sticker"):
        out.append({"kind": "image", "mime_type": "", "name": "sticker", "url": message["sticker"], "preview_url": None})
    # ponytail: these are Meta's temporary links; copy the files into our own storage (with hashes) when evidence retention lands
    return [a for a in out if str(a["url"]).startswith("https://")]


def fetch_messages(conversation_id: str) -> list[dict]:
    data = graph(
        f"{conversation_id}/messages",
        fields="id,message,from,created_time,sticker,attachments{id,mime_type,name,size,file_url,image_data,video_data}",
        limit=50,
    )
    return [
        {
            "id": m["id"],
            "text": m.get("message", ""),
            "from_page": m.get("from", {}).get("id") == config.PAGE_ID,
            "from_name": m.get("from", {}).get("name"),
            "created_time": m.get("created_time"),
            "attachments": attachments(m),
        }
        for m in reversed(data.get("data", []))  # Graph returns newest first
    ]
