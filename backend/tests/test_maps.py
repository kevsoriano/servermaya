import pytest

from core import config
from domain.orders import Refused
from integrations import maps

MANILA = {
    "formatted_address": "12 A. Mabini St, Malate, Manila",
    "geometry": {"location": {"lat": 14.5628, "lng": 120.9880}, "location_type": "RANGE_INTERPOLATED"},
    "address_components": [{"long_name": "Manila", "types": ["locality", "political"]}],
}
MAKATI = {
    "formatted_address": "San Antonio, Makati City",
    "geometry": {"location": {"lat": 14.5639, "lng": 121.0091}, "location_type": "APPROXIMATE"},
    "address_components": [
        {"long_name": "San Antonio", "types": ["sublocality_level_1", "political"]},
        {"long_name": "Makati City", "types": ["locality", "political"]},
    ],
}


def test_the_result_in_the_addresss_own_city_wins_over_googles_first():
    # Live, Google ranked a Mabini St. in Manila first for this Makati address.
    address = "12 Mabini St., Brgy. San Antonio, Makati City"
    assert maps.choose(address, [MANILA, MAKATI]) == (MAKATI, True)
    assert maps.choose("12 Mabini St., Quezon City", [MANILA]) == (MANILA, False)  # no match: first, flagged
    assert maps.choose(address, []) is None


def test_geocode_reports_precision_and_city(monkeypatch):
    monkeypatch.setattr(config, "MAPS_SERVER_KEY", "k")
    asked = []
    reply = type("R", (), {"json": lambda self: {"status": "OK", "results": [MANILA, MAKATI]}})()
    monkeypatch.setattr(maps.requests, "get", lambda url, params, timeout: asked.append(params) or reply)
    found = maps.geocode("12 Mabini St., Brgy. San Antonio, Makati City")
    assert (found["lat"], found["precision"], found["city_matches"]) == (14.5639, "APPROXIMATE", True)
    assert found["precision_text"].startswith("approximate") and asked[0]["components"] == "country:PH"
    assert found["warning"].startswith("Only the area was found")
    monkeypatch.setattr(config, "MAPS_SERVER_KEY", "")
    assert maps.geocode("anything") is None  # Maps not set up: no suggestion, pins pasted by hand


def test_pins_from_numbers_and_every_kind_of_google_maps_link(monkeypatch):
    assert maps.pin("14.6091, 121.0223") == (14.6091, 121.0223)
    assert maps.pin("https://www.google.com/maps/@14.55,121.02,17z") == (14.55, 121.02)
    # A place link: its own pin (!3d!4d), not the map's centre after @
    place = "https://www.google.com/maps/place/Shop/@14.5500,121.0200,17z/data=!3m1!4b1!4m6!3m5!8m2!3d14.5547!4d121.0244"
    assert maps.pin(place) == (14.5547, 121.0244)

    # A short share link is followed, hop by hop, only through Google's hosts.
    hops = {"https://maps.app.goo.gl/abc": place, "https://maps.app.goo.gl/evil": "https://evil.example/@14.1,121.1"}

    def get(url, allow_redirects, timeout):
        return type("R", (), {"is_redirect": url in hops, "headers": {"location": hops.get(url)}})()

    monkeypatch.setattr(maps.requests, "get", get)
    assert maps.pin("Here po: https://maps.app.goo.gl/abc") == (14.5547, 121.0244)
    with pytest.raises(Refused):
        maps.pin("https://maps.app.goo.gl/evil")

    # A link that only names a place is geocoded.
    monkeypatch.setattr(maps, "geocode", lambda text: {"lat": 14.56, "lng": 121.0} if text == "Ayala Triangle" else None)
    assert maps.pin("https://www.google.com/maps?q=Ayala+Triangle") == (14.56, 121.0)
    with pytest.raises(Refused):
        maps.pin("somewhere near the church")


def test_a_pin_on_another_street_is_flagged_even_at_rooftop_precision():
    # Live: "7 Sampaguita St., Mandaluyong" came back ROOFTOP, on another street
    cresta = {
        "geometry": {"location": {"lat": 14.58, "lng": 121.04}, "location_type": "ROOFTOP"},
        "address_components": [{"long_name": "Cresta", "types": ["route"]}, {"long_name": "Mandaluyong City", "types": ["locality"]}],
    }
    address = "7 Sampaguita St., Brgy. Malamig, Mandaluyong City"
    assert maps.warning(address, cresta, True).startswith("It is on Cresta, not the street in the address.")
    right = {**cresta, "address_components": [{"long_name": "Sampaguita Street", "types": ["route"]}]}
    assert maps.warning(address, right, True) is None  # a building on the right street, in the right city
    assert "not in the city" in maps.warning(address, right, False)


def test_what_is_at_a_pin_is_checked_against_the_orders_address(monkeypatch):
    monkeypatch.setattr(config, "MAPS_SERVER_KEY", "k")
    at = {
        "formatted_address": "128 Kalayaan Ave, Diliman, Quezon City",
        "address_components": [{"long_name": "Kalayaan Avenue", "types": ["route"]}, {"long_name": "Quezon City", "types": ["locality"]}],
    }
    reply = type("R", (), {"json": lambda self: {"status": "OK", "results": [at]}})()
    monkeypatch.setattr(maps.requests, "get", lambda url, params, timeout: reply)
    assert maps.reverse(14.63, 121.05, "Unit 4B, 128 Kalayaan Ave, Quezon City") == {"formatted": at["formatted_address"], "warning": None}
    # Lalamove would quote this pin just the same for a Makati address; the seller is told
    moved = maps.reverse(14.63, 121.05, "12 Mabini St., Makati City")
    assert moved["warning"] == "This pin is not in the city the address names. This pin is on Kalayaan Avenue, not the street in the address."


def test_a_pin_near_a_corner_is_not_flagged_for_the_cross_street(monkeypatch):
    # Live: the estimated pin for "31 Molave St, Pasig" was nearest to a building on Quezon St
    monkeypatch.setattr(config, "MAPS_SERVER_KEY", "k")
    on = lambda street: {
        "formatted_address": f"{street}, Pasig",
        "address_components": [{"long_name": street, "types": ["route"]}, {"long_name": "Pasig", "types": ["locality"]}],
    }
    reply = lambda results: type("R", (), {"json": lambda self: {"status": "OK", "results": results}})()
    monkeypatch.setattr(maps.requests, "get", lambda url, params, timeout: reply([on("Quezon Street"), on("Molave Street")]))
    assert maps.reverse(14.57, 121.08, "31 Molave St., Brgy. Bagong Ilog, Pasig City")["warning"] is None
    monkeypatch.setattr(maps.requests, "get", lambda url, params, timeout: reply([on("Quezon Street"), on("Shaw Boulevard")]))
    assert maps.reverse(14.57, 121.08, "31 Molave St., Pasig City")["warning"] == "This pin is on Quezon Street, not the street in the address."
