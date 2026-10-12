import os
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from google import genai
from google.cloud import firestore
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

import extraction
import orders as rules

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")
# Comma-separated. Empty means nobody gets in.
ALLOWLIST = {e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAIL", "").split(",") if e.strip()}
PAGE_ID = os.environ.get("MESSENGER_PAGE_ID", "")
PAGE_TOKEN = os.environ.get("MESSENGER_PAGE_TOKEN", "")
GRAPH = "https://graph.facebook.com"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# ponytail: one store, add real store ids when a second seller onboards
STORE = "main"

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
    return fetch_messages(conversation_id)


def fetch_messages(conversation_id: str) -> list[dict]:
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


@lru_cache
def clients():
    """Created on first use so tests and Messenger-only routes need no Google credentials."""
    return genai.Client(vertexai=True, project=PROJECT_ID, location="global"), firestore.Client(project=PROJECT_ID)


def orders():
    return clients()[1].collection("stores").document(STORE).collection("orders")


@app.post("/api/conversations/{conversation_id}/draft-order")
def draft_order(conversation_id: str, user: dict = Depends(current_user)):
    messages = conversation(conversation_id, user)
    if not messages:
        raise HTTPException(400, "Conversation has no messages")
    try:
        draft = extraction.extract(clients()[0], GEMINI_MODEL, messages)
    except Exception:
        raise HTTPException(502, "Could not extract an order from this conversation")
    order = {
        "status": "draft",
        "version": 1,
        "source": {"type": "messenger", "conversation_id": conversation_id},
        "messages": messages,  # snapshot of the evidence the draft was built from
        "draft": draft,  # copy of the current version, for listing
        "confirmations": [],
        "confirm_token": None,
        "model": GEMINI_MODEL,
        "created_by": user["email"],
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    ref = orders().document()
    batch = clients()[1].batch()
    batch.set(ref, order)
    batch.set(ref.collection("versions").document("1"), version_doc(draft, "ai_extraction", user["email"]))
    batch.commit()
    return present(ref.get())


def version_doc(draft: dict, origin: str, by: str) -> dict:
    return {"draft": draft, "origin": origin, "created_by": by, "created_at": firestore.SERVER_TIMESTAMP}


def present(snap) -> dict:
    # Defaults cover drafts saved before confirmations existed.
    order = {"confirmations": [], "confirm_token": None, **snap.to_dict()}
    return {**order, "id": snap.id, "missing": rules.missing(order["draft"])}


def change(ref, rule, by: str = "") -> dict:
    """Apply one of the rules in orders.py inside a transaction, so an edit and a confirmation cannot interleave."""

    @firestore.transactional
    def run(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise HTTPException(404, "Order not found")
        try:
            updates = rule(snap.to_dict())
        except rules.Refused as e:
            raise HTTPException(409, str(e))
        if updates:
            tx.update(ref, updates)
        if "draft" in updates:  # versions are written once and never changed
            tx.set(ref.collection("versions").document(str(updates["version"])), version_doc(updates["draft"], "seller_edit", by))

    run(clients()[1].transaction())
    return present(ref.get())


@app.get("/api/orders")
def list_orders(user: dict = Depends(current_user)):
    docs = orders().order_by("created_at", direction=firestore.Query.DESCENDING).limit(25).stream()
    return [present(d) for d in docs]


@app.put("/api/orders/{order_id}")
def edit_order(order_id: str, body: rules.Edit, user: dict = Depends(current_user)):
    return change(orders().document(order_id), lambda o: rules.edit(o, body), user["email"])


@app.post("/api/orders/{order_id}/send-confirmation")
def send_confirmation(order_id: str, user: dict = Depends(current_user)):
    return change(orders().document(order_id), lambda o: rules.send(o, secrets.token_urlsafe(32)))


@app.post("/api/orders/{order_id}/assert-confirmation")
def assert_confirmation(order_id: str, user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    return change(orders().document(order_id), lambda o: rules.confirm(o, "seller_asserted", user["email"], now))


# Customer link routes. No sign-in: the unguessable token in the link is the credential.
def by_token(token: str):
    # ponytail: token stored as-is so the seller can re-copy the link; store a hash if the DB ever gets other readers
    found = list(orders().where(filter=firestore.FieldFilter("confirm_token", "==", token)).limit(1).stream()) if token else []
    if not found:
        raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
    return found[0]


def customer_view(order: dict) -> dict:
    """Only what the customer needs to check. No chat, claims or seller details."""
    d = order["draft"]
    return {
        "status": order["status"],
        "items": [{k: i[k] for k in ("name", "variant", "quantity", "unit_price")} for i in d["items"]],
        "total": d["total"],
        **{f: d[f]["value"] for f in rules.FIELDS},
    }


@app.get("/api/public/orders/{token}")
def public_order(token: str):
    return customer_view(by_token(token).to_dict())


@app.post("/api/public/orders/{token}/confirm")
def public_confirm(token: str):
    now = datetime.now(timezone.utc)

    def rule(order: dict) -> dict:
        if order.get("confirm_token") != token:  # edited between lookup and transaction
            raise HTTPException(404, "This link is no longer valid. Please ask the seller for a new one.")
        return rules.confirm(order, "customer", "confirmation_link", now)

    return customer_view(change(by_token(token).reference, rule))
