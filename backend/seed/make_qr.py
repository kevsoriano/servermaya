"""Make the demo shop's sample GCash QR once; the JPEG is kept in seed/images and committed.

Run: cd backend && uv run --with segno python -m seed.make_qr   (segno draws the QR; only needed here, so not a dependency)

The demo shop's GCash account is made up, so its QR must not pay anyone: the code holds a plain sentence, not a GCash
account, and the image is stamped as a sample. A real seller saves a screenshot of their own QR in Settings instead.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core import config

OUT = Path(__file__).parent / "images" / "gcash-qr-sample.jpg"
PAYLOAD = "SAMPLE ONLY - demo shop - not a GCash account - do not pay"


def main() -> None:
    import segno

    buf = io.BytesIO()
    segno.make(PAYLOAD, error="m").save(buf, kind="png", scale=12, border=2)
    code = Image.open(buf).convert("RGB")
    w = code.width + 120
    card = Image.new("RGB", (w, code.height + 300), (0, 112, 224))  # GCash blue
    d = ImageDraw.Draw(card)
    font = lambda n: ImageFont.load_default(size=n)
    d.text((w // 2, 40), "GCash", font=font(44), fill="white", anchor="mm")
    d.text((w // 2, 95), config.DEMO_GCASH["name"], font=font(30), fill="white", anchor="mm")
    card.paste(code, (60, 140))
    d.rectangle((40, code.height + 170, w - 40, code.height + 260), fill="white")
    d.text((w // 2, code.height + 200), "SAMPLE · DO NOT SCAN OR PAY", font=font(30), fill=(198, 40, 40), anchor="mm")
    d.text((w // 2, code.height + 238), "Demo shop: not a real GCash account", font=font(20), fill=(90, 90, 90), anchor="mm")
    card.save(OUT, "JPEG", quality=90)
    print(f"wrote {OUT.name}: {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
