import os

from fastapi import Depends, FastAPI, Header, HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")
# Comma-separated. Empty means nobody gets in.
ALLOWLIST = {e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAIL", "").split(",") if e.strip()}

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
