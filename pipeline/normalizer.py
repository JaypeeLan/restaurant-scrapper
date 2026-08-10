"""
Normalize the three source shapes into one canonical raw-post document.

You are storing raw for now (no event parsing), but "raw" still needs a stable
key and a stable timestamp or dedup and change detection don't work. Everything
else is kept verbatim under `source.raw` so a later extraction pass has the full
payload to work from and never needs a refetch.

Source shapes:
  graph      → {id, caption, media_type, media_url, permalink, timestamp, ...}
  web_json   → GraphQL node {shortcode, edge_media_to_caption, taken_at_timestamp}
  embed      → {shortcode, caption, owner}
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")


def _utc(ts: Any) -> datetime | None:
    """Accept unix seconds or ISO-8601 (Graph returns ISO with +0000)."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        raw = ts.strip()
        if raw.isdigit():
            return _utc(int(raw))
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            # Graph's "+0000" form lacks the colon isoformat wants.
            try:
                return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z").astimezone(
                    timezone.utc
                )
            except ValueError:
                return None
    return None


def shortcode_from_permalink(permalink: str | None) -> str | None:
    if not permalink:
        return None
    m = _SHORTCODE_RE.search(permalink)
    return m.group(1) if m else None


def content_hash(doc: dict[str, Any]) -> str:
    """
    Stable hash of the fields that would make us want to re-read a post.

    Caption edits and engagement drift both matter for event detection (a
    restaurant editing "SOLD OUT" into a caption is signal), so both are in.
    """
    parts = [
        str(doc.get("caption") or ""),
        str(doc.get("likeCount") or 0),
        str(doc.get("commentCount") or 0),
        str(doc.get("mediaType") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# ── per-source normalizers ────────────────────────────────────────────────────


def from_graph(item: dict[str, Any], *, handle: str, ig_user_id: str | None = None) -> dict[str, Any]:
    """Instagram Graph API business_discovery media item."""
    permalink = item.get("permalink")
    shortcode = shortcode_from_permalink(permalink)
    caption = item.get("caption") or ""
    doc = {
        "_id": f"{handle}:{shortcode or item.get('id')}",
        "handle": handle,
        "igUserId": ig_user_id,
        "postId": str(item.get("id") or ""),
        "shortcode": shortcode,
        "permalink": permalink,
        "caption": caption,
        "mediaType": item.get("media_type"),
        "mediaUrl": item.get("media_url") or item.get("thumbnail_url"),
        "likeCount": item.get("like_count"),
        "commentCount": item.get("comments_count"),
        "postedAt": _utc(item.get("timestamp")),
        "source": {"name": "graph", "raw": item},
    }
    doc["contentHash"] = content_hash(doc)
    return doc


def from_web_json(node: dict[str, Any], *, handle: str) -> dict[str, Any]:
    """GraphQL / Polaris timeline node scraped from the logged-out profile page."""
    caption = ""
    caption_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if caption_edges:
        caption = ((caption_edges[0] or {}).get("node") or {}).get("text") or ""
    elif isinstance(node.get("caption"), dict):
        caption = node["caption"].get("text") or ""
    elif isinstance(node.get("caption"), str):
        caption = node["caption"]

    shortcode = node.get("shortcode") or node.get("code")
    typename = node.get("__typename") or ""
    media_type_raw = node.get("media_type")
    # Polaris uses numeric media_type (1=image, 2=video, 8=carousel).
    if media_type_raw == 2 or typename in ("GraphVideo", "XIGPolarisVideoMedia") or node.get(
        "is_video"
    ):
        media_type = "VIDEO"
    elif media_type_raw == 8 or typename in ("GraphSidecar", "XIGPolarisSidecarMedia"):
        media_type = "CAROUSEL_ALBUM"
    else:
        media_type = "IMAGE"

    post_id = node.get("pk") or node.get("id") or ""
    if isinstance(post_id, str) and post_id.startswith("POLARIS_"):
        post_id = post_id.removeprefix("POLARIS_")

    owner = node.get("owner") or node.get("user") or {}

    doc = {
        "_id": f"{handle}:{shortcode}",
        "handle": handle,
        "igUserId": str(owner.get("id") or owner.get("pk") or "") or None,
        "postId": str(post_id),
        "shortcode": shortcode,
        "permalink": f"https://www.instagram.com/p/{shortcode}/" if shortcode else None,
        "caption": caption,
        "mediaType": media_type,
        "mediaUrl": node.get("display_url") or node.get("display_uri"),
        "likeCount": (node.get("edge_liked_by") or {}).get("count"),
        "commentCount": (node.get("edge_media_to_comment") or {}).get("count"),
        "postedAt": _utc(node.get("taken_at_timestamp") or node.get("taken_at")),
        "source": {"name": "web_json", "raw": node},
    }
    doc["contentHash"] = content_hash(doc)
    return doc


def from_embed(payload: dict[str, Any], *, handle: str) -> dict[str, Any]:
    """Caption-only top-up from /p/{shortcode}/embed/captioned/."""
    shortcode = payload.get("shortcode")
    doc = {
        "_id": f"{handle}:{shortcode}",
        "handle": handle,
        "postId": None,
        "shortcode": shortcode,
        "permalink": f"https://www.instagram.com/p/{shortcode}/" if shortcode else None,
        "caption": payload.get("caption") or "",
        "mediaType": None,
        "mediaUrl": None,
        "likeCount": None,
        "commentCount": None,
        "postedAt": None,
        "source": {"name": "embed", "raw": payload},
    }
    doc["contentHash"] = content_hash(doc)
    return doc


def profile_from_graph(discovery: dict[str, Any], *, handle: str) -> dict[str, Any]:
    return {
        "handle": handle,
        "igUserId": str(discovery.get("id") or "") or None,
        "name": discovery.get("name"),
        "biography": discovery.get("biography"),
        "website": discovery.get("website"),
        "followers": discovery.get("followers_count"),
        "mediaCount": discovery.get("media_count"),
        "profilePicUrl": discovery.get("profile_picture_url"),
        "isBusiness": True,
        "sourceName": "graph",
    }


def profile_from_web(user: dict[str, Any], *, handle: str) -> dict[str, Any]:
    website = user.get("external_url")
    if not website:
        links = user.get("bio_links") or []
        if links and isinstance(links[0], dict):
            website = links[0].get("url")

    followers = (user.get("edge_followed_by") or {}).get("count")
    if followers is None:
        followers = user.get("follower_count")

    media_count = (user.get("edge_owner_to_timeline_media") or {}).get("count")
    if media_count is None:
        media_count = user.get("all_media_count")

    return {
        "handle": handle,
        "igUserId": str(user.get("id") or user.get("pk") or "") or None,
        "name": user.get("full_name"),
        "biography": user.get("biography"),
        "website": website,
        "followers": followers,
        "mediaCount": media_count,
        "profilePicUrl": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "isBusiness": bool(user.get("is_business_account")),
        "isPrivate": bool(user.get("is_private")),
        "sourceName": "web_json",
    }


def timeline_nodes(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract post nodes from a scraped profile user object (legacy or Polaris)."""
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
    if not edges:
        edges = (user.get("polaris_ordered_timeline_connection") or {}).get("edges") or []
    return [e.get("node") or {} for e in edges if isinstance(e, dict)]


def highlight_edges(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Highlight tray edges from legacy or Polaris profile payloads."""
    return (
        (user.get("edge_highlight_reels") or {}).get("edges")
        or (user.get("lox_highlights_connection") or {}).get("edges")
        or []
    )
