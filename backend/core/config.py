import os

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "servermaya-ai-builder-cup")


# The one way in: every visitor signs in anonymously, then enters this code once. Empty means nobody gets in. Given to
# judges with the submission, never in the repo; changing it shuts out every session admitted with the old one.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")


PAGE_ID = os.environ.get("MESSENGER_PAGE_ID", "")


PAGE_TOKEN = os.environ.get("MESSENGER_PAGE_TOKEN", "")


# The Messenger webhook: Meta signs each event with the app's secret, and checks the verify token once when it is set up.
# Both empty: the webhook is off, and the inbox works by reading Messenger as before.
MESSENGER_APP_SECRET = os.environ.get("MESSENGER_APP_SECRET", "")
MESSENGER_VERIFY_TOKEN = os.environ.get("MESSENGER_VERIFY_TOKEN", "")


# "1" only once Meta has granted the app the Human Agent permission (App Review). A person may then reply up to 7 days after
# the customer's last message, tagged HUMAN_AGENT; without it, Messenger allows 24 hours. Every reply here is a seller's click.
HUMAN_AGENT = os.environ.get("MESSENGER_HUMAN_AGENT", "") == "1"


# Google Maps Platform. The server key may only call the Geocoding API (address to pin); the browser key may only load the
# Maps JavaScript API, and only on this app's addresses, so it is public. Empty: no map, and pins are pasted as before.
MAPS_SERVER_KEY = os.environ.get("MAPS_SERVER_KEY", "")
MAPS_BROWSER_KEY = os.environ.get("MAPS_BROWSER_KEY", "")
# The map style for draggable pins; Google's DEMO_MAP_ID works for development
MAPS_MAP_ID = os.environ.get("MAPS_MAP_ID", "DEMO_MAP_ID")


GRAPH = "https://graph.facebook.com"


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


# The demo's AI customer only writes chat lines in character (code decides taps and receipts), so it runs on the Lite model.
GEMINI_DEMO_MODEL = os.environ.get("GEMINI_DEMO_MODEL", "gemini-3.5-flash-lite")


# Memory Bank for past claim decisions: projects/.../locations/global/reasoningEngines/... from `make memory-bank`. Empty turns it off.
MEMORY_BANK = os.environ.get("MEMORY_BANK", "")


# Model Armor screens customer messages and GCash receipt screenshots: projects/.../locations/us/templates/... Empty turns it off.
MODEL_ARMOR_TEMPLATE = os.environ.get("MODEL_ARMOR_TEMPLATE", "")


# ponytail: one store, add real store ids when a second seller onboards
STORE = "main"
# The app's own pages, the only origins allowed to call the API from another origin: the deployed frontend sends AI actions
# straight to Cloud Run, past Firebase Hosting's 60-second limit on the requests it forwards.
APP_ORIGINS = [
    f"https://{PROJECT_ID}.web.app",
    f"https://{PROJECT_ID}.firebaseapp.com",
    "http://localhost:5173",
]


PAYMONGO_KEY = os.environ.get("PAYMONGO_SECRET_KEY", "")


PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET", "")


LALAMOVE_KEY = os.environ.get("LALAMOVE_API_KEY", "")


LALAMOVE_SECRET = os.environ.get("LALAMOVE_API_SECRET", "")


LALAMOVE_URL = os.environ.get("LALAMOVE_URL", "https://rest.sandbox.lalamove.com")


EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "servermaya-ai-builder-cup-evidence")


PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://servermaya-ai-builder-cup.web.app")


# A Lalamove sandbox booking moves on by itself, one simulated step a second, so the demo never waits on a driver the sandbox
# will not assign. Set to 0 when real sellers use the app; it never applies to live bookings either way.
SANDBOX_AUTO_DELIVERY = os.environ.get("SANDBOX_AUTO_DELIVERY", "1") == "1"


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
