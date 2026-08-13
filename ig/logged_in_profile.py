"""
Logged-in Instagram profile read — bio, link-in-bio, and grid images.

Instagram's JSON endpoints are not usable for this: `web_profile_info` returns
a server-side 400 (`ig_business_category_subvertical has been deleted`) on both
`www` and `i` hosts, and the logged-in HTML renders only the *viewer*, never the
profile being visited. The rendered page is the only route that works.

Everything here is read-only. Pacing defaults to the same conservative gap the
ingest fallback uses — this session has been checkpointed once already, and a
tight loop is what does that.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any
from urllib.parse import unquote

from config import settings
from ig import logged_in_search as L

log = logging.getLogger("ig.profile")

_PROFILE_JS = """
() => {
  const header = document.querySelector('header');
  const links = Array.from(document.querySelectorAll('header a[href]'))
    .map(a => a.href);
  const imgs = Array.from(document.querySelectorAll('main img, article img'))
    .map(i => i.src)
    .filter(s => s && s.includes('fbcdn'));
  return {
    headerText: header ? header.innerText : '',
    links,
    images: imgs.slice(0, 12),
  };
}
"""

# 'est. 2019', 'established 2018', 'since 2021'
_ESTABLISHED = re.compile(
    r"\b(?:est\.?|established|since|serving since)\s*[:\-]?\s*((?:19|20)\d{2})\b",
    re.I,
)
_WA_LINK = re.compile(r"(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\+?\d[\d\s-]{6,})", re.I)
# Nigerian numbers: 080…, +23480…, 0700…
_PHONE = re.compile(r"(\+?234[\d\s-]{7,}|\b0[789]\d[\d\s-]{6,})")
_WA_WORD = re.compile(r"\bwhat'?s\s?app\b", re.I)


def _clean_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("0") and len(digits) >= 11:
        digits = "+234" + digits[1:]
    return digits


def parse_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Rendered profile → the Restaurant fields no other source carries."""
    text = profile.get("headerText") or ""
    links = profile.get("links") or []
    out: dict[str, Any] = {}

    # whatsApp — an explicit wa.me link is definitive; a bio phone number
    # beside the word "WhatsApp" is the common Lagos pattern.
    for link in links:
        m = _WA_LINK.search(link)
        if m:
            out["whatsApp"] = _clean_phone(m.group(1))
            break
        short = re.search(r"wa\.me/message/([A-Z0-9]{6,})", link, re.I)
        if short:
            out["whatsApp"] = f"https://wa.me/message/{short.group(1)}"
            break
    if "whatsApp" not in out and _WA_WORD.search(text):
        m = _PHONE.search(text)
        if m:
            out["whatsApp"] = _clean_phone(m.group(1))

    m = _ESTABLISHED.search(text)
    if m:
        out["dateEstablished"] = f"{m.group(1)}-01-01"

    # The avatar is served from the profile-pic CDN bucket at 150px; feeding it
    # to a vision model would judge the logo, not the room.
    images = [
        i for i in (profile.get("images") or [])
        if i and "profile_pic" not in i and "t51.2885-19" not in i
    ]
    if images:
        out["photos"] = images[:6]
        # Guests and the venue itself posting photos is positive evidence that
        # photography is permitted. Absence of posts is not evidence of a ban,
        # so this is only ever set true.
        out["photographyAllowed"] = True

    # Link-in-bio is where Lagos venues publish menus. Instagram wraps outbound
    # links in l.instagram.com/?u=<encoded>, and some bios show the destination
    # as plain text with no anchor at all.
    outbound: list[str] = []
    for link in links:
        if "l.instagram.com" in link:
            m = re.search(r"[?&]u=([^&]+)", link)
            if m:
                outbound.append(unquote(m.group(1)).split("?fbclid")[0])
        elif "instagram.com" not in link.lower():
            outbound.append(link)

    # Prefer a link that looks like a menu over whatever happens to be first —
    # one bio led with /cafe/house-rules, which is not a menu.
    def _rank(url: str) -> int:
        low = url.lower()
        if re.search(r"\b(menu|carte|food|drinks)\b", low):
            return 0
        if any(host in low for host in ("linktr.ee", "bento.me", "milkshake", "menus.ws")):
            return 1
        return 2

    bio_link = sorted(outbound, key=_rank)[0] if outbound else None
    if not bio_link:
        m = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+){1,3}/?[^\s|]*)", text, re.I)
        if m and "instagram" not in m.group(1).lower():
            candidate = m.group(1).rstrip(".,")
            if "." in candidate and not candidate.replace(".", "").isdigit():
                bio_link = f"https://{candidate}"
    if bio_link:
        out["linkInBio"] = bio_link
    return out


async def _fetch_many(handles: list[str], *, gap: tuple[float, float]) -> dict[str, dict[str, Any]]:
    from playwright.async_api import async_playwright

    cookies = L._cookie_map()
    if not cookies:
        raise RuntimeError("no Instagram cookies configured")
    headers = L._headers()
    agent = headers.get("user-agent") or headers.get("User-Agent")

    results: dict[str, dict[str, Any]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.SCRAPER_HEADLESS)
        context = await browser.new_context(user_agent=agent)
        await context.add_cookies([
            {"name": k, "value": v, "domain": ".instagram.com", "path": "/"}
            for k, v in cookies.items()
        ])
        try:
            for idx, handle in enumerate(handles):
                if idx:
                    await asyncio.sleep(random.uniform(*gap))
                page = await context.new_page()
                try:
                    await page.goto(
                        f"https://www.instagram.com/{handle}/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await page.wait_for_timeout(6_000)
                    raw = await page.evaluate(_PROFILE_JS)
                    if not (raw or {}).get("headerText"):
                        log.warning("[ig-profile] %s rendered no header", handle)
                        continue
                    results[handle] = {"handle": handle, **raw}
                except Exception as exc:  # noqa: BLE001
                    log.warning("[ig-profile] %s failed: %s", handle, exc)
                finally:
                    await page.close()
        finally:
            await browser.close()
    return results


def fetch_profiles(
    handles: list[str],
    *,
    min_gap_s: float | None = None,
    max_gap_s: float | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Read several profiles in one browser session.

    Pacing between profiles defaults to the ingest fallback's gap. Lower it
    only for small test runs.
    """
    wanted = [h for h in dict.fromkeys(h.strip().lstrip("@") for h in handles if h) if h]
    if not wanted:
        return {}
    gap = (
        settings.IG_FALLBACK_MIN_GAP_S if min_gap_s is None else min_gap_s,
        settings.IG_FALLBACK_MAX_GAP_S if max_gap_s is None else max_gap_s,
    )
    return asyncio.run(_fetch_many(wanted, gap=gap))
