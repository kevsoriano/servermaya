"""The API. Routes live in routes/, one module per area; shared code in core/, integrations/ and services/; business rules,
as pure functions, in domain/."""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import auth, config
from routes import catalog, claims, conversations, customer, demo, dispatch, gcash, orders, packing, patterns, refunds, returns, webhooks

app = FastAPI()
# Only the app's own pages, and only for what they send: a bearer token, JSON or a file, the PDF password.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.APP_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Pdf-Password"],
    max_age=3600,
)


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    # How long after the customer's last message Messenger lets a seller reply: 24 hours, or 7 days with the Human Agent tag
    maps = {"key": config.MAPS_BROWSER_KEY, "map_id": config.MAPS_MAP_ID} if config.MAPS_BROWSER_KEY else None
    return {**user, "reply_hours": 168 if config.HUMAN_AGENT else 24, "maps": maps}


# FastAPI matches routes in the order they were added: these keep the order main.py had before it was split.
for area in (conversations, demo, orders, customer, webhooks, packing, gcash, dispatch, claims, refunds, returns, patterns, catalog):
    app.include_router(area.router)
