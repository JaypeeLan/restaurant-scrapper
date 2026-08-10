"""
Resilient HTTP for ScrapeCreators (timeouts, retries, jitter, circuit breaker).

Adapted from validds/scraper/browser/scrapecreators_http.py. The important
difference from a naive client: we distinguish *retryable* upstream hiccups
(429/5xx, connect timeouts) from *abort* conditions (401/402/403 — bad key or
out of credits). Retrying an abort condition is how you burn a plan and get
your key throttled.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger("ig.http")

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=45.0)

_RETRYABLE_EXC = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Auth / billing — alert and abort the run. Never retry these.
_ABORT_STATUS = frozenset({401, 402, 403})


class UpstreamAbort(RuntimeError):
    """Non-retryable upstream failure — bad key, no credits, forbidden."""


class CircuitOpen(RuntimeError):
    """Too many consecutive failures — stop hammering the upstream."""


class CircuitBreaker:
    """
    Trips after `threshold` consecutive failures.

    The point is not politeness, it's damage control: if ScrapeCreators starts
    returning errors for every handle, continuing costs money and produces
    nothing. Any success resets the counter.
    """

    def __init__(self, threshold: int = 12) -> None:
        self.threshold = max(1, threshold)
        self._consecutive_failures = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold:
            self._tripped = True
            log.error(
                "[circuit] OPEN after %d consecutive failures — aborting run",
                self._consecutive_failures,
            )

    def check(self) -> None:
        if self._tripped:
            raise CircuitOpen(
                f"circuit open after {self._consecutive_failures} consecutive failures"
            )


async def jitter_sleep(min_ms: int, max_ms: int) -> None:
    """
    Randomised pacing between requests.

    Uniform-interval request patterns are trivially fingerprintable. Even
    against a paid API this smooths burst load and keeps you off rate limits.
    """
    if max_ms <= 0:
        return
    lo, hi = min(min_ms, max_ms), max(min_ms, max_ms)
    await asyncio.sleep(random.uniform(lo, hi) / 1000.0)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 4,
    label: str = "",
) -> dict[str, Any]:
    """
    GET with exponential backoff + full jitter. Raises UpstreamAbort on
    401/402/403 and the last httpx error if every attempt fails.
    """
    last_exc: BaseException | None = None
    tag = label or url

    for attempt in range(max_attempts):
        try:
            resp = await client.get(
                url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT
            )

            if resp.status_code in _ABORT_STATUS:
                body = (resp.text or "")[:300]
                raise UpstreamAbort(f"{tag} HTTP {resp.status_code}: {body}")

            if resp.status_code in _RETRYABLE_STATUS and attempt + 1 < max_attempts:
                # Honour Retry-After when the upstream sends one.
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), 60.0)
                else:
                    base = min(2.0 * (2**attempt), 30.0)
                    delay = random.uniform(0, base)  # full jitter
                log.warning(
                    "[http] %s HTTP %s (attempt %d/%d), retry in %.1fs",
                    tag,
                    resp.status_code,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"data": data}

        except UpstreamAbort:
            raise
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            base = min(2.0 * (2**attempt), 30.0)
            delay = random.uniform(0, base)
            log.warning(
                "[http] %s %s (attempt %d/%d), retry in %.1fs",
                tag,
                type(exc).__name__,
                attempt + 1,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            break

    raise last_exc or RuntimeError(f"{tag}: exhausted {max_attempts} attempts")
