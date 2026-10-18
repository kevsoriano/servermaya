import hashlib
import hmac
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


MAX_ATTEMPTS = 10  # wrong codes per session; a long code makes guessing hopeless anyway
_admitted: dict[str, str] = {}  # uid -> the code key it was admitted with, kept per instance to save a read per request


def code_key(code: str) -> str:
    """What a session stores of the code it entered: never the code itself."""
    return hashlib.sha256(f"suri-access|{code}".encode()).hexdigest()[:32]


def visitors():
    return db.store_doc().collection("visitors")


def admitted(uid: str) -> bool:
    """Whether this session entered the current access code. A changed code admits nobody until they enter the new one."""
    if not config.ACCESS_CODE:
        return False
    current = code_key(config.ACCESS_CODE)
    if _admitted.get(uid) == current:
        return True
    if (visitors().document(uid).get().to_dict() or {}).get("code") == current:
        _admitted[uid] = current
        return True
    return False


def admit(uid: str, code: str) -> bool:
    """Check a code this session entered; on a match, the session is admitted for as long as the code stays the same."""
    if not config.ACCESS_CODE:
        raise HTTPException(503, "Access is closed for now.")
    ref = visitors().document(uid)
    tries = (ref.get().to_dict() or {}).get("attempts", 0)
    if tries >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many wrong codes. Sign out and ask for the access code again.")
    if not hmac.compare_digest(code.strip(), config.ACCESS_CODE):
        ref.set({"attempts": tries + 1}, merge=True)
        return False
    ref.set({"code": code_key(config.ACCESS_CODE), "admitted_at": firestore.SERVER_TIMESTAMP, "attempts": 0}, merge=True)
    _admitted[uid] = code_key(config.ACCESS_CODE)
    return True


# Before the code is entered, a session may only ask who it is and enter the code.
OPEN_ROUTES = {("GET", "/api/me"), ("POST", "/api/access")}


def current_user(request: Request, authorization: str = Header(default="")) -> dict:
    """Verify the Firebase ID token and the access code. Every protected route depends on this.

    Every visitor is an anonymous Firebase sign-in that has entered the access code: there are no accounts. A visitor reaches
    their own demo chats and orders, the shop's Messenger inbox and its orders, and the read-only settings (see demo_gate).
    "demo" is kept in the user for the visitor limits (the AI budget) and the demo shop's settings."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = id_token.verify_firebase_token(token, _http, audience=config.PROJECT_ID)
    except Exception:
        raise HTTPException(401, "Invalid token")
    if claims.get("iss") != f"https://securetoken.google.com/{config.PROJECT_ID}":
        raise HTTPException(401, "Invalid token")
    if (claims.get("firebase") or {}).get("sign_in_provider") != "anonymous":
        raise HTTPException(401, "Sign in with the access code")
    # what records say did something: a short form of the session, so two judges' actions can be told apart
    label = f"visitor {claims['sub'][:6]}"
    user = {"uid": claims["sub"], "email": label, "name": "Visitor", "demo": True}
    route = (request.method, getattr(request.scope.get("route"), "path", ""))
    if route in OPEN_ROUTES:
        return {**user, "admitted": admitted(user["uid"])}
    if not admitted(user["uid"]):
        raise HTTPException(403, "Enter the access code")
    demo_gate(request, user)
    return user


# Routes a visitor may call that name no chat or order. A route with a demo chat or order in the path is allowed only for the
# visitor's own; every other route is refused (the settings stay read-only), so a new route stays closed until it is listed.
DEMO_ROUTES = {
    ("GET", "/api/me"),
    ("POST", "/api/access"),
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
        cid = params["conversation_id"]  # a Messenger thread is the shop's, open to every visitor; its routes check its id
        if demo.is_demo(cid) and chats.demo_doc(cid).get("owner") != user["uid"]:
            raise HTTPException(404, "Demo chat not found")
    if "order_id" in params:
        snap = db.orders().document(params["order_id"]).get()
        if not snap.exists or not visible(snap.to_dict(), user):
            raise HTTPException(404, "Order not found")
    if not params.keys() & {"conversation_id", "order_id"} and (request.method, getattr(request.scope.get("route"), "path", "")) not in DEMO_ROUTES:
        raise HTTPException(403, "Not available in the demo")


def visible(order: dict, user: dict) -> bool:
    """Demo orders belong to whoever made their chat; everything else is the shop's (its Messenger orders), open to every visitor."""
    source = order.get("source") or {}
    return source.get("owner") == user["uid"] if source.get("type") == "demo" else True


def ai_budget(user: dict, n: int = 1):
    """Count n Gemini calls for a visitor before making them, or refuse with 429. Every visitor is counted.

    Counted when asked, not when Gemini answers, so a failed call still counts: the limit is on cost, and a failure costs too.
    Every visitor's AI call adds to the same daily total, and transactions on one document abort each other under load, so
    the counters are read once and then only incremented by the server. Calls racing at the very edge of a limit can overshoot
    it by a few: it bounds cost, it is not an exact count.
    """
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
