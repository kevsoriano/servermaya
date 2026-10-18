"""Model Armor: a second screen for what customers send, beside the prompts' own defences (extraction.inline and the
system prompts). It looks for prompt injection or jailbreak text and for links known to be unsafe.

It only flags. A finding becomes a check for the seller, never a block: no Gemini call waits for it, and nothing is refused
because of it. When it is off (MODEL_ARMOR_TEMPLATE empty) or does not answer, the answer is None: not screened, which is
never shown as a pass.

The template is in the "us" multi-region, because image screening (in Preview) runs only in us and eu. Tagalog is not one
of Model Armor's tested languages, and text under three words is never flagged, so it misses some things the prompts catch.
"""

import base64
import logging
from functools import lru_cache

import google.auth
from google.auth.transport.requests import AuthorizedSession

from core import config

log = logging.getLogger(__name__)

# Filter names as Model Armor reports them -> what this app calls a finding
FINDINGS = {"pi_and_jailbreak": "instructions", "malicious_uris": "unsafe_link"}
IMAGE_TYPES = {"image/jpeg", "image/png"}  # also BMP; up to 4 MB each
MAX_IMAGE_BYTES = 4 * 1024 * 1024


@lru_cache
def session() -> AuthorizedSession:
    return AuthorizedSession(google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])[0])


def findings(result: dict) -> list[str] | None:
    """What matched, from a sanitizeUserPrompt response's filterResults. None if a filter we use did not run."""
    out = []
    for name, wrapped in result.get("filterResults", {}).items():
        if name not in FINDINGS:
            continue
        inner = next(iter(wrapped.values()), {})  # e.g. {"piAndJailbreakFilterResult": {"executionState": ..., "matchState": ...}}
        if inner.get("executionState") != "EXECUTION_SUCCESS":
            return None
        if inner.get("matchState") == "MATCH_FOUND":
            out.append(FINDINGS[name])
    return out


def screen(text: str | None = None, image: tuple[bytes, str] | None = None) -> list[str] | None:
    """Screen one customer message's text, or one image (data, content type). Returns the findings, [] for none, or None
    when not screened: off, an unsupported image, or any failure."""
    if not config.MODEL_ARMOR_TEMPLATE:
        return None
    if image:
        data, content_type = image
        if content_type not in IMAGE_TYPES or len(data) > MAX_IMAGE_BYTES:
            return None
        item = {"byteItem": {"byteDataType": "IMAGE", "byteData": base64.b64encode(data).decode()}}
    elif text and text.strip():
        item = {"text": text}
    else:
        return None
    location = config.MODEL_ARMOR_TEMPLATE.split("/locations/")[1].split("/")[0]
    try:
        r = session().post(
            f"https://modelarmor.{location}.rep.googleapis.com/v1/{config.MODEL_ARMOR_TEMPLATE}:sanitizeUserPrompt",
            json={"userPromptData": item, "multiLanguageDetectionMetadata": {"enableMultiLanguageDetection": True}},
            timeout=10,
        )
        r.raise_for_status()
        return findings(r.json()["sanitizationResult"])
    except Exception as e:
        log.warning("Model Armor screen failed: %r", e)
        return None
