"""Google Maps Platform: the Geocoding API turns an address into a pin, and share links are followed to the pin inside them.

Lalamove takes each stop as coordinates plus an address line and does not geocode, so a pin is needed. Geocoding a Philippine
address often lands on the street or barangay, or in another city with a street of the same name ("12 Mabini St., Makati"
came back first as a Mabini St. in Manila), so every suggestion says how precise it is and whether its city is the one in
the address, and the seller confirms the pin on a map before quoting. Even Google's top precision (ROOFTOP) once landed on
another street in the right city ("7 Sampaguita St." came back as a building on Cresta), so the street is compared too.
"""

import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from core import config
from domain import delivery
from domain.orders import Refused

GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
PRECISION = {
    "ROOFTOP": "exact address",
    "RANGE_INTERPOLATED": "street address, estimated along the street",
    "GEOMETRIC_CENTER": "centre of a street or building area",
    "APPROXIMATE": "approximate: only the barangay or city was found",
}
PLACE_TYPES = {"locality", "administrative_area_level_2", "administrative_area_level_3", "sublocality", "sublocality_level_1"}
SHORT_HOSTS = ("maps.app.goo.gl", "goo.gl")


def places(result: dict) -> list[str]:
    """The city, municipality and barangay names in one geocoding result."""
    return [c["long_name"] for c in result.get("address_components", []) if PLACE_TYPES & set(c.get("types", []))]


GENERIC = {"street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "drive", "dr", "lane", "extension", "ext", "corner"}


def street_of(result: dict) -> str | None:
    return next((c["long_name"] for c in result.get("address_components", []) if "route" in c.get("types", [])), None)


def same_street(address: str, street: str) -> bool:
    """Whether the street Google found is the one the address names: one of its distinctive words appears in the address."""
    words = [w for w in re.findall(r"[a-z0-9]+", street.lower()) if w not in GENERIC and len(w) > 2]
    return any(re.search(rf"\b{re.escape(w)}\b", address.lower()) for w in words)


def warning(address: str, result: dict, city_matches: bool) -> str | None:
    """What the seller should check about a suggested pin, in plain words; None when it is a building on the right street."""
    kind, street = result["geometry"].get("location_type"), street_of(result)
    problems = []
    if not city_matches:
        problems.append("It is not in the city the address names.")
    if street and not same_street(address, street):
        problems.append(f"It is on {street}, not the street in the address.")
    if kind in ("APPROXIMATE", "GEOMETRIC_CENTER"):
        problems.append("Only the area was found, not the house or building.")
    elif kind == "RANGE_INTERPOLATED":
        problems.append("The house number is estimated along the street.")
    return " ".join(problems + ["Drag the pin to the exact spot, or ask the customer for their pin."]) if problems else None


def choose(address: str, results: list[dict]) -> tuple[dict, bool] | None:
    """The first result whose city or barangay is named in the address, else the first result, flagged as not matching."""
    if not results:
        return None
    text = address.lower()
    for r in results:
        if any(name.lower().removesuffix(" city") in text for name in places(r)):
            return r, True
    return results[0], False


def geocode(address: str) -> dict | None:
    """A suggested pin for an address in the Philippines, or None when Maps is not set up or finds nothing."""
    if not config.MAPS_SERVER_KEY or not (address or "").strip():
        return None
    try:
        r = requests.get(GEOCODE, params={"address": address, "components": "country:PH", "region": "ph", "key": config.MAPS_SERVER_KEY}, timeout=10)
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    picked = choose(address, data.get("results", []) if data.get("status") == "OK" else [])
    if not picked:
        return None
    result, city_matches = picked
    where = result["geometry"]["location"]
    kind = result["geometry"].get("location_type", "APPROXIMATE")
    return {
        "address": address,
        "lat": where["lat"],
        "lng": where["lng"],
        "formatted": result.get("formatted_address", ""),
        "precision": kind,
        "precision_text": PRECISION.get(kind, kind),
        "city_matches": city_matches,
        "warning": warning(address, result, city_matches),
    }


def reverse(lat: float, lng: float, address: str) -> dict | None:
    """What is at a pin, and whether it is in the order's city and on its street. Lalamove routes and prices by the pin alone
    and passes the address line to the rider unchecked (in its sandbox, a Quezon City pin quoted the same with a Makati
    address or "asdf"), so this is the only check that the rider goes where the address says."""
    if not config.MAPS_SERVER_KEY:
        return None
    try:
        r = requests.get(GEOCODE, params={"latlng": f"{lat},{lng}", "region": "ph", "key": config.MAPS_SERVER_KEY}, timeout=10)
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    results = data.get("results", []) if data.get("status") == "OK" else []
    if not results:
        return None
    here, text = results[0], (address or "").lower()
    problems = []
    if not any(name.lower().removesuffix(" city") in text for name in places(here) if name):
        problems.append("This pin is not in the city the address names.")
    street = street_of(here)
    if street and not same_street(address or "", street):
        problems.append(f"This pin is on {street}, not the street in the address.")
    return {"formatted": here.get("formatted_address", ""), "warning": " ".join(problems) or None}


def google_host(host: str) -> bool:
    host = (host or "").lower()
    return host in SHORT_HOSTS or host == "google.com" or host.endswith(".google.com") or host.endswith(".goo.gl")


def resolve(url: str) -> str:
    """Follow a short share link (maps.app.goo.gl) to the full Google Maps link, hop by hop, never leaving Google's hosts."""
    for _ in range(5):
        host = (urlparse(url).hostname or "").lower()
        if not google_host(host):
            raise Refused("Only Google Maps links can be used for a pin")
        if not host.endswith("goo.gl"):
            return url
        try:
            r = requests.get(url, allow_redirects=False, timeout=10)
        except requests.RequestException:
            raise Refused("Could not open that Google Maps link. Paste the pin instead.")
        if not r.is_redirect:
            return url
        url = urljoin(url, r.headers["location"])
    raise Refused("Could not open that Google Maps link. Paste the pin instead.")


def coordinates(text: str) -> tuple[float, float]:
    """The pin in a pasted pin or full Google Maps link. A place link's own pin (!3d<lat>!4d<lng>) wins over the map's centre."""
    place = re.search(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{2,3}\.\d+)", text or "")
    return delivery.parse_pin(f"{place[1]}, {place[2]}" if place else text)


def pin(text: str) -> tuple[float, float]:
    """A pin from what the seller or customer pasted: numbers, a Google Maps link, or a short share link. A link that names a
    place without coordinates (…/maps?q=Some+Address) is geocoded."""
    text = (text or "").strip()
    url = re.search(r"https?://\S+", text)
    if url and "goo.gl" in (urlparse(url[0]).hostname or ""):
        text = resolve(url[0])
    try:
        return coordinates(text)
    except Refused:
        query = parse_qs(urlparse(text).query)
        named = (query.get("q") or query.get("query") or [None])[0]
        found = geocode(named) if named and url else None
        if not found:
            raise
        return delivery.parse_pin(f"{found['lat']}, {found['lng']}")
