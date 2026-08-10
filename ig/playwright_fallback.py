"""
Free self-hosted fallback — logged-out instagram.com via a stealth browser.

Read this before turning the volume up
──────────────────────────────────────
This path scrapes instagram.com directly and violates Instagram's Terms of
Service. It exists for what the Graph API can't reach — story highlights, and
personal/non-business accounts — not as a primary source.

The honest ceiling: from a single residential IP, logged-out Instagram
tolerates roughly 100–150 profile fetches per hour. Past that you get the
"Please wait a few minutes before you try again" interstitial, which then
persists for that IP for hours and sometimes escalates to a hard block on the
/{handle}/ HTML route specifically. No amount of stealth changes this — it is
IP-level velocity accounting, not fingerprinting.

So the strategy is: keep this permanently low-volume and let it drain across
days, rather than trying to beat the limit.

What gets you blocked, in order of how fast it happens:

 1. Velocity from one IP.   The dominant factor by far. → IG_FALLBACK_MAX_PER_RUN
                            plus 20–55s randomised gaps and concurrency of 2.
 2. Datacenter IPs.         Instagram classifies AWS/GCP/Hetzner ranges on
                            sight — a VPS gets blocked in minutes where a home
                            connection lasts hours. → run this from a residential
                            connection, or set IG_PROXY_URL if you have one.
 3. Logging in.             A burner account doing bulk profile views is gone in
                            days and burns the IP with it. → this module is
                            logged-OUT only. It never sends cookies. Don't add them.
 4. Headless fingerprint.   navigator.webdriver, missing plugins, 800x600
                            viewport. → playwright-stealth + realistic context.
 5. Hardcoded GraphQL hashes. doc_id values rot in weeks. → we read the JSON
                            embedded in the HTML page, which changes far more
                            slowly, and the /embed/captioned/ route for posts,
                            which has been stable for years.

The circuit breaker matters more here than anywhere else: once Instagram starts
serving interstitials, every further request deepens the block. Fail fast, stop
for the run, try again in the next cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from config import settings

log = logging.getLogger("ig.fallback")

# Deliberately tiny. This is a tail-case path, not a throughput path.
_MAX_CONCURRENCY = 2


def _browsers_dir() -> Path:
    root = Path(__file__).resolve().parents[1]
    configured = (settings.PLAYWRIGHT_BROWSERS_PATH or "ms-playwright").strip()
    path = Path(configured)
    if not path.is_absolute():
        path = root / path
    path.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(path)
    return path


def ensure_playwright_browsers() -> Path:
    """
    Keep Chromium under the project tree.

    Render's ~/.cache path used during `playwright install` is not reliably
    present when the cron runtime starts, which surfaces as
    "Executable doesn't exist ... chromium_headless_shell".
    """
    path = _browsers_dir()
    shell = next(
        path.glob(
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
        ),
        None,
    )
    full = next(path.glob("chromium-*/chrome-linux64/chrome"), None)
    # macOS local paths (for dev)
    if shell is None:
        shell = next(
            path.glob(
                "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"
            ),
            None,
        )
    if full is None:
        full = next(path.glob("chromium-*/chrome-mac-*/Google Chrome for Testing"), None)
    if shell is not None or full is not None:
        return path

    log.info("[fallback] installing Playwright Chromium into %s", path)
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(path)},
    )
    return path

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

_VIEWPORTS = [
    {"width": 1512, "height": 858},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]

# Interstitials that mean "back off", not "no such account".
_BLOCK_MARKERS = (
    "Please wait a few minutes before you try again",
    "challenge_required",
    "login_required",
    "Something went wrong. There's an issue and the page could not be loaded",
)

# Soft misses — account missing / gated for logged-out viewers, not a block.
_UNAVAILABLE_MARKERS = (
    "Profile isn't available",
    "Sorry, this page isn't available.",
)


class Blocked(RuntimeError):
    """Instagram served a rate-limit or challenge interstitial. Stop the run."""


def _proxy_config() -> dict[str, str] | None:
    """Optional generic proxy. Empty IG_PROXY_URL means: use your own IP."""
    url = settings.IG_PROXY_URL
    if not url:
        return None
    m = re.match(r"^(https?|socks5)://([^:]+):([^@]+)@(.+)$", url)
    if not m:
        return {"server": url}
    scheme, username, password, hostport = m.groups()
    return {
        "server": f"{scheme}://{hostport}",
        "username": username,
        "password": password,
    }


def _extract_embedded_json(html: str) -> list[Any]:
    """
    Pull the JSON payloads Instagram inlines into the HTML shell.

    More durable than the private GraphQL endpoints, whose doc_id values rotate
    every few weeks.
    """
    blobs: list[Any] = []
    for match in re.finditer(
        r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            blobs.append(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
    for match in re.finditer(
        r"window\.__additionalDataLoaded\s*\([^,]+,\s*(\{.*?\})\);", html, re.S
    ):
        try:
            blobs.append(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
    return blobs


def _iter_xig_users(blobs: list[Any]) -> list[dict[str, Any]]:
    """Collect every `xig_user_by_username` dict Instagram inlined (Polaris)."""
    found: list[dict[str, Any]] = []
    stack: list[Any] = list(blobs)
    visited = 0
    while stack and visited < 100_000:
        node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            user = node.get("xig_user_by_username")
            if isinstance(user, dict):
                found.append(user)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _merge_xig_user(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Logged-out Polaris splits profile + timeline across multiple Relay payloads
    that share the same `xig_user_by_username` key. Merge them into one user.
    """
    merged: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if key == "polaris_ordered_timeline_connection" and isinstance(value, dict):
                existing = merged.get(key)
                if not isinstance(existing, dict) or len(value.get("edges") or []) > len(
                    existing.get("edges") or []
                ):
                    merged[key] = value
            elif value is not None or key not in merged:
                merged[key] = value
    return merged


