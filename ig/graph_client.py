"""
Instagram Graph API — business_discovery.

The only genuinely free way to read other accounts' posts without ever being
blocked, because you aren't scraping: Meta serves it to you.

    GET /{IG_GRAPH_VERSION}/{your_ig_user_id}
        ?fields=business_discovery.username({handle}){...}
        &access_token=...

What you get per account:
    id, username, name, biography, website, followers_count, media_count,
    profile_picture_url, and media{ id, caption, media_type, media_url,
    permalink, thumbnail_url, timestamp, like_count, comments_count }

What you do NOT get:
    - story highlights (not exposed at all)
    - live stories
    - personal (non-business, non-creator) accounts
    - private accounts

Setup (one time, free):
    1. Create a Meta app at developers.facebook.com → type "Business".
    2. Add the "Instagram Graph API" product.
    3. Convert an IG account you own to Business/Creator, link it to a FB Page.
    4. Grant instagram_basic + pages_show_list + pages_read_engagement.
    5. Exchange for a long-lived token (60 days) and refresh it on a cron.
    6. IG_GRAPH_USER_ID = the numeric IG Business account id from
       GET /me/accounts → /{page_id}?fields=instagram_business_account

Rate limit: ~200 calls/hour against your app/user. One call covers one
restaurant including its recent media, so 1,000 accounts/day sits well
inside the ceiling. The RateLimiter below keeps you there on purpose.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx

from config import settings
from ig.http import CircuitBreaker, UpstreamAbort, get_json

log = logging.getLogger("ig.graph")

_MEDIA_FIELDS = (
    "id,caption,media_type,media_url,permalink,thumbnail_url,"
    "timestamp,like_count,comments_count"
)
_PROFILE_FIELDS = (
    "id,username,name,biography,website,followers_count,follows_count,"
    "media_count,profile_picture_url"
)

# Graph error subcodes that mean "this handle can't be discovered", not "retry".
_NOT_DISCOVERABLE = {
    110,  # user not found
    2207013,  # not a business account
}


class NotDiscoverable(RuntimeError):
    """Handle is private, personal, or does not exist — route to fallback."""


class RateLimiter:
    """
    Sliding-window limiter. Blocks rather than errors, so callers just await.

    Deliberately set below Meta's documented 200/hr: hitting the real ceiling
    returns error code 4/17 and locks you out for the remainder of the window,
    which is far more expensive than waiting a few seconds here.
    """

    def __init__(self, calls_per_hour: int) -> None:
        self.capacity = max(1, calls_per_hour)
        self._window = 3600.0
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self._window:
                    self._calls.popleft()
                if len(self._calls) < self.capacity:
                    self._calls.append(now)
                    return
                wait = self._window - (now - self._calls[0]) + 0.25
                log.info("[graph] rate limit window full — sleeping %.1fs", wait)
                await asyncio.sleep(wait)

    @property
    def used_in_window(self) -> int:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self._window:
            self._calls.popleft()
        return len(self._calls)


class InstagramGraph:
    """Async business_discovery client. Use as an async context manager."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        ig_user_id: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.access_token = (
            access_token if access_token is not None else settings.IG_GRAPH_ACCESS_TOKEN
        )
        self.ig_user_id = ig_user_id or settings.IG_GRAPH_USER_ID
        self.base = (
            f"{settings.IG_GRAPH_BASE_URL.rstrip('/')}/{settings.IG_GRAPH_VERSION}"
        )
        self.limiter = RateLimiter(settings.IG_GRAPH_CALLS_PER_HOUR)
        self.breaker = breaker or CircuitBreaker(settings.IG_CIRCUIT_THRESHOLD)
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.ig_user_id)

    async def __aenter__(self) -> "InstagramGraph":
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=settings.IG_CONCURRENCY * 2)
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_account(
        self, handle: str, *, media_limit: int | None = None, after: str | None = None
    ) -> dict[str, Any]:
        """
        One call → profile fields + up to `media_limit` recent posts.

        Raises NotDiscoverable for private/personal/missing handles so the
        caller can hand them to the Playwright fallback.
        """
        if self._client is None:
            raise RuntimeError("InstagramGraph must be used as an async context manager")
        if not self.configured:
            raise RuntimeError("IG_GRAPH_ACCESS_TOKEN / IG_GRAPH_USER_ID not set")

        self.breaker.check()
        await self.limiter.acquire()

        limit = media_limit or settings.IG_GRAPH_MEDIA_LIMIT
        media_args = f"limit({limit})" if not after else f"limit({limit}).after({after})"
        fields = (
            f"business_discovery.username({handle})"
            f"{{{_PROFILE_FIELDS},media.{media_args}{{{_MEDIA_FIELDS}}}}}"
        )

        try:
            payload = await get_json(
                self._client,
                f"{self.base}/{self.ig_user_id}",
                params={"fields": fields, "access_token": self.access_token},
                max_attempts=3,
                label=f"graph:{handle}",
            )
        except UpstreamAbort as exc:
            # Graph returns 400 with a subcode for undiscoverable handles.
            text = str(exc)
            if any(str(code) in text for code in _NOT_DISCOVERABLE):
                raise NotDiscoverable(f"{handle}: not discoverable via Graph") from exc
            self.breaker.record_failure()
            raise
        except Exception:
            self.breaker.record_failure()
            raise

        error = payload.get("error")
        if error:
            subcode = error.get("error_subcode") or error.get("code")
            if subcode in _NOT_DISCOVERABLE:
                raise NotDiscoverable(f"{handle}: {error.get('message')}")
            self.breaker.record_failure()
            raise RuntimeError(f"graph:{handle}: {error}")

        discovery = payload.get("business_discovery")
        if not discovery:
            raise NotDiscoverable(f"{handle}: empty business_discovery response")

        self.breaker.record_success()
        return discovery

    async def paginate_media(
        self, handle: str, *, max_pages: int = 1, stop_at_id: str | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """
        Profile + media across pages, stopping at the newest post we already have.

        Returns (profile_fields, media_items). The early stop is what keeps a
        1,000-account daily cycle at ~1,000 calls instead of ~5,000.
        """
        profile: dict[str, Any] = {}
        collected: list[dict[str, Any]] = []
        cursor: str | None = None

        for _ in range(max(1, max_pages)):
            discovery = await self.fetch_account(handle, after=cursor)
            if not profile:
                profile = {k: v for k, v in discovery.items() if k != "media"}

            media = discovery.get("media") or {}
            items = media.get("data") or []
            if not items:
                break

            for item in items:
                if stop_at_id and str(item.get("id")) == str(stop_at_id):
                    return profile, collected
                collected.append(item)

            cursor = ((media.get("paging") or {}).get("cursors") or {}).get("after")
            if not cursor or not (media.get("paging") or {}).get("next"):
                break

        return profile, collected


def daily_call_budget(calls_per_hour: int | None = None) -> int:
    """How many Graph calls a 24h cycle can make at the configured rate."""
    return (calls_per_hour or settings.IG_GRAPH_CALLS_PER_HOUR) * 24
