"""
Ingest orchestrator.

Flow per account:
    1. Graph API business_discovery — optional; used when credentials are set.
    2. If Graph is unset, or NotDiscoverable (private / personal / missing) →
       queue for Playwright.
    3. Playwright drains at low volume with a hard per-run cap, logged out.
    4. Normalize → change-detect → upsert raw → reschedule by tier.

The two failure modes that actually matter are handled explicitly:
    - Rate window exhaustion. The limiter blocks instead of erroring, and the
      run stops once the due queue is drained rather than spinning.
    - Playwright blocks. Three consecutive interstitials abandon the scrape for
      the whole run; the affected accounts stay due and get picked up next cycle.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from config import settings
from ig.graph_client import InstagramGraph, NotDiscoverable
from ig.http import CircuitOpen
from pipeline import normalizer as norm
from pipeline import store, tiers

log = logging.getLogger("ig.ingest")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


class Ingestor:
    def __init__(self, db, *, dry_run: bool = False) -> None:
        self.db = db
        self.dry_run = dry_run
        self.fallback_queue: list[dict[str, Any]] = []
        self.stats = {
            "accounts": 0,
            "graphOk": 0,
            "graphMissed": 0,
            "fallbackOk": 0,
            "failed": 0,
            "postsNew": 0,
            "postsChanged": 0,
            "postsUnchanged": 0,
        }
        # Per-handle outcomes for the Runs UI (stable order = due-queue order).
        self.account_results: list[dict[str, Any]] = []
        self._by_handle: dict[str, dict[str, Any]] = {}

    def _seed_accounts(self, accounts: list[dict[str, Any]]) -> None:
        for account in accounts:
            handle = str(account.get("handle") or "").strip().lstrip("@").lower()
            if not handle:
                continue
            row = {
                "handle": handle,
                "tier": account.get("tier"),
                "status": "queued",
                "source": None,
                "postsNew": 0,
                "postsChanged": 0,
                "error": None,
            }
            self._by_handle[handle] = row
            self.account_results.append(row)

    def _touch(
        self,
        handle: str,
        *,
        status: str | None = None,
        source: str | None = None,
        posts_new: int | None = None,
        posts_changed: int | None = None,
        error: str | None = None,
    ) -> None:
        handle = handle.strip().lstrip("@").lower()
        row = self._by_handle.get(handle)
        if row is None:
            row = {
                "handle": handle,
                "tier": None,
                "status": "queued",
                "source": None,
                "postsNew": 0,
                "postsChanged": 0,
                "error": None,
            }
            self._by_handle[handle] = row
            self.account_results.append(row)
        if status is not None:
            row["status"] = status
        if source is not None:
            row["source"] = source
        if posts_new is not None:
            row["postsNew"] = posts_new
        if posts_changed is not None:
            row["postsChanged"] = posts_changed
        if error is not None:
            row["error"] = error[:240]

    # ── persistence shared by both sources ────────────────────────────────────

    def _persist(
        self,
        handle: str,
        *,
        profile: dict[str, Any] | None,
        posts: list[dict[str, Any]],
        current_tier: str,
        source: str,
    ) -> int:
        result = (
            {"new": 0, "changed": 0, "unchanged": len(posts)}
            if self.dry_run
            else store.upsert_posts(self.db, posts)
        )
        self.stats["postsNew"] += result["new"]
        self.stats["postsChanged"] += result["changed"]
        self.stats["postsUnchanged"] += result["unchanged"]

        newest_id, newest_at = store.newest_watermark(posts)
        tier = tiers.classify(newest_at)
        tier = tiers.promote_on_new_posts(tier, result["new"])

        if not self.dry_run:
            store.record_success(
                self.db,
                handle,
                profile=profile,
                newest_post_id=newest_id,
                newest_posted_at=newest_at,
                new_post_count=result["new"],
                tier=tier,
                next_fetch_at=tiers.next_fetch_at(tier),
                source=source,
            )

        self._touch(
            handle,
            status="ok",
            source=source,
            posts_new=result["new"],
            posts_changed=result["changed"],
            error=None,
        )

        log.info(
            "[%s] %s → %d new / %d changed / %d unchanged (tier=%s)",
            source,
            handle,
            result["new"],
            result["changed"],
            result["unchanged"],
            tier,
        )
        return result["new"]

    # ── source 1: Graph ───────────────────────────────────────────────────────

    async def run_graph(self, accounts: list[dict[str, Any]]) -> None:
        if not accounts:
            return

        async with InstagramGraph() as graph:
            if not graph.configured:
                log.info(
                    "[graph] not configured — Playwright-only mode "
                    "(set IG_GRAPH_ACCESS_TOKEN + IG_GRAPH_USER_ID later to use Graph)"
                )
                for account in accounts:
                    self._touch(account["handle"], status="queued_fallback", source="playwright")
                self.fallback_queue.extend(accounts)
                return

            sem = asyncio.Semaphore(settings.IG_CONCURRENCY)

            async def one(account: dict[str, Any]) -> None:
                handle = account["handle"]
                async with sem:
                    try:
                        max_pages = (
                            settings.IG_GRAPH_MEDIA_LIMIT and 1
                            if account.get("backfilled")
                            else 3
                        )
                        profile_raw, items = await graph.paginate_media(
                            handle,
                            max_pages=max_pages,
                            stop_at_id=account.get("newestPostId"),
                        )
                    except NotDiscoverable as exc:
                        log.info("[graph] %s not discoverable (%s) → fallback", handle, exc)
                        self.stats["graphMissed"] += 1
                        self._touch(
                            handle,
                            status="graph_missed",
                            source="graph",
                            error=str(exc),
                        )
                        self.fallback_queue.append(account)
                        return
                    except CircuitOpen:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[graph] %s failed: %s", handle, exc)
                        self.stats["failed"] += 1
                        self._touch(
                            handle,
                            status="failed",
                            source="graph",
                            error=str(exc),
                        )
                        if not self.dry_run:
                            store.record_failure(self.db, handle, str(exc))
                        return

                    profile = norm.profile_from_graph(profile_raw, handle=handle)
                    posts = [
                        norm.from_graph(i, handle=handle, ig_user_id=profile.get("igUserId"))
                        for i in items
                    ]
                    self.stats["graphOk"] += 1
                    self._persist(
                        handle,
                        profile=profile,
                        posts=posts,
                        current_tier=account.get("tier", "warm"),
                        source="graph",
                    )

            try:
                await asyncio.gather(*(one(a) for a in accounts))
            except CircuitOpen as exc:
                log.error("[graph] %s — stopping Graph phase", exc)

    # ── source 2: logged-out fallback ─────────────────────────────────────────

    async def run_fallback(self) -> None:
        if not self.fallback_queue:
            return
        if not settings.IG_FALLBACK_ENABLED:
            log.info(
                "[fallback] disabled — %d accounts skipped", len(self.fallback_queue)
            )
            for account in self.fallback_queue:
                self._touch(
                    account["handle"],
                    status="skipped_fallback_disabled",
                    source="playwright",
                )
            return

        from ig.playwright_fallback import Blocked, PlaywrightFallback

        queue = self.fallback_queue[: settings.IG_FALLBACK_MAX_PER_RUN]
        overflow = self.fallback_queue[settings.IG_FALLBACK_MAX_PER_RUN :]
        for account in overflow:
            self._touch(
                account["handle"],
                status="skipped_cap",
                source="playwright",
                error=f"over IG_FALLBACK_MAX_PER_RUN={settings.IG_FALLBACK_MAX_PER_RUN}",
            )

        log.info(
            "[fallback] draining %d of %d queued accounts (cap %d)",
            len(queue),
            len(self.fallback_queue),
            settings.IG_FALLBACK_MAX_PER_RUN,
        )

        stopped_reason: str | None = None
        async with PlaywrightFallback() as fb:
            for account in queue:
                handle = account["handle"]
                self._touch(handle, status="fetching", source="playwright")
                try:
                    user = await fb.fetch_profile(handle)
                except Blocked as exc:
                    log.error("[fallback] blocked (%s) — stopping for this run", exc)
                    stopped_reason = str(exc)
                    self._touch(
                        handle,
                        status="blocked",
                        source="playwright",
                        error=stopped_reason,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("[fallback] %s failed: %s", handle, exc)
                    self.stats["failed"] += 1
                    self._touch(
                        handle,
                        status="failed",
                        source="playwright",
                        error=str(exc),
                    )
                    if not self.dry_run:
                        store.record_failure(self.db, handle, str(exc))
                    continue

                if not user:
                    self.stats["failed"] += 1
                    self._touch(
                        handle,
                        status="failed",
                        source="playwright",
                        error="fallback returned no user",
                    )
                    if not self.dry_run:
                        store.record_failure(self.db, handle, "fallback returned no user")
                    continue

                profile = norm.profile_from_web(user, handle=handle)
                posts = [
                    norm.from_web_json(n, handle=handle) for n in norm.timeline_nodes(user)
                ]
                self.stats["fallbackOk"] += 1
                self._persist(
                    handle,
                    profile=profile,
                    posts=posts,
                    current_tier=account.get("tier", "warm"),
                    source="web_json",
                )

                if settings.IG_HIGHLIGHTS_ENABLED and not self.dry_run:
                    trays = [
                        {
                            "id": str((e.get("node") or {}).get("id") or ""),
                            "title": (e.get("node") or {}).get("title"),
                            "coverUrl": (
                                ((e.get("node") or {}).get("cover_media") or {}).get(
                                    "thumbnail_src"
                                )
                            ),
                            "mediaCount": (e.get("node") or {}).get("media_count"),
                        }
                        for e in norm.highlight_edges(user)
                    ]
                    store.upsert_highlights(self.db, handle, trays)

            log.info("[fallback] stats: %s", fb.stats)

        if stopped_reason:
            for account in queue:
                handle = account["handle"]
                row = self._by_handle.get(handle)
                if row and row.get("status") in ("queued", "queued_fallback", "fetching", "graph_missed"):
                    self._touch(
                        handle,
                        status="skipped_blocked",
                        source="playwright",
                        error=stopped_reason,
                    )


async def ingest(*, limit: int, tier: str | None, dry_run: bool) -> dict[str, Any]:
    db = None if dry_run else store.get_db()
    if db is not None:
        store.ensure_indexes(db)

    accounts = store.due_accounts(db, limit=limit, tier=tier) if db is not None else []
    if not accounts:
        log.info("no accounts due")
        return {"accounts": 0}

    started = datetime.now(timezone.utc)
    ing = Ingestor(db, dry_run=dry_run)
    ing.stats["accounts"] = len(accounts)
    ing._seed_accounts(accounts)

    await ing.run_graph(accounts)
    try:
        await ing.run_fallback()
    except Exception as exc:  # noqa: BLE001
        log.error("[fallback] aborted: %s", exc)
        for row in ing.account_results:
            if row.get("status") in ("queued", "queued_fallback", "fetching", "graph_missed"):
                row["status"] = "failed"
                row["error"] = str(exc)[:240]
                ing.stats["failed"] += 1

    # Leftover queue states → not processed (blocked mid-run already marked).
    for row in ing.account_results:
        if row.get("status") in ("queued", "queued_fallback", "fetching", "graph_missed"):
            row["status"] = "skipped_unprocessed"
            row["error"] = row.get("error") or "not processed this run"

    summary = {
        **ing.stats,
        "kind": "ingest",
        "accountResults": ing.account_results,
        "startedAt": started,
        "durationS": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    if db is not None:
        store.record_run(db, summary)
    log.info("run complete: %s", summary)
    return summary


def main() -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Instagram restaurant post ingest")
    parser.add_argument("--limit", type=int, default=200, help="max accounts this run")
    parser.add_argument("--tier", choices=tiers.TIER_ORDER, help="restrict to one tier")
    parser.add_argument("--dry-run", action="store_true", help="no writes")
    args = parser.parse_args()

    problems = settings.preflight()
    if problems and not args.dry_run:
        for p in problems:
            log.error("preflight: %s", p)
        raise SystemExit(1)

    asyncio.run(ingest(limit=args.limit, tier=args.tier, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