def _find_user_node(blobs: list[Any]) -> dict[str, Any] | None:
    """
    Depth-first hunt for an IG user object.

    Prefer the current Polaris shape (`xig_user_by_username`, possibly split
    across blobs). Fall back to the older GraphQL profile node that carried
    `edge_owner_to_timeline_media` / `biography` inline.
    """
    xig_parts = _iter_xig_users(blobs)
    if xig_parts:
        return _merge_xig_user(xig_parts)

    stack: list[Any] = list(blobs)
    visited = 0
    while stack and visited < 100_000:
        node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            if "username" in node and (
                "edge_owner_to_timeline_media" in node or "biography" in node
            ):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


class PlaywrightFallback:
    """
    Logged-out profile / post fetch via a stealth browser.

        async with PlaywrightFallback() as fb:
            user = await fb.fetch_profile("some_restaurant")
            caption = await fb.fetch_post_caption("DEiyb48AeB9")
    """

    def __init__(self, *, max_per_run: int | None = None) -> None:
        self._pw = None
        self._browser = None
        self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._used = 0
        self._max_per_run = (
            max_per_run if max_per_run is not None else settings.IG_FALLBACK_MAX_PER_RUN
        )
        self._consecutive_blocks = 0
        self.stats = {"attempted": 0, "ok": 0, "blocked": 0, "empty": 0}

    async def __aenter__(self) -> "PlaywrightFallback":
        from playwright.async_api import async_playwright

        ensure_playwright_browsers()
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": settings.SCRAPER_HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if settings.IG_PROXY_URL:
            launch_kwargs["proxy"] = {"server": settings.IG_PROXY_URL}
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = self._pw = None

    @property
    def exhausted(self) -> bool:
        return self._used >= self._max_per_run

    async def _new_context(self):
        return await self._browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            viewport=random.choice(_VIEWPORTS),
            locale="en-US",
            timezone_id="America/New_York",
            proxy=_proxy_config(),
            java_script_enabled=True,
        )

    async def _prepare(self, page) -> None:
        try:
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(page)
        except Exception as exc:  # noqa: BLE001 — stealth is best-effort
            log.debug("[fallback] playwright-stealth skipped: %s", exc)

        # Images/fonts/media are pure cost — we only want the inline JSON.
        async def _route(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _route)

    async def _pace(self) -> None:
        await asyncio.sleep(
            random.uniform(settings.IG_FALLBACK_MIN_GAP_S, settings.IG_FALLBACK_MAX_GAP_S)
        )

    def _guard(self, handle: str) -> bool:
        if self.exhausted:
            log.info(
                "[fallback] per-run cap reached (%d) — skipping %s",
                self._max_per_run,
                handle,
            )
            return False
        if self._consecutive_blocks >= 3:
            raise Blocked("three consecutive blocks — abandoning fallback for this run")
        return True

    async def fetch_profile(self, handle: str) -> dict[str, Any] | None:
        """IG user node with the ~12 most recent grid posts, or None."""
        if not self._guard(handle):
            return None

        async with self._sem:
            self._used += 1
            self.stats["attempted"] += 1
            await self._pace()

            context = await self._new_context()
            try:
                page = await context.new_page()
                await self._prepare(page)

                resp = await page.goto(
                    f"https://www.instagram.com/{handle}/",
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )

                if resp is not None and resp.status == 429:
                    self._consecutive_blocks += 1
                    self.stats["blocked"] += 1
                    raise Blocked(f"{handle}: HTTP 429")

                html = await page.content()
                title = await page.title()
                if any(marker in html for marker in _BLOCK_MARKERS):
                    self._consecutive_blocks += 1
                    self.stats["blocked"] += 1
                    raise Blocked(f"{handle}: interstitial served")

                if any(marker in html or marker in title for marker in _UNAVAILABLE_MARKERS):
                    self.stats["empty"] += 1
                    log.warning("[fallback] %s: profile unavailable logged-out", handle)
                    return None

                user = _find_user_node(_extract_embedded_json(html))
                if user is None:
                    self.stats["empty"] += 1
                    log.warning("[fallback] %s: no user node in page JSON", handle)
                    return None

                self._consecutive_blocks = 0
                self.stats["ok"] += 1
                return user
            finally:
                await context.close()

    async def fetch_post_caption(self, shortcode: str) -> dict[str, Any] | None:
        """
        Caption + poster for one post via the public embed route.

        /p/{shortcode}/embed/captioned/ is a lightweight, long-stable, oEmbed-
        adjacent page. It is markedly less rate-limited than the profile route,
        which makes it the right way to top up captions for posts you already
        discovered elsewhere.
        """
        if not self._guard(shortcode):
            return None

        async with self._sem:
            self._used += 1
            self.stats["attempted"] += 1
            await self._pace()

            context = await self._new_context()
            try:
                page = await context.new_page()
                await self._prepare(page)
                await page.goto(
                    f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                html = await page.content()

                if any(marker in html for marker in _BLOCK_MARKERS):
                    self._consecutive_blocks += 1
                    self.stats["blocked"] += 1
                    raise Blocked(f"{shortcode}: interstitial served")

                caption = None
                m = re.search(
                    r'<div class="Caption".*?</div>\s*</div>', html, re.S
                ) or re.search(r'"edge_media_to_caption".*?"text":\s*"(.*?)"', html, re.S)
                if m:
                    raw = m.group(1) if m.re.groups else m.group(0)
                    caption = re.sub(r"<[^>]+>", " ", raw)
                    caption = re.sub(r"\s+", " ", caption).strip()

                owner = None
                om = re.search(r'"owner":\s*\{[^}]*"username":\s*"([^"]+)"', html)
                if om:
                    owner = om.group(1)

                self._consecutive_blocks = 0
                self.stats["ok"] += 1
                return {"shortcode": shortcode, "caption": caption, "owner": owner}
            finally:
                await context.close()

    async def fetch_highlights(self, handle: str) -> list[dict[str, Any]]:
        """
        Highlight trays for a handle.

        Instagram gates the highlight reel API behind login, so logged-out we
        can only recover what the profile page inlines: the tray ids, titles
        and covers. That's enough to detect that a new "Events" highlight
        appeared; reading the slides inside it is not available for free.
        """
        user = await self.fetch_profile(handle)
        if not user:
            return []

        trays: list[dict[str, Any]] = []
        edges = (
            (user.get("edge_highlight_reels") or {}).get("edges")
            or (user.get("lox_highlights_connection") or {}).get("edges")
            or []
        )
        for edge in edges:
            node = edge.get("node") or {}
            trays.append(
                {
                    "id": str(node.get("id") or ""),
                    "title": node.get("title"),
                    "coverUrl": (node.get("cover_media") or {}).get("thumbnail_src"),
                    "mediaCount": node.get("media_count"),
                }
            )
        return trays
