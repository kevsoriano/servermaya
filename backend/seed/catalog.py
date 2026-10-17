"""The demo shop's catalog: every product the demo chats sell, with prices, stock and a reference photo per variant.

The photos are AI-generated samples (seed/make_images.py), served from seed/images. Lavender candles are down to 1, so a
two-candle order shows the stock note.
"""

import io
from pathlib import Path

from PIL import Image, ImageFilter

IMAGES = Path(__file__).parent / "images"


def photo(name: str) -> dict:
    return {"path": f"demo/{name}.jpg", "content_type": "image/jpeg"}


DEMO = [
    {
        "id": "mug",
        "name": "Ceramic mug",
        "variants": [
            {"name": "Blue", "price": 18000, "stock": 12, "photo": photo("mug-blue")},
            {"name": "White", "price": 18000, "stock": 8, "photo": photo("mug-white")},
        ],
    },
    {
        "id": "tote",
        "name": "Tote bag",
        "variants": [
            {"name": "Black", "price": 25000, "stock": 4, "photo": photo("tote-black")},
            {"name": "Cream", "price": 25000, "stock": 6, "photo": photo("tote-cream")},
        ],
    },
    {
        "id": "candle",
        "name": "Scented soy candle",
        "variants": [
            {"name": "Vanilla", "price": 22000, "stock": 5, "photo": photo("candle-vanilla")},
            {"name": "Lavender", "price": 22000, "stock": 1, "photo": photo("candle-lavender")},
        ],
    },
    {"id": "pouch", "name": "Linen pouch", "variants": [{"name": None, "price": 15000, "stock": 10, "photo": photo("pouch-linen")}]},
]


def line(product_id: str, variant: str | None) -> int:
    """The catalog line number (from 1) of a product and variant, as drafting numbers it, for the seeded orders."""
    found = [(p["id"], v["name"]) for p in DEMO for v in p["variants"]]
    return found.index((product_id, variant)) + 1


def image(name: str, blur: int = 0) -> bytes:
    """A seed image as JPEG bytes; blur: a Gaussian radius, for a shaky photo (8 is far below MIN_SHARPNESS)."""
    data = (IMAGES / f"{name}.jpg").read_bytes()
    if not blur:
        return data
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).filter(ImageFilter.GaussianBlur(blur)).save(out, "JPEG", quality=85)
    return out.getvalue()
