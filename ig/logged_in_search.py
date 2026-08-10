"""
Logged-in Instagram web session — handle discovery only.

Uses cookie auth (sessionid + csrftoken) against Instagram's topsearch API.
This is NOT for ingesting posts/stories; post ingest stays on Graph / logged-out.

Session cookies belong in `.env` only. Never commit them. Rotate immediately if
a session was pasted into chat, logs, or a ticket.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

from config import settings

log = logging.getLogger("ig.logged_in")

_IG_APP_ID = "936619743392459"
_TOPSEARCH = "https://www.instagram.com/api/v1/web/search/topsearch/"
_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$", re.I)


class LoggedInAuthError(RuntimeError):
    """Session missing, expired, or challenged."""


def _parse_cookie_header(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _parse_netscape_cookie_file(path: Path) -> dict[str, str]:
    """Parse a Netscape / curl cookie jar (tabs)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # sometimes spaces instead of tabs after copy/paste
            parts = re.split(r"\s+", line)
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _expires, name, value = parts[:7]
        if "instagram.com" not in domain:
            continue
        out[name] = value
    return out


def _cookie_map() -> dict[str, str]:
    """
    Merge cookie sources. Precedence (highest last):
    individual env → IG_COOKIES header → Netscape cookie file.
    So cookies.txt / export wins over stale IG_SESSIONID in .env.
    """
    materialize_cookies_file()
    merged: dict[str, str] = {}
    if settings.IG_SESSIONID:
        merged["sessionid"] = settings.IG_SESSIONID.strip().strip('"')
    if settings.IG_CSRFTOKEN:
        merged["csrftoken"] = settings.IG_CSRFTOKEN.strip().strip('"')
    if settings.IG_DS_USER_ID:
        merged["ds_user_id"] = settings.IG_DS_USER_ID.strip().strip('"')
    if settings.IG_MID:
        merged["mid"] = settings.IG_MID.strip().strip('"')
    if settings.IG_DID:
        merged["ig_did"] = settings.IG_DID.strip().strip('"')
    merged.update(_parse_cookie_header(settings.IG_COOKIES))
    cookie_file = (settings.IG_COOKIES_FILE or "").strip()
    if cookie_file:
        merged.update(_parse_netscape_cookie_file(Path(cookie_file)))
    return merged


def materialize_cookies_file() -> Path | None:
    """
    On Render (and similar), paste Netscape cookies into IG_COOKIES_NETSCAPE
    or a Cookie header into IG_COOKIES. Writes cookies.txt when needed so
    IG_COOKIES_FILE keeps working.
    """
    path = Path(settings.IG_COOKIES_FILE or "cookies.txt")
    netscape = (settings.IG_COOKIES_NETSCAPE or "").strip()
    if netscape:
        path.write_text(netscape if netscape.endswith("\n") else netscape + "\n", encoding="utf-8")
        return path
    # Already have a file (local) or only IG_COOKIES header (parsed in-memory).
    if path.is_file():
        return path
    return None


def session_configured() -> bool:
    materialize_cookies_file()
    cookies = _cookie_map()
    return bool(cookies.get("sessionid") and cookies.get("csrftoken"))


def _cookie_header() -> str:
    cookies = _cookie_map()
    preferred = [
        "sessionid",
        "csrftoken",
        "ds_user_id",
        "mid",
        "ig_did",
        "datr",
        "rur",
        "wd",
    ]
    parts: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in cookies and cookies[key]:
            parts.append(f"{key}={cookies[key]}")
            seen.add(key)
    for key, val in cookies.items():
        if key in seen or not val:
            continue
        parts.append(f"{key}={val}")
    return "; ".join(parts)


def _headers() -> dict[str, str]:
    cookies = _cookie_map()
    csrf = cookies.get("csrftoken") or settings.IG_CSRFTOKEN
    return {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/",
        "user-agent": settings.IG_SESSION_USER_AGENT,
        "x-asbd-id": "359341",
        "x-csrftoken": unquote(csrf) if csrf else "",
        "x-ig-app-id": _IG_APP_ID,
        "x-requested-with": "XMLHttpRequest",
        "cookie": _cookie_header(),
    }


def _normalize_user(raw: dict[str, Any]) -> dict[str, Any] | None:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    if not isinstance(user, dict):
        return None
    handle = (
        user.get("username")
        or user.get("user_name")
        or ""
    ).strip().lstrip("@").lower()
    if not handle or not _HANDLE_RE.match(handle):
        return None
    return {
        "handle": handle,
        "igUserId": str(user.get("pk") or user.get("id") or "") or None,
        "fullName": user.get("full_name") or user.get("fullName") or "",
        "isPrivate": bool(user.get("is_private")),
        "isVerified": bool(user.get("is_verified")),
        "followerCount": user.get("follower_count") or user.get("followerCount"),
        "profilePicUrl": user.get("profile_pic_url") or user.get("profilePicUrl"),
        "searchSocialContext": raw.get("social_context") or user.get("social_context"),
    }


