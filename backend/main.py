import os
import re

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")
# Comma-separated. Empty means nobody gets in.
ALLOWLIST = {e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAIL", "").split(",") if e.strip()}
PAGE_ID = os.environ.get("MESSENGER_PAGE_ID", "")
PAGE_TOKEN = os.environ.get("MESSENGER_PAGE_TOKEN", "")
GRAPH = "https://graph.facebook.com"

app = FastAPI()
_http = google_requests.Request()


def current_user(authorization: str = Header(default="")) -> dict:
    """Verify the Firebase ID token and the email allowlist. Every protected route depends on this."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = id_token.verify_firebase_token(token, _http, audience=PROJECT_ID)
    except Exception:
        raise HTTPException(401, "Invalid token")
    if claims.get("iss") != f"https://securetoken.google.com/{PROJECT_ID}":
        raise HTTPException(401, "Invalid token")
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in ALLOWLIST:
        raise HTTPException(403, "This account is not allowed")
    return {"uid": claims["sub"], "email": email, "name": claims.get("name")}


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


def graph(path: str, **params) -> dict:
    """GET from the Graph API as the Page. Token goes in a header so it never lands in a URL or log."""
    if not PAGE_ID or not PAGE_TOKEN:
        raise HTTPException(503, "Messenger is not configured")
    try:
        r = requests.get(f"{GRAPH}/{path}", params=params, headers={"Authorization": f"Bearer {PAGE_TOKEN}"}, timeout=10)
        body = r.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Could not reach Messenger")
    if not r.ok:
        raise HTTPException(502, body.get("error", {}).get("message", "Messenger request failed"))
    return body


# ponytail: first page only (25 threads, 50 messages), add paging when a Page outgrows it
@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user)):
    data = graph(f"{PAGE_ID}/conversations", platform="messenger", fields="id,updated_time,snippet,participants")
    return [
        {
            "id": c["id"],
            "updated_time": c.get("updated_time"),
            "snippet": c.get("snippet", ""),
            "customer": next((p for p in c.get("participants", {}).get("data", []) if p.get("id") != PAGE_ID), None),
        }
        for c in data.get("data", [])
    ]


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)):
    # The id goes into a Graph URL sent with the Page token, so only accept the thread-id shape.
    if not re.fullmatch(r"t_\d+", conversation_id):
        raise HTTPException(400, "Invalid conversation id")
    data = graph(f"{conversation_id}/messages", fields="id,message,from,created_time,attachments", limit=50)
    return [
        {
            "id": m["id"],
            "text": m.get("message", ""),
            "from_page": m.get("from", {}).get("id") == PAGE_ID,
            "from_name": m.get("from", {}).get("name"),
            "created_time": m.get("created_time"),
            "attachments": len(m.get("attachments", {}).get("data", [])),
        }
        for m in reversed(data.get("data", []))  # Graph returns newest first
    ]
