"""
Runtime settings — all overridable via environment variables.

Mirrors the conventions in validds/scraper/config/settings.py so this module
can be dropped into that tree with no changes.

Default mode is Playwright (logged-out scrape). Graph API is optional and can
be added later — when credentials are set it becomes the primary path and
Playwright stays the fallback for non-business / undiscoverable handles.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool) -> bool:
    v = os.getenv(key, "").lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return default


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGODB_URI: str = _str("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME: str = _str("MONGODB_DB_NAME", "validds")

# Collection names — raw-first, no event parsing yet.
COL_ACCOUNTS: str = _str("IG_COL_ACCOUNTS", "ig_accounts")
COL_POSTS: str = _str("IG_COL_POSTS", "ig_posts_raw")
COL_HIGHLIGHTS: str = _str("IG_COL_HIGHLIGHTS", "ig_highlights_raw")
COL_RUNS: str = _str("IG_COL_RUNS", "ig_runs")

# ── Source 1: Instagram Graph API (optional — add later) ──────────────────────
# When empty, every due account goes straight to Playwright.
# Long-lived Page access token with instagram_basic + pages_read_engagement.
IG_GRAPH_ACCESS_TOKEN: str = _str("IG_GRAPH_ACCESS_TOKEN", "")
# Numeric id of the IG Business account YOU own — the query is made "as" this one.
IG_GRAPH_USER_ID: str = _str("IG_GRAPH_USER_ID", "")
IG_GRAPH_VERSION: str = _str("IG_GRAPH_VERSION", "v21.0")
IG_GRAPH_BASE_URL: str = _str("IG_GRAPH_BASE_URL", "https://graph.facebook.com")
# Meta's documented ceiling is 200 calls/hour/user. Stay under it deliberately.
IG_GRAPH_CALLS_PER_HOUR: int = _int("IG_GRAPH_CALLS_PER_HOUR", 180)
# Posts requested per business_discovery call (Graph caps around 25 usefully).
IG_GRAPH_MEDIA_LIMIT: int = _int("IG_GRAPH_MEDIA_LIMIT", 25)

# ── Source 2: Playwright (primary until Graph credentials are set) ────────────
IG_FALLBACK_ENABLED: bool = _bool("IG_FALLBACK_ENABLED", True)
# Only fall back for accounts Graph couldn't serve (private / personal / missing).
# When Graph is unset, every account uses this path.
IG_FALLBACK_AFTER_FAILURES: int = _int("IG_FALLBACK_AFTER_FAILURES", 2)
# The single most important number here. Above ~120/hr from one IP you WILL
# start collecting interstitials. Keep this low and let it drain over days.
IG_FALLBACK_MAX_PER_RUN: int = _int("IG_FALLBACK_MAX_PER_RUN", 40)
IG_FALLBACK_MIN_GAP_S: int = _int("IG_FALLBACK_MIN_GAP_S", 20)
IG_FALLBACK_MAX_GAP_S: int = _int("IG_FALLBACK_MAX_GAP_S", 55)
# Optional: any proxy URL you already have. Empty = your own IP.
IG_PROXY_URL: str = _str("IG_PROXY_URL", "")
SCRAPER_HEADLESS: bool = _bool("SCRAPER_HEADLESS", True)
IG_HIGHLIGHTS_ENABLED: bool = _bool("IG_HIGHLIGHTS_ENABLED", True)

# ── Concurrency / pacing ──────────────────────────────────────────────────────
IG_CONCURRENCY: int = _int("IG_CONCURRENCY", 4)
IG_MIN_DELAY_MS: int = _int("IG_MIN_DELAY_MS", 200)
IG_MAX_DELAY_MS: int = _int("IG_MAX_DELAY_MS", 700)
IG_CIRCUIT_THRESHOLD: int = _int("IG_CIRCUIT_THRESHOLD", 12)

# ── Tiered refresh cadence (hours between fetches) ────────────────────────────
# Assigned from observed posting frequency — see pipeline/tiers.py.
TIER_INTERVALS_HOURS: dict[str, int] = {
    "hot": _int("IG_TIER_HOT_HOURS", 12),  # posts most days
    "warm": _int("IG_TIER_WARM_HOURS", 24),  # posts weekly
    "cold": _int("IG_TIER_COLD_HOURS", 96),  # posts monthly
    "dormant": _int("IG_TIER_DORMANT_HOURS", 336),  # nothing in 90d
}

# ── Ops ───────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = _str("LOG_LEVEL", "INFO").upper()

# ── DeepSeek experience extraction (optional; OpenAI-compatible) ──────────────
# When API key is set, refining drafts with DeepSeek is on by default.
DEEPSEEK_API_KEY: str = _str("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = _str("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL: str = _str("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_ENABLED: bool = _bool("DEEPSEEK_ENABLED", bool(DEEPSEEK_API_KEY))
# Skip thinking mode for faster/cheaper structured JSON extraction.
DEEPSEEK_THINKING: bool = _bool("DEEPSEEK_THINKING", False)
DEEPSEEK_TIMEOUT_S: int = _int("DEEPSEEK_TIMEOUT_S", 45)
# Cap live LLM calls per /api/events pass (rest use cache or heuristics).
DEEPSEEK_MAX_PER_REQUEST: int = _int("DEEPSEEK_MAX_PER_REQUEST", 25)

# ── Logged-in Instagram session (handle discovery ONLY) ───────────────────────
# Paste fresh browser cookies into .env. Never commit. Rotate if leaked.
# Prefer IG_COOKIES = the full Cookie header from DevTools (most reliable).
IG_COOKIES: str = _str("IG_COOKIES", "")
# Netscape cookie file path (e.g. cookies.txt from a browser export). Gitignored.
IG_COOKIES_FILE: str = _str("IG_COOKIES_FILE", "cookies.txt")
# Full Netscape cookie file contents (for Render secrets — multiline).
IG_COOKIES_NETSCAPE: str = _str("IG_COOKIES_NETSCAPE", "")
IG_SESSIONID: str = _str("IG_SESSIONID", "")
IG_CSRFTOKEN: str = _str("IG_CSRFTOKEN", "")
IG_DS_USER_ID: str = _str("IG_DS_USER_ID", "")
IG_MID: str = _str("IG_MID", "")
IG_DID: str = _str("IG_DID", "")  # ig_did cookie
IG_SESSION_USER_AGENT: str = _str(
    "IG_SESSION_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
)
IG_HANDLE_MIN_SCORE: float = float(_str("IG_HANDLE_MIN_SCORE", "0.55") or "0.55")
IG_SEARCH_GAP_S: float = float(_str("IG_SEARCH_GAP_S", "2.5") or "2.5")
COL_HANDLE_CANDIDATES: str = _str("IG_COL_HANDLE_CANDIDATES", "ig_handle_candidates")
COL_PLACES: str = _str("IG_COL_PLACES", "places_raw")

# ── Venue discovery (Places → Instagram handles) ──────────────────────────────
# auto = Google if GOOGLE_PLACES_API_KEY set, else free OpenStreetMap Overpass
PLACES_BACKEND: str = _str("PLACES_BACKEND", "auto")
GOOGLE_PLACES_API_KEY: str = _str("GOOGLE_PLACES_API_KEY", "")
OVERPASS_URL: str = _str(
    "OVERPASS_URL",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
DISCOVER_CITY: str = _str("DISCOVER_CITY", "lagos")
DISCOVER_PLACE_LIMIT: int = _int("DISCOVER_PLACE_LIMIT", 150)
DISCOVER_RESOLVE_LIMIT: int = _int("DISCOVER_RESOLVE_LIMIT", 40)

# ── Scheduler cadence (local `schedule` CLI + UI; Render cron should match) ────
INGEST_EVERY_MINUTES: int = _int("INGEST_EVERY_MINUTES", 30)
INGEST_LIMIT: int = _int("INGEST_LIMIT", 40)
DISCOVER_EVERY_HOURS: int = _int("DISCOVER_EVERY_HOURS", 24)


def preflight() -> list[str]:
    """Return a list of blocking config problems (empty == good to run)."""
    problems: list[str] = []
    if not MONGODB_URI:
        problems.append("MONGODB_URI is not set")
    if not IG_GRAPH_ACCESS_TOKEN and not IG_FALLBACK_ENABLED:
        problems.append(
            "no source configured: set IG_GRAPH_ACCESS_TOKEN or IG_FALLBACK_ENABLED=true"
        )
    if IG_GRAPH_ACCESS_TOKEN and not IG_GRAPH_USER_ID:
        problems.append("IG_GRAPH_ACCESS_TOKEN is set but IG_GRAPH_USER_ID is empty")
    if IG_CONCURRENCY < 1:
        problems.append("IG_CONCURRENCY must be >= 1")
    if IG_FALLBACK_ENABLED and IG_FALLBACK_MAX_PER_RUN > 150:
        problems.append(
            f"IG_FALLBACK_MAX_PER_RUN={IG_FALLBACK_MAX_PER_RUN} is above the ~150/hr "
            "logged-out ceiling — you will collect interstitials"
        )
    if DEEPSEEK_ENABLED and not DEEPSEEK_API_KEY:
        problems.append("DEEPSEEK_ENABLED=true but DEEPSEEK_API_KEY is empty")
    return problems
