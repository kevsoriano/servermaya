"""The shop's catalog: products, each variant's price and stock, and an optional reference photo per variant.

The catalog is the seller's own record, so code may use it. Gemini only says which catalog line an item is; code looks up
every value from that line. A price no seller message states comes from the catalog, and says so. A chat price that differs
from the catalog, and stock that runs short, are noted for the seller: the price the customer agreed to in the chat stands.
"""

from pydantic import BaseModel, Field


class VariantIn(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    price: int = Field(ge=1, le=10_000_000, description="Centavos")
    stock: int | None = Field(default=None, ge=0, le=1_000_000, description="None: not tracked")


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    variants: list[VariantIn] = Field(min_length=1, max_length=20)


def lines(products: list[dict]) -> list[tuple[dict, dict]]:
    """Every (product, variant), in order: the catalog numbered from 1 as Gemini sees it."""
    return [(p, v) for p in products for v in p["variants"]]


def snapshot(products: list[dict], n: int | None) -> dict | None:
    """The catalog line an item was matched to, as it stood when the order was drafted. None for no or a made-up number."""
    found = lines(products)
    if n is None or not 1 <= n <= len(found):
        return None
    p, v = found[n - 1]
    return {"product_id": p["id"], "name": p["name"], "variant": v["name"], "price": v["price"], "stock": v.get("stock"), "photo": v.get("photo")}


def money(centavos: int) -> str:
    return f"₱{centavos / 100:,.2f}"


def notes(draft: dict) -> list[str]:
    """What the seller should know from their catalog: a chat price that differs from it, and stock that is short."""
    out = []
    for i in draft.get("items", []):
        c = i.get("catalog")
        if not c:
            continue
        label = c["name"] + (f" ({c['variant']})" if c["variant"] else "")
        if i.get("price_from") == "chat" and i["unit_price"] != c["price"]:
            out.append(
                f"{label}: the chat says {money(i['unit_price'])}, your catalog says {money(c['price'])}. The chat price is what the customer agreed to."
            )
        if c["stock"] == 0:
            out.append(f"{label}: out of stock in your catalog.")
        elif c["stock"] is not None and i["quantity"] > c["stock"]:
            out.append(f"{label}: the order asks for {i['quantity']}, your catalog has {c['stock']} in stock.")
    return out
