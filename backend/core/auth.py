from datetime import datetime

from fastapi import Header, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.cloud import firestore
from google.oauth2 import id_token

from core import config, db
from domain import receipts as gcash
from seed import demo
from services import chats

_http = google_requests.Request()


def current_user(request: Request, authorization: str = Header(default="")) -> dict:
    """Verify the Firebase ID token and the email allowlist. Every protected route depends on this.

    Anonymous Firebase sign-ins are demo visitors: they pass without the allowlist, but only reach their own demo chats and
    the orders made from them (see demo_gate)."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = id_token.verify_firebase_token(token, _http, audience=config.PROJECT_ID)
    except Exception:
        raise HTTPException(401, "Invalid token")
    if claims.get("iss") != f"https://securetoken.google.com/{config.PROJECT_ID}":
        raise HTTPException(401, "Invalid token")
    if (claims.get("firebase") or {}).get("sign_in_provider") == "anonymous":
        user = {"uid": claims["sub"], "email": "demo visitor", "name": "Demo visitor", "demo": True}
        demo_gate(request, user)
        return user
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in config.ALLOWLIST:
        raise HTTPException(403, "This account is not allowed")
    return {"uid": claims["sub"], "email": email, "name": claims.get("name"), "demo": False}


# Routes a demo visitor may call that name no chat or order. Routes with a chat or order in the path are allowed only for
# the visitor's own; every other route is refused, so a new route stays closed to demo visitors until it is listed here.
DEMO_ROUTES = {
    ("GET", "/api/me"),
    ("GET", "/api/conversations"),
    ("POST", "/api/demo/threads"),
    ("POST", "/api/demo/reset"),
    ("GET", "/api/orders"),
    ("GET", "/api/settings/gcash"),
    ("GET", "/api/settings/gcash/qr"),  # the demo shop's sample QR
    ("GET", "/api/settings/dispatch"),
    ("GET", "/api/patterns"),
    ("GET", "/api/patterns/returns"),
    ("POST", "/api/gcash/statements"),
    ("GET", "/api/demo/sample-statement"),
    ("GET", "/api/catalog"),
    ("GET", "/api/catalog/{product_id}/photos/{n}"),
}


def demo_gate(request: Request, user: dict):
    params = request.path_params
    if "conversation_id" in params:
        cid = params["conversation_id"]
        if not demo.is_demo(cid) or chats.demo_doc(cid).get("owner") != user["uid"]:
            raise HTTPException(404, "Demo chat not found")
    if "order_id" in params:
        snap = db.orders().document(params["order_id"]).get()
        if not snap.exists or not visible(snap.to_dict(), user):
            raise HTTPException(404, "Order not found")
    if not params.keys() & {"conversation_id", "order_id"} and (request.method, getattr(request.scope.get("route"), "path", "")) not in DEMO_ROUTES:
        raise HTTPException(403, "Not available in the demo")


def visible(order: dict, user: dict) -> bool:
    """Demo orders belong to whoever made their chat; everything else is the store's, closed to demo visitors."""
    source = order.get("source") or {}
    return source.get("owner") == user["uid"] if source.get("type") == "demo" else not user.get("demo")


def ai_budget(user: dict, n: int = 1):
    """Count n Gemini calls for a demo visitor before making them, or refuse with 429. Sellers are never limited.

    Counted when asked, not when Gemini answers, so a failed call still counts: the limit is on cost, and a failure costs too.
    Every visitor's AI call adds to the same daily total, and transactions on one document abort each other under load, so
    the counters are read once and then only incremented by the server. Calls racing at the very edge of a limit can overshoot
    it by a few: it bounds cost, it is not an exact count.
    """
    if not user.get("demo"):
        return
    day = datetime.now(gcash.PHT).strftime("%Y-%m-%d")
    mine = db.store_doc().collection("demoAiUse").document(f"{user['uid']}_{day}")
    total = db.store_doc().collection("demoAiTotals").document(day)
    used = {s.id: (s.to_dict() or {}).get("count", 0) for s in db.clients()[1].get_all([mine, total])}
    if problem := demo.over_budget(used.get(mine.id, 0), used.get(total.id, 0), n):
        raise HTTPException(429, problem)
    batch = db.clients()[1].batch()
    for ref in (mine, total):
        batch.set(ref, {"day": day, "count": firestore.Increment(n)}, merge=True)
    batch.commit()
