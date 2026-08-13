"""City presets + place discovery backends (OSM free, Google Places optional)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from config import settings

log = logging.getLogger("ig.places")

# Lagos-first. Expand as needed.
CITY_PRESETS: dict[str, dict[str, Any]] = {
    "lagos": {
        "name": "Lagos",
        "country": "Nigeria",
        "query_suffix": "Lagos Nigeria",
        # south, west, north, east
        "bbox": (6.35, 3.05, 6.72, 3.70),
        "lat": 6.5244,
        "lng": 3.3792,
    },
    "abuja": {
        "name": "Abuja",
        "country": "Nigeria",
        "query_suffix": "Abuja Nigeria",
        "bbox": (8.90, 7.30, 9.20, 7.60),
        "lat": 9.0765,
        "lng": 7.3986,
    },
}

_IG_URL = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?",
    re.I,
)


def extract_instagram_handle(text: str | None) -> str | None:
    if not text:
        return None
    m = _IG_URL.search(text)
    if not m:
        return None
    handle = m.group(1).lower().rstrip(".")
    if handle in {"p", "reel", "reels", "stories", "explore", "accounts"}:
        return None
    return handle


def place_search_query(place: dict[str, Any]) -> str:
    """Query string for Instagram topsearch."""
    name = (place.get("name") or "").strip()
    city = (place.get("city") or "").strip()
    if city and city.lower() not in name.lower():
        return f"{name} {city}".strip()
    return name


_OVERPASS_FALLBACKS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def search_osm(city_key: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """
    Free venue discovery via OpenStreetMap Overpass.

    No Instagram links usually — resolve handles separately with logged-in search.
    """
    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}; known: {sorted(CITY_PRESETS)}")

    s, w, n, e = preset["bbox"]
    query = f"""
    [out:json][timeout:90];
    (
      node["amenity"~"restaurant|bar|cafe|fast_food|nightclub|pub|biergarten"]({s},{w},{n},{e});
      way["amenity"~"restaurant|bar|cafe|fast_food|nightclub|pub|biergarten"]({s},{w},{n},{e});
      node["tourism"~"hotel|hostel"]({s},{w},{n},{e});
      way["tourism"~"hotel|hostel"]({s},{w},{n},{e});
    );
    out center tags;
    """
    headers = {
        "Accept": "*/*",
        "User-Agent": "validds-instagram-ingest/1.0 (venue discovery)",
    }
    urls = []
    primary = settings.OVERPASS_URL
    if primary:
        urls.append(primary)
    for u in _OVERPASS_FALLBACKS:
        if u not in urls:
            urls.append(u)

    payload: dict[str, Any] | None = None
    last_err: Exception | None = None
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for url in urls:
            try:
                resp = client.post(url, data={"data": query})
                if resp.status_code >= 400:
                    log.warning("[osm] %s → HTTP %s", url, resp.status_code)
                    continue
                payload = resp.json()
                log.info("[osm] using %s", url)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.warning("[osm] %s failed: %s", url, exc)
    if payload is None:
        raise RuntimeError(f"all Overpass endpoints failed: {last_err}")

    places: list[dict[str, Any]] = []
    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        ig = (
            extract_instagram_handle(tags.get("contact:instagram"))
            or extract_instagram_handle(website)
        )
        osm_id = f"{el.get('type')}/{el.get('id')}"
        places.append({
            "_id": f"osm:{osm_id}",
            "source": "osm",
            "sourceId": osm_id,
            "name": name,
            "city": preset["name"],
            "country": preset["country"],
            "amenity": tags.get("amenity") or tags.get("tourism"),
            "website": website,
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "lat": lat,
            "lng": lng,
            "instagramHint": ig,
            "rawTags": tags,
        })
        if len(places) >= limit:
            break

    log.info("[osm] %s → %d places", city_key, len(places))
    return places


# Structured venue attributes. These live in Google's pricier
# "Enterprise + Atmosphere" SKU, so they are opt-in per call via `rich`.
_ATMOSPHERE_MASK = (
    "places.servesBreakfast,places.servesBrunch,places.servesLunch,"
    "places.servesDinner,places.servesVegetarianFood,places.servesCocktails,"
    "places.servesCoffee,places.servesBeer,places.servesWine,places.servesDessert,"
    "places.dineIn,places.takeout,places.delivery,places.reservable,"
    "places.outdoorSeating,places.restroom,places.goodForGroups,"
    "places.goodForChildren,places.liveMusic,places.accessibilityOptions,"
    "places.parkingOptions,places.priceRange,places.reviews,"
)

# Google primaryTypes that are not restaurants. Measured over 250 Lagos venues:
# night_club 0/38 and bar 0/26 carry meal attributes, because they do not serve
# meals. Importing them as Restaurants is what made `meal` look 43% missing.
_NON_RESTAURANT_TYPES = {
    "night_club", "bar", "lounge_bar", "pub", "liquor_store",
    "meal_delivery", "event_venue", "hotel", "lodging",
    "association_or_organization", "store", "supermarket",
}

_ATTR_KEYS = (
    "servesBreakfast", "servesBrunch", "servesLunch", "servesDinner",
    "servesVegetarianFood", "servesCocktails", "servesCoffee", "servesBeer",
    "servesWine", "servesDessert", "dineIn", "takeout", "delivery",
    "reservable", "outdoorSeating", "restroom", "goodForGroups",
    "goodForChildren", "liveMusic",
)


def geocode_address(address: str, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """
    Address string → {lat, lng, precision} via the Geocoding API.

    `precision` is Google's `location_type`: ROOFTOP is an exact building,
    APPROXIMATE is usually a neighbourhood centroid ("Lekki Phase 1") and
    should not be treated as the venue's real position.
    """
    key = settings.GOOGLE_PLACES_API_KEY
    if not key or not (address or "").strip():
        return None
    own = client is None
    if own:
        client = httpx.Client(timeout=20.0)
    assert client is not None
    try:
        resp = client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": key, "region": "ng"},
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        geometry = data["results"][0].get("geometry") or {}
        loc = geometry.get("location") or {}
        if loc.get("lat") is None or loc.get("lng") is None:
            return None
        return {
            "lat": float(loc["lat"]),
            "lng": float(loc["lng"]),
            "precision": geometry.get("location_type"),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("[geocode] %s failed: %s", address[:40], exc)
        return None
    finally:
        if own:
            client.close()


_IG_LINK = re.compile(r"instagram\.com/([A-Za-z0-9._]{2,30})", re.I)
# instagram.com/<these> are routes, not accounts.
_IG_RESERVED = {
    "p", "reel", "reels", "explore", "accounts", "tv", "stories", "share",
    "direct", "about", "developer", "legal", "privacy", "terms", "help",
    # Surfaced by search-result markup rather than by venue pages.
    "popular", "locations", "web", "login", "signup", "lite", "channel",
}
# Site templates ship with an unedited social link; one Lagos venue publishes
# instagram.com/yourpage. These are never real accounts.
_IG_PLACEHOLDER = {
    "yourpage", "yourusername", "yourhandle", "youraccount", "yourprofile",
    "yourbrand", "yourcompany", "username", "profile", "page", "home", "user",
    "account", "instagram", "brandname", "handle", "example", "site",
}


def _handle_relates(handle: str, *, name: str = "", url: str = "") -> bool:
    """
    Does this handle plausibly belong to this venue?

    A real handle almost always shares a run of characters with the venue name
    or its domain (thehouseng.com → thehouselagos). A template placeholder
    shares nothing with either, which is what makes it detectable.
    """
    # City and format words are shared by most Lagos venues, so matching on
    # them is no evidence at all — 'hrclagos' would otherwise validate against
    # any venue named '<something> Lagos'.
    generic = (
        "lagos", "nigeria", "abuja", "restaurant", "lounge", "bar", "cafe",
        "kitchen", "grill", "the", "eatery", "bistro", "official", "ng",
    )
    def _strip(text: str) -> str:
        text = text.lower()
        for word in generic:
            text = text.replace(word, " ")
        return re.sub(r"[^a-z0-9]", "", text)

    target = _strip(handle)
    if not target:
        return False
    haystack = _strip(f"{name} {urlparse(url).netloc}")
    if not haystack:
        # Nothing distinctive to compare against — don't reject on no evidence.
        return True
    for size in range(len(target), 3, -1):
        for start in range(0, len(target) - size + 1):
            if target[start:start + size] in haystack:
                return True
    return False


def handle_from_website(
    url: str, *, name: str = "", client: httpx.Client | None = None
) -> str | None:
    """
    Fetch a venue site / Linktree and pull the Instagram handle it links to.

    Fallback for when the logged-in topsearch path is unavailable. Many venues
    are behind bot filters, so a miss here is normal, not an error. Passing
    ``name`` lets template placeholders be rejected.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    # Directories store bare hosts ('thesmiths.ng') as often as full URLs.
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"

    # Stored deep links rot ('/about' 404s) while the root still serves the
    # footer social icons, so try the origin as a second attempt.
    parsed = urlparse(raw)
    candidates = [raw]
    root = f"{parsed.scheme}://{parsed.netloc}/"
    if parsed.path.strip("/") and root != raw:
        candidates.append(root)

    own = client is None
    if own:
        client = httpx.Client(timeout=20.0, follow_redirects=True)
    assert client is not None
    headers = {
        # Bare clients get 406'd by several Lagos venue sites.
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        for candidate in candidates:
            try:
                resp = client.get(candidate, headers=headers)
            except Exception as exc:  # noqa: BLE001
                log.debug("[ig-from-site] %s failed: %s", candidate[:50], exc)
                continue
            if resp.status_code >= 400:
                continue
            for hit in _IG_LINK.findall(resp.text or ""):
                handle = hit.lower()
                if handle in _IG_RESERVED or handle in _IG_PLACEHOLDER:
                    continue
                if not _handle_relates(handle, name=name, url=candidate):
                    log.debug(
                        "[ig-from-site] rejected %r for %r (unrelated)", handle, name
                    )
                    continue
                return handle
        return None
    finally:
        if own:
            client.close()


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Two shapes in the wild: wa.me/<number> and wa.me/message/<short code>.
# The short code is a valid contact link even though it carries no digits.
_WA_HREF = re.compile(r"(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\+?\d[\d\s%-]{6,})", re.I)
_WA_SHORT = re.compile(r"https?://wa\.me/message/([A-Z0-9]{6,})", re.I)
# Directory placeholders, template dummies, and the addresses of the sites that
# merely write *about* venues. Harvesting any of these yields a wrong contact.
_EMAIL_JUNK = re.compile(
    r"(example|domain\.com|yourname|your-?email|sentry|wixpress|godaddy|"
    r"unclaimed|noreply|no-reply|dinesurf|eatdrinklagos|tripadvisor|"
    r"squarespace|wordpress|shopify|sentry\.io)",
    re.I,
)
_CONTACT_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/reservations")


def harvest_contacts(
    website: str,
    *,
    client: httpx.Client | None = None,
    max_pages: int = 3,
) -> dict[str, Any]:
    """
    Emails and WhatsApp numbers from the venue's OWN site.

    Deliberately domain-scoped. Searching the open web for "<venue> email"
    returns a nearby hotel's address, the directory's `unclaimed@` placeholder,
    or the review blog's editor — measured, not hypothetical. An address on the
    venue's own domain is the only one that can be trusted without a human.
    """
    raw = (website or "").strip()
    if not raw:
        return {}
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    host = urlparse(raw).netloc.lower()
    # Social profiles carry no contact details worth trusting.
    if any(s in host for s in ("instagram.", "facebook.", "twitter.", "x.com")):
        return {}
    # Link aggregators are not the venue's domain, so no email is attributable,
    # but they are where Lagos venues put their WhatsApp button.
    aggregator = any(s in host for s in ("linktr.ee", "linktree.com", "bento.me", "beacons.ai"))

    own = client is None
    if own:
        client = httpx.Client(timeout=20.0, follow_redirects=True)
    assert client is not None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    emails: list[str] = []
    whatsapp: str | None = None
    root = f"{urlparse(raw).scheme}://{urlparse(raw).netloc}"
    try:
        paths = [""] if aggregator else list(_CONTACT_PATHS[:max_pages + 3])
        for path in paths:
            if len(emails) >= 2 and whatsapp:
                break
            target = f"{root}{path}" if path else raw
            body = ""
            try:
                resp = client.get(target, headers=headers)
                if resp.status_code < 400:
                    body = resp.text or ""
                elif resp.status_code in (403, 429):
                    # Linktree fingerprints plain clients; render it instead.
                    from pipeline.web_menu import _fetch_via_browser

                    rendered = _fetch_via_browser(target)
                    body = rendered.decode("utf-8", "replace") if rendered else ""
            except Exception:  # noqa: BLE001
                continue
            if not body:
                continue
            for hit in ([] if aggregator else _EMAIL.findall(body)):
                low = hit.lower()
                if _EMAIL_JUNK.search(low) or low in emails:
                    continue
                # Prefer addresses on the venue's own domain; a gmail on the
                # venue's contact page is still theirs, an unrelated corporate
                # domain usually is not.
                domain = low.split("@")[-1]
                bare = host.replace("www.", "")
                if domain == bare or domain.endswith(bare) or domain in (
                    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                ):
                    emails.append(low)
            if not whatsapp:
                m = _WA_HREF.search(body)
                if m:
                    digits = re.sub(r"[^\d+]", "", unquote(m.group(1)))
                    if len(digits) >= 10:
                        whatsapp = f"+234{digits[1:]}" if digits.startswith("0") else digits
                else:
                    short = _WA_SHORT.search(body)
                    if short:
                        whatsapp = f"https://wa.me/message/{short.group(1)}"
    finally:
        if own:
            client.close()

    out: dict[str, Any] = {}
    if emails:
        out["emails"] = emails[:2]
    if whatsapp:
        out["whatsApp"] = whatsapp
    return out


def venue_interior_images(
    name: str,
    *,
    city: str = "Lagos",
    limit: int = 8,
    client: httpx.Client | None = None,
) -> list[str]:
    """
    Image-search results showing the venue's *room*.

    A venue's own Instagram grid is mostly plated food, flyers and portraits,
    so judging lighting or seating from it fails on input quality rather than
    on the model. An interior-targeted image search returns review-site and
    directory photographs of the space itself.
    """
    if not settings.SERPER_API_KEY or not (name or "").strip():
        return []
    own = client is None
    if own:
        client = httpx.Client(timeout=30.0)
    assert client is not None
    urls: list[str] = []
    try:
        resp = client.post(
            settings.SERPER_IMAGE_ENDPOINT,
            headers={
                "X-API-KEY": settings.SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "q": f"{name} {city} restaurant interior seating",
                "num": limit * 2,
                "gl": settings.SERPER_COUNTRY,
            },
        )
        if resp.status_code >= 400:
            log.warning("[venue-images] serper HTTP %s", resp.status_code)
            return []
        for item in (resp.json().get("images") or []):
            url = item.get("imageUrl") or ""
            # Instagram's SEO crawler endpoint serves a redirect, not an image,
            # and TikTok's API images need headers we do not send.
            if not url or "lookaside.instagram.com" in url or "tiktok.com/api" in url:
                continue
            urls.append(url)
            if len(urls) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("[venue-images] %s failed: %s", name[:30], exc)
    finally:
        if own:
            client.close()
    return urls


def handle_from_search(
    name: str,
    *,
    city: str = "Lagos",
    client: httpx.Client | None = None,
    retries: int = 1,
) -> str | None:
    """
    Find a venue's Instagram handle via a web search instead of Instagram.

    Instagram's own topsearch needs a logged-in session that gets checkpointed;
    a `site:instagram.com` query answers the same question without touching
    Instagram at all. DuckDuckGo's HTML endpoint needs no API key. It answers
    202 with an empty body when it wants you to slow down — that is a soft
    failure, not a miss.
    """
    if not settings.SERPER_API_KEY:
        log.debug("[ig-search] SERPER_API_KEY not set")
        return None

    query = f"site:instagram.com {name} {city}".strip()
    own = client is None
    if own:
        client = httpx.Client(timeout=25.0)
    assert client is not None
    try:
        for attempt in range(retries + 1):
            try:
                resp = client.post(
                    settings.SERPER_ENDPOINT,
                    headers={
                        "X-API-KEY": settings.SERPER_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": 10, "gl": settings.SERPER_COUNTRY},
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("[ig-search] %s failed: %s", name[:30], exc)
                return None
            if resp.status_code == 429:
                if attempt < retries:
                    time.sleep(2.0)
                    continue
                log.warning("[ig-search] serper rate limited")
                return None
            if resp.status_code >= 400:
                log.warning(
                    "[ig-search] serper HTTP %s: %s",
                    resp.status_code, (resp.text or "")[:120],
                )
                return None
            payload = resp.json()
            # Profile URLs only — /p/ and /reel/ links are posts that merely
            # mention the venue, and their handle segment is the post id.
            for item in payload.get("organic") or []:
                link = item.get("link") or ""
                for hit in _IG_LINK.findall(link):
                    handle = unquote(hit).lower().strip("/")
                    if handle in _IG_RESERVED or handle in _IG_PLACEHOLDER:
                        continue
                    if not _handle_relates(handle, name=name):
                        continue
                    return handle
            return None
        return None
    finally:
        if own:
            client.close()


def _resolve_google_photos(
    client: httpx.Client,
    key: str,
    photos: list[dict[str, Any]],
    *,
    limit: int = 3,
) -> list[str]:
    """
    Photo resources → real CDN URLs.

    searchText returns only `places/<id>/photos/<ref>`; the browsable URL comes
    from the media endpoint. `skipHttpRedirect` makes it answer JSON instead of
    302-ing to the image, so one cheap call per photo. Billed per request, so
    `limit` caps how many we resolve per venue.
    """
    urls: list[str] = []
    for photo in photos[:limit]:
        resource = photo.get("name")
        if not resource:
            continue
        try:
            resp = client.get(
                f"https://places.googleapis.com/v1/{resource}/media",
                params={
                    "maxHeightPx": 800,
                    "maxWidthPx": 1600,
                    "skipHttpRedirect": "true",
                },
                headers={"X-Goog-Api-Key": key},
            )
            if resp.status_code >= 400:
                log.debug("[google] photo media HTTP %s", resp.status_code)
                continue
            uri = (resp.json() or {}).get("photoUri")
            if uri:
                urls.append(uri)
        except Exception as exc:  # noqa: BLE001
            log.debug("[google] photo resolve failed: %s", exc)
    return urls


def search_google_places(
    city_key: str, *, limit: int = 60, photo_limit: int = 3, rich: bool = True
) -> list[dict[str, Any]]:
    """
    Google Places API (New) text search. Requires GOOGLE_PLACES_API_KEY.

    ``photo_limit`` photos are resolved per venue (0 disables — each resolve is
    a billed Places request). ``rich`` adds the structured atmosphere fields
    (meal service, seating, price range, reviews) on the pricier SKU.
    """
    key = settings.GOOGLE_PLACES_API_KEY
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set")

    preset = CITY_PRESETS.get(city_key.lower())
    if not preset:
        raise ValueError(f"unknown city {city_key!r}")

    # Food-serving venues only. A nightclub is not a restaurant — Google
    # correctly returns no `serves*` attributes for one, and the existing
    # exploree `restaurants` rows are all kitchens (incl. hybrid
    # "Restaurant & Lounge"), never pure clubs.
    types = [
        "restaurant",
        "cafe",
        "bakery",
        "meal_takeaway",
    ]
    places: list[dict[str, Any]] = []
    seen: set[str] = set()

    with httpx.Client(timeout=45.0) as client:
        for place_type in types:
            if len(places) >= limit:
                break
            body = {
                "textQuery": f"{place_type.replace('_', ' ')} in {preset['query_suffix']}",
                "pageSize": min(20, limit - len(places)),
                "locationBias": {
                    "circle": {
                        "center": {
                            "latitude": preset["lat"],
                            "longitude": preset["lng"],
                        },
                        "radius": 25000.0,
                    }
                },
            }
            page_token = None
            pages = 0
            while len(places) < limit and pages < 3:
                if page_token:
                    body["pageToken"] = page_token
                resp = client.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers={
                        "Content-Type": "application/json",
                        "X-Goog-Api-Key": key,
                        "X-Goog-FieldMask": (
                            "places.id,places.displayName,places.formattedAddress,"
                            "places.websiteUri,places.nationalPhoneNumber,"
                            "places.internationalPhoneNumber,places.location,"
                            "places.types,places.googleMapsUri,places.primaryType,"
                            "places.regularOpeningHours,places.editorialSummary,"
                            "places.rating,places.userRatingCount,places.priceLevel,"
                            "places.photos,"
                            + (_ATMOSPHERE_MASK if rich else "")
                            + "nextPageToken"
                        ),
                    },
                    json=body,
                )
                if resp.status_code >= 400:
                    log.warning(
                        "[google] places HTTP %s: %s",
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
                    break
                data = resp.json()
                for p in data.get("places") or []:
                    pid = p.get("id") or ""
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    name = ((p.get("displayName") or {}).get("text") or "").strip()
                    if not name:
                        continue
                    # A text query for "restaurant in Lagos" still returns
                    # clubs and delivery-only kitchens; drop them by primary
                    # type rather than importing venues that cannot serve a meal.
                    if (p.get("primaryType") or "") in _NON_RESTAURANT_TYPES:
                        continue
                    website = p.get("websiteUri")
                    loc = p.get("location") or {}
                    hours = None
                    roh = p.get("regularOpeningHours") or {}
                    if isinstance(roh, dict) and roh.get("weekdayDescriptions"):
                        hours = "; ".join(roh["weekdayDescriptions"][:7])
                    summary = ((p.get("editorialSummary") or {}).get("text") or "").strip()
                    places.append({
                        "_id": f"google:{pid}",
                        "source": "google",
                        "sourceId": pid,
                        "name": name,
                        "city": preset["name"],
                        "country": preset["country"],
                        "amenity": p.get("primaryType") or place_type,
                        "address": p.get("formattedAddress"),
                        "website": website,
                        "phone": p.get("nationalPhoneNumber")
                        or p.get("internationalPhoneNumber"),
                        "email": None,
                        "hours": hours,
                        "description": summary or None,
                        "lat": loc.get("latitude"),
                        "lng": loc.get("longitude"),
                        "instagramHint": extract_instagram_handle(website),
                        "mapsUri": p.get("googleMapsUri"),
                        "types": p.get("types") or [],
                        "rating": p.get("rating"),
                        "ratingCount": p.get("userRatingCount"),
                        "priceLevel": p.get("priceLevel"),
                        "priceRange": p.get("priceRange"),
                        "accessibility": p.get("accessibilityOptions") or {},
                        "parking": p.get("parkingOptions") or {},
                        "attrs": {
                            k: p[k] for k in _ATTR_KEYS if p.get(k) is not None
                        },
                        "reviews": [
                            ((r.get("text") or {}).get("text") or "").strip()
                            for r in (p.get("reviews") or [])
                            if ((r.get("text") or {}).get("text") or "").strip()
                        ][:5],
                        "photos": _resolve_google_photos(
                            client, key, p.get("photos") or [], limit=photo_limit
                        ),
                    })
                    if len(places) >= limit:
                        break
                page_token = data.get("nextPageToken")
                pages += 1
                if not page_token:
                    break

    log.info("[google] %s → %d places", city_key, len(places))
    return places


def discover_places(
    city_key: str,
    *,
    limit: int = 200,
    backend: str | None = None,
) -> list[dict[str, Any]]:
    """
    backend: auto | osm | google | flavorqueste | reisty | enrich

    ``auto`` = google if API key set, else osm (legacy).
    ``enrich`` = FlavorQueste + Reisty (+ google/osm) for denser Lagos coverage.
    """
    mode = (backend or settings.PLACES_BACKEND or "auto").lower()
    if mode == "auto":
        mode = "google" if settings.GOOGLE_PLACES_API_KEY else "osm"

    if mode == "google":
        return search_google_places(city_key, limit=limit)
    if mode == "osm":
        return search_osm(city_key, limit=limit)
    if mode == "flavorqueste":
        from discover.flavorqueste import search_flavorqueste

        return search_flavorqueste(city_key, limit=limit)
    if mode == "reisty":
        from discover.reisty import search_reisty

        return search_reisty(city_key, limit=limit)
    if mode == "enrich":
        return _discover_enriched(city_key, limit=limit)
    raise ValueError(f"unknown places backend {mode!r}")


def _discover_enriched(city_key: str, *, limit: int) -> list[dict[str, Any]]:
    """Merge Lagos directories. Separate source ids — no cross-source overwrite."""
    from discover.flavorqueste import search_flavorqueste
    from discover.reisty import search_reisty

    chunks: list[dict[str, Any]] = []
    # FlavorQueste is the densest public catalog (~600).
    try:
        chunks.extend(search_flavorqueste(city_key, limit=min(limit, 400)))
    except Exception as exc:  # noqa: BLE001
        log.warning("[enrich] flavorqueste failed: %s", exc)
    try:
        chunks.extend(search_reisty(city_key, limit=min(limit, 120), detail=True))
    except Exception as exc:  # noqa: BLE001
        log.warning("[enrich] reisty failed: %s", exc)

    # Keep Maps/OSM as a supplement for venues missing from local apps.
    remaining = max(0, limit - len(chunks))
    if remaining > 0:
        try:
            if settings.GOOGLE_PLACES_API_KEY:
                chunks.extend(search_google_places(city_key, limit=min(60, remaining)))
            else:
                chunks.extend(search_osm(city_key, limit=min(80, remaining)))
        except Exception as exc:  # noqa: BLE001
            log.warning("[enrich] maps/osm supplement failed: %s", exc)

    # Prefer entries that already carry an IG hint / email when names collide
    # across sources — still keep distinct _ids in Mongo via upsert.
    log.info("[enrich] %s → %d places (pre-cap)", city_key, len(chunks))
    return chunks[:limit]