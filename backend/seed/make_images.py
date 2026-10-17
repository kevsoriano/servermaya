"""Make the demo's product photos once with Gemini's image model; the JPEGs are kept in seed/images and committed.

Run: cd backend && uv run python -m seed.make_images   (needs gcloud application-default credentials; about a minute)

The catalog references are generated first. Each demo packing or damage photo is then made from its product's reference,
so the mug in the packing photo is the same mug as in the catalog. Every image is stamped as an AI-generated sample: the
demo has no real product photos, and a real seller's catalog uses photos of their own products.
"""

import io
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

from core import config

MODEL = "gemini-3.1-flash-image"
OUT = Path(__file__).parent / "images"
STYLE = "Realistic photo, soft daylight, no text, no logos, no people."

REFERENCES = {
    "mug-blue": "Catalog photo: one plain deep-blue glazed ceramic coffee mug, side view, handle on the right, plain light grey background.",
    "mug-white": "Catalog photo: one plain white glazed ceramic coffee mug, side view, handle on the right, plain light grey background.",
    "tote-cream": "Catalog photo: one plain cream canvas tote bag with two shoulder straps, flat front view, plain light grey background.",
    "tote-black": "Catalog photo: one plain black canvas tote bag with two shoulder straps, flat front view, plain light grey background.",
    "candle-vanilla": "Catalog photo: one soy candle in a clear glass jar, ivory wax, plain kraft paper label with no writing, plain light grey background.",
    "candle-lavender": "Catalog photo: one soy candle in a clear glass jar, pale lavender-purple wax, plain kraft paper label with no writing, "
    "plain light grey background.",
    "pouch-linen": "Catalog photo: one small natural beige linen drawstring pouch, front view, plain light grey background.",
}

# name: (reference it is made from, what to show)
FROM_REFERENCE = {
    "packing-mug-blue": (
        "mug-blue",
        "The same mug as in this image, seen from above at an angle on a wooden packing table, next to an open cardboard box "
        "and a sheet of bubble wrap. The mug is whole and undamaged.",
    ),
    "damage-mug-blue": (
        "mug-blue",
        "The same mug as in this image, on a kitchen table next to opened packaging, with a clear crack running down its side "
        "and a small chip missing from the rim.",
    ),
    "packing-tote-cream": (
        "tote-cream",
        "The same tote bag as in this image, folded on a wooden packing table next to a grey poly mailer bag, seen from above.",
    ),
}


def generate(client, prompt: str, reference: bytes | None = None) -> bytes:
    contents = ([types.Part.from_bytes(data=reference, mime_type="image/jpeg")] if reference else []) + [f"{prompt} {STYLE}"]
    for attempt in range(6):  # the image model's quota allows few requests a minute
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="4:3")),
            )
            return next(p.inline_data.data for p in response.candidates[0].content.parts if p.inline_data)
        except genai.errors.ClientError as e:
            if e.code != 429 or attempt == 5:
                raise
            time.sleep(15 * (attempt + 1))


def stamped(data: bytes) -> bytes:
    """1024 px wide, stamped as an AI-generated sample, as JPEG."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((1024, 1024))
    d = ImageDraw.Draw(im)
    text, font = "SAMPLE IMAGE · AI GENERATED", ImageFont.load_default(size=24)
    w = d.textlength(text, font=font)
    d.rounded_rectangle((im.width - w - 36, im.height - 52, im.width - 12, im.height - 12), 8, fill=(255, 255, 255))
    d.text((im.width - w - 24, im.height - 46), text, font=font, fill=(198, 40, 40))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def main() -> None:
    client = genai.Client(vertexai=True, project=config.PROJECT_ID, location="global")
    OUT.mkdir(exist_ok=True)
    only = set(sys.argv[1:])  # regenerate just these, e.g. one that came out wrong; otherwise make only what is missing
    wanted = lambda name, ref=None: name in only or ref in only or (not only and not (OUT / f"{name}.jpg").exists())
    for name, prompt in REFERENCES.items():
        if wanted(name):
            (OUT / f"{name}.jpg").write_bytes(stamped(generate(client, prompt)))
            print(f"wrote {name}.jpg")
    for name, (ref, prompt) in FROM_REFERENCE.items():
        if wanted(name, ref):
            (OUT / f"{name}.jpg").write_bytes(stamped(generate(client, prompt, (OUT / f"{ref}.jpg").read_bytes())))
            print(f"wrote {name}.jpg")


if __name__ == "__main__":
    main()
