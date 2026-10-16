import os

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")


# Comma-separated. Empty means nobody gets in.
ALLOWLIST = {e.strip().lower() for e in os.environ.get("ALLOWLIST_EMAIL", "").split(",") if e.strip()}


PAGE_ID = os.environ.get("MESSENGER_PAGE_ID", "")


PAGE_TOKEN = os.environ.get("MESSENGER_PAGE_TOKEN", "")


GRAPH = "https://graph.facebook.com"


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


# ponytail: one store, add real store ids when a second seller onboards
STORE = "main"


PAYMONGO_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")


PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")


LALAMOVE_KEY = os.environ.get("LALAMOVE_API_KEY", "")


LALAMOVE_SECRET = os.environ.get("LALAMOVE_API_SECRET", "")


LALAMOVE_URL = os.environ.get("LALAMOVE_URL", "https://rest.sandbox.lalamove.com")


EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "servermaya-ai-builder-cup-evidence")


PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://servermaya-ai-builder-cup.web.app")


# Fictional, so a demo visitor never sees the seller's real GCash account or pickup address. Demo orders always use these.
DEMO_GCASH = {"name": "Juan Dela Cruz", "mobile": "639171234567", "qr": None, "demo": True}


DEMO_PICKUP = {
    "address": "6750 Ayala Ave., Makati City",
    "lat": 14.5547,
    "lng": 121.0244,
    "name": "Demo shop",
    "phone": "+639171234567",
    "demo": True,
}
