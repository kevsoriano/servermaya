import io
from datetime import UTC

import pytest


def test_packing_rules():
    from datetime import datetime

    from domain import orders as rules
    from domain import packing as pack

    now = datetime.now(UTC)
    items = [
        {"name": "mug", "variant": "blue", "quantity": 2, "unit_price": 18000, "sources": []},
        {"name": "tumbler", "variant": None, "quantity": 1, "unit_price": 35000, "sources": []},
    ]
    order = {"status": "confirmed", "version": 1, "draft": {"items": items}, "payment": {"status": "pending", "version": 1}}
    with pytest.raises(rules.Refused):
        pack.require_ready(order)  # not paid yet
    order["payment"] = {"status": "verified", "version": 1}
    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # no photos
    order |= pack.add_photo(order, {"id": "p1"})
    order |= pack.add_photo(order, {"id": "p2"})

    seen = lambda line, **k: pack.Seen(**{"line": line, "visible": True, "counts": [], "damage": None, "photos": [1], "note": "", **k})
    c = lambda *pairs: [pack.Count(variant=v, quantity=q) for v, q in pairs]
    # The PRD case: two blue mugs expected, one blue and one red in the photo.
    report = pack.Report(
        lines=[seen(1, counts=c(("blue", 1), ("red", 1))), seen(2, counts=c((None, 1)), photos=[7])], suggestions=["Lay the mugs side by side"]
    )
    order |= pack.apply_check(order, report, "m", now)
    assert [r["auto_result"] for r in order["packing"]["rows"]] == ["conflict", "insufficient_visibility"]
    assert order["packing"]["rows"][1]["photos"] == []  # citation of a photo that does not exist is dropped

    # A photo code finds too blurred to count in cannot back a match, whatever Gemini read in it.
    two_blue = pack.Report(lines=[seen(1, counts=c(("blue", 2))), seen(2, counts=c((None, 1)))], suggestions=[])
    blurred = pack.apply_check(order, two_blue, "m", now, too_unclear={1})["packing"]
    assert [r["auto_result"] for r in blurred["rows"]] == ["insufficient_visibility", "insufficient_visibility"]
    assert blurred["suggestions"][0].startswith("Photo 1 is too blurry")
    assert pack.apply_check(order, two_blue, "m", now)["packing"]["rows"][0]["auto_result"] == "matched"

    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    def png(im):
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()

    im = Image.new("RGB", (1024, 768), (233, 220, 201))
    ImageDraw.Draw(im).rounded_rectangle((300, 250, 420, 420), 18, fill=(40, 90, 200), outline=(0, 30, 120), width=3)
    sharp, blurry, dark = png(im), png(im.filter(ImageFilter.GaussianBlur(10))), png(ImageEnhance.Brightness(im).enhance(0.06))
    assert pack.unclear([sharp, blurry, dark, b"not an image"]) == {2, 3}  # an unreadable format is not judged

    blue2 = {"variant": "blue", "quantity": 2}
    assert pack.result(blue2, seen(1, counts=c(("Blue", 2)))) == "matched"
    assert pack.result(blue2, seen(1, counts=c(("light blue", 1), ("blue", 1)))) == "matched"
    assert pack.result(blue2, seen(1, counts=c(("red", 2)))) == "conflict"
    assert pack.result(blue2, seen(1, counts=c(("blue", 1)))) == "conflict"  # missing one
    assert pack.result(blue2, seen(1, counts=c(("blue", 3)))) == "conflict"  # one too many
    assert pack.result(blue2, seen(1, counts=c(("blue", 2)), damage="chipped rim")) == "conflict"
    assert pack.result(blue2, seen(1, counts=c((None, 2)))) == "insufficient_visibility"
    assert pack.result(blue2, seen(1, counts=[])) == "insufficient_visibility"  # could not count
    assert pack.result(blue2, seen(1, counts=c(("blue", 2)), photos=[])) == "insufficient_visibility"  # no photo cited
    assert pack.result({"variant": None, "quantity": 1}, seen(2, visible=False, counts=c((None, 1)))) == "insufficient_visibility"
    assert pack.result({"variant": None, "quantity": 1}, seen(2, counts=c((None, 1)))) == "matched"
    assert pack.result(blue2, None) == "insufficient_visibility"

    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # mug conflict unresolved
    order |= pack.confirm_row(order, 1, "me", "Swapped the red mug for a blue one", now)
    with pytest.raises(rules.Refused):
        pack.complete(order, "me", now)  # tumbler still unresolved
    with pytest.raises(rules.Refused):
        pack.confirm_row(order, 2, "me", "  ", now)
    order |= pack.confirm_row(order, 2, "me", "Checked tumbler by hand, sealed box", now)
    row = order["packing"]["rows"][1]
    assert pack.status(row) == "seller_confirmed" and row["auto_result"] == "insufficient_visibility"  # both kept
    order |= pack.complete(order, "me", now)
    assert order["packing"]["completed"]["by"] == "me"
    with pytest.raises(rules.Refused):
        pack.add_photo(order, {"id": "p3"})

    # Changing the order voids packing but keeps it on record.
    body = rules.Edit(items=[{"name": "mug", "variant": "blue", "quantity": 3, "unit_price": 18000}])
    full = {
        **order,
        "draft": {
            **order["draft"],
            "total": 36000,
            "recipient_name": {"value": None, "sources": []},
            "phone": {"value": None, "sources": []},
            "address": {"value": None, "sources": []},
            "delivery_instructions": {"value": None, "sources": []},
        },
    }
    up = rules.edit(full, body)
    assert up["packing"] is None and up["packing_history"][0]["completed"]


def test_packing_accepts_a_seller_confirmed_gcash_payment():

    from domain import orders as rules
    from domain import packing as pack

    order = {"status": "confirmed", "version": 2, "draft": {"items": [{"name": "mug", "variant": None, "quantity": 1}]}}
    with pytest.raises(rules.Refused):
        pack.require_ready(order)
    assert pack.paid_by({**order, "manual_payment": {"status": "claimed", "version": 2}}) is None  # a screenshot alone is not enough
    assert pack.paid_by({**order, "manual_payment": {"status": "seller_confirmed", "version": 1}}) is None  # an older version
    confirmed = {**order, "manual_payment": {"status": "seller_confirmed", "version": 2}}
    assert pack.paid_by(confirmed) == "gcash_seller_confirmed" and pack.require_ready(confirmed)["version"] == 2
    assert pack.paid_by({**confirmed, "payment": {"status": "verified", "version": 2}}) == "paymongo"