def score_handle(query: str, candidate: dict[str, Any]) -> float:
    """0–1 relevance of a topsearch hit to a venue query (e.g. 'shiro lagos')."""
    q = re.sub(r"[^a-z0-9\s]", " ", query.lower())
    tokens = [t for t in q.split() if len(t) > 1]
    if not tokens:
        return 0.0

    handle = (candidate.get("handle") or "").lower()
    name = (candidate.get("fullName") or "").lower()
    blob = f"{handle} {name}"
    compact_q = "".join(tokens)

    score = 0.0
    hits = sum(1 for t in tokens if t in blob)
    score += hits / len(tokens)

    primary = max(tokens, key=len)
    if primary in handle:
        score += 0.25
    if primary in name:
        score += 0.15
    # Prefer @shirolagos over @pay2shoplagos for query "shiro lagos"
    if compact_q and compact_q in handle.replace("_", "").replace(".", ""):
        score += 0.35
    if candidate.get("isVerified"):
        score += 0.05
    if candidate.get("isPrivate"):
        score -= 0.35
    # Penalize handles that look like people/personal shopping
    if re.search(r"(shopper|blog|foodie|pay\d|wife|hubby)", handle + name, re.I):
        score -= 0.25

    return max(0.0, min(1.0, score))


def topsearch_users(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """
    Logged-in Instagram typeahead / topsearch — returns user candidates only.
    """
    if not session_configured():
        raise LoggedInAuthError(
            "IG_SESSIONID and IG_CSRFTOKEN required for logged-in handle search"
        )

    q = (query or "").strip()
    if len(q) < 2:
        return []

    url = (
        f"{_TOPSEARCH}?context=blended&query={quote(q)}"
        f"&include_reel=false&search_surface=web_top_search"
    )
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=_headers())
    except httpx.HTTPError as exc:
        raise LoggedInAuthError(f"topsearch network error: {exc}") from exc

    if resp.status_code in (401, 403):
        raise LoggedInAuthError(
            f"topsearch HTTP {resp.status_code} — session expired or challenged; "
            "refresh cookies in .env"
        )
    if resp.status_code == 429:
        raise LoggedInAuthError("topsearch rate-limited (429) — back off")
    if resp.status_code >= 400:
        raise LoggedInAuthError(
            f"topsearch HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        # Login wall / HTML challenge pages.
        raise LoggedInAuthError(
            "topsearch returned non-JSON (login wall or challenge page)"
        ) from exc

    users_raw = payload.get("users") or []
    out: list[dict[str, Any]] = []
    for row in users_raw:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_user(row)
        if not normalized:
            continue
        normalized["score"] = round(score_handle(q, normalized), 3)
        normalized["query"] = q
        out.append(normalized)

    out.sort(key=lambda u: u.get("score") or 0, reverse=True)
    return out[: max(1, limit)]


def search_best_handle(
    query: str,
    *,
    min_score: float | None = None,
) -> dict[str, Any] | None:
    """Return the best user hit above min_score, or None."""
    threshold = (
        settings.IG_HANDLE_MIN_SCORE if min_score is None else min_score
    )
    variants = [query.strip()]
    # "Tantalizers Lagos" sometimes returns empty; bare name can still hit.
    parts = query.strip().split()
    if len(parts) > 1:
        bare = " ".join(parts[:-1]).strip()
        if bare and bare.lower() not in {v.lower() for v in variants}:
            variants.append(bare)

    best: dict[str, Any] | None = None
    for q in variants:
        hits = topsearch_users(q, limit=8)
        if not hits:
            continue
        candidate = dict(hits[0])
        candidate["query"] = query.strip()  # keep original for provenance
        if best is None or (candidate.get("score") or 0) > (best.get("score") or 0):
            best = candidate
        if (best.get("score") or 0) >= 0.9:
            break

    if not best or (best.get("score") or 0) < threshold:
        return None
    return best


def search_many(
    queries: list[str],
    *,
    min_score: float | None = None,
    gap_s: float | None = None,
) -> list[dict[str, Any]]:
    """Run topsearch for each query; return best hits that clear the threshold."""
    gap = settings.IG_SEARCH_GAP_S if gap_s is None else gap_s
    results: list[dict[str, Any]] = []
    for i, query in enumerate(queries):
        q = query.strip()
        if not q or q.startswith("#"):
            continue
        try:
            best = search_best_handle(q, min_score=min_score)
        except LoggedInAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("search failed for %r: %s", q, exc)
            best = None
        if best:
            results.append(best)
            log.info(
                "[search] %r → @%s (%.2f)",
                q,
                best["handle"],
                best.get("score") or 0,
            )
        else:
            log.info("[search] %r → no confident handle", q)
        if i < len(queries) - 1 and gap > 0:
            time.sleep(gap)
    return results
