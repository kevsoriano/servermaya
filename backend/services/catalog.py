"""Where a catalog comes from: the store's, in Firestore, or the fixed demo catalog for demo visitors and demo orders."""

from core import db
from seed import catalog as demo_catalog


def docs():
    return db.store_doc().collection("catalog")


def products(demo: bool) -> list[dict]:
    if demo:
        return demo_catalog.DEMO
    return sorted(({"id": d.id, **d.to_dict()} for d in docs().stream()), key=lambda p: p["name"].lower())


def photo_bytes(photo: dict) -> bytes:
    """A reference photo: a seller's from Storage, or a demo one from seed/images."""
    if photo["path"].startswith("demo/"):
        return (demo_catalog.IMAGES / photo["path"].removeprefix("demo/")).read_bytes()
    return db.bucket().blob(photo["path"]).download_as_bytes()
