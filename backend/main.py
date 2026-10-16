"""The API. Routes live in routes/, one module per area; shared code in core/, integrations/ and services/; business rules,
as pure functions, in domain/."""

from fastapi import Depends, FastAPI

from core import auth
from routes import claims, conversations, customer, demo, dispatch, gcash, orders, packing, patterns, refunds, webhooks

app = FastAPI()


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    return user


# FastAPI matches routes in the order they were added: these keep the order main.py had before it was split.
for area in (conversations, demo, orders, customer, webhooks, packing, gcash, dispatch, claims, refunds, patterns):
    app.include_router(area.router)
