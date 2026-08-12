"""
CLI entrypoint.

    python main.py preflight
    python main.py seed --file handles.txt
    python main.py discover --city lagos
    python main.py capacity
    python main.py ingest --limit 200
    python main.py schedule --every 30 --discover-every 4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from config import settings
from pipeline import store, tiers

log = logging.getLogger("ig.main")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


def cmd_preflight(_: argparse.Namespace) -> int:
    problems = settings.preflight()
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print("  ✓ config OK")
    try:
        db = store.get_db()
        db.command("ping")
        store.ensure_indexes(db)
        total = db[settings.COL_ACCOUNTS].count_documents({})
        due = db[settings.COL_ACCOUNTS].count_documents(
            {
                "nextFetchAt": {
                    "$lte": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    )
                }
            }
        )
        print(f"  ✓ mongo reachable — {total} accounts, {due} due now")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ mongo: {exc}")
        return 1

    print(
        "  ✓ graph configured (primary)"
        if settings.IG_GRAPH_ACCESS_TOKEN
        else "  · graph not set — Playwright-only (optional later)"
    )
    print(
        "  ✓ playwright enabled"
        if settings.IG_FALLBACK_ENABLED
        else "  ✗ playwright disabled (IG_FALLBACK_ENABLED=false)"
    )
    from ig import logged_in_search

    print(
        "  ✓ logged-in search configured (handle discovery)"
        if logged_in_search.session_configured()
        else "  ! logged-in search not configured — cookies.txt / IG_COOKIES"
    )
    print(
        "  ✓ google places configured"
        if settings.GOOGLE_PLACES_API_KEY
        else "  ! google places not set — discover uses free OSM Overpass"
    )
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    handles = []
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            handles = [
                line.strip()
                for line in fh
                if line.strip() and not line.startswith("#")
            ]
    elif args.handles:
        handles = args.handles
    else:
        print("provide --file or --handles")
        return 1

    db = store.get_db()
    store.ensure_indexes(db)
    added = store.upsert_accounts(db, handles)
    print(f"seeded {len(handles)} handles ({added} new)")
    return 0


def cmd_capacity(_: argparse.Namespace) -> int:
    db = store.get_db()
    counts = {
        t: db[settings.COL_ACCOUNTS].count_documents({"tier": t}) for t in tiers.TIER_ORDER
    }
    plan = tiers.plan_capacity(counts)

    print(f"  accounts by tier: {counts}")
    print(f"  daily demand:     {plan['dailyDemand']} calls")
    print(
        f"  daily capacity:   {plan['dailyCapacity']} calls "
        f"({settings.IG_GRAPH_CALLS_PER_HOUR}/hr)"
    )
    print(f"  utilization:      {plan['utilization']}")
    print("  " + ("✓ within budget" if plan["withinBudget"] else "✗ OVER BUDGET"))
    if not plan["withinBudget"]:
        print("    → raise tier intervals in settings, or split across a second IG app")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from run_ingest import ingest

    summary = asyncio.run(ingest(limit=args.limit, tier=args.tier, dry_run=args.dry_run))
    if args.dry_run or not settings.DEEPSEEK_ENABLED:
        return 0
    from pipeline import deepseek_extract

    db = store.get_db()
    stats = deepseek_extract.backfill_llm(
        db,
        limit=settings.LLM_BACKFILL_LIMIT,
        dry_run=False,
    )
    log.info("post-ingest llm backfill: %s", stats)
    store.record_run(
        db,
        {
            "kind": "llm_backfill",
            "parentKind": "ingest",
            **stats,
        },
    )
    return 0


def cmd_backfill_ocr(args: argparse.Namespace) -> int:
    """Persist flyer OCR titles onto existing posts (flyer-first naming)."""
    from pipeline import ocr as ocr_mod

    db = store.get_db()
    store.ensure_indexes(db)
    stats = ocr_mod.backfill_ocr(
        db,
        limit=args.limit,
        handle=args.handle,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(
        f"ocr backfill scanned={stats['scanned']} updated={stats['updated']} "
        f"ok={stats['ok']} empty={stats['empty']} error={stats['error']}"
        + (" (dry-run)" if args.dry_run else "")
    )
    store.record_run(
        db,
        {
            "kind": "ocr_backfill",
            "dryRun": bool(args.dry_run),
            **stats,
        },
    )
    return 0 if stats.get("error", 0) == 0 or stats.get("ok", 0) > 0 else 1


def cmd_backfill_llm(args: argparse.Namespace) -> int:
    """Live DeepSeek refine on experience posts (background only)."""
    from pipeline import deepseek_extract

    db = store.get_db()
    store.ensure_indexes(db)
    stats = deepseek_extract.backfill_llm(
        db,
        limit=args.limit,
        handle=args.handle,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(
        f"llm backfill scanned={stats['scanned']} updated={stats['updated']} "
        f"ok={stats['ok']} skipped={stats['skipped']} "
        f"notExperience={stats['notExperience']} error={stats['error']}"
        + (" (dry-run)" if args.dry_run else "")
        + (" (disabled)" if stats.get("disabled") else "")
    )
    store.record_run(
        db,
        {
            "kind": "llm_backfill",
            "dryRun": bool(args.dry_run),
            **stats,
        },
    )
    if stats.get("disabled"):
        return 0
    return 0 if stats.get("error", 0) == 0 or stats.get("ok", 0) > 0 else 1


def cmd_backfill_menus(args: argparse.Namespace) -> int:
    """Fetch highlight slides (logged-in) → OCR → MenuType drafts."""
    from datetime import timedelta

    from ig import logged_in_search
    from pipeline import menu_extract

    from_stored = bool(getattr(args, "from_stored", False))
    if not from_stored and not logged_in_search.session_configured():
        print("  ✗ cookies required (cookies.txt / IG_COOKIES)")
        return 1

    db = store.get_db()
    store.ensure_indexes(db)

    every_days = max(1, int(getattr(args, "every_days", None) or settings.MENU_EVERY_DAYS))
    stale_before = datetime.now(timezone.utc) - timedelta(days=every_days)

    query: dict = {
        "title": {
            "$regex": (
                r"menu|food|drink|beverage|wine|cocktail|kitchen|brunch|"
                r"pastr(?:y|ies)|takeaway|bar"
            ),
            "$options": "i",
        }
    }
    if args.handle:
        query["handle"] = args.handle.strip().lstrip("@").lower()
    if args.tray:
        query["trayId"] = str(args.tray).strip()
    if from_stored:
        query["slides.0.ocrText"] = {"$exists": True}
    elif not args.force:
        # Missing, failed, or older than weekly cadence.
        query["$or"] = [
            {"menuExtractedAt": {"$exists": False}},
            {"menuStatus": {"$in": ["empty", "error"]}},
            {"menuItemCount": {"$lte": 0}},
            {"menuExtractedAt": {"$lt": stale_before}},
        ]

    limit = max(1, int(args.limit if args.limit is not None else settings.MENU_BACKFILL_LIMIT))
    trays = list(
        db[settings.COL_HIGHLIGHTS]
        .find(query)
        .sort([("menuExtractedAt", 1), ("handle", 1), ("title", 1)])
        .limit(limit)
    )
    if not trays:
        print("no menu highlights to process")
        return 0

    print(
        f"menus backfill candidates={len(trays)} "
        f"(stale>{every_days}d or missing; limit={limit})"
    )

    ok = 0
    empty = 0
    errors = 0
    for tray in trays:
        handle = tray.get("handle") or ""
        tray_id = str(tray.get("trayId") or "")
        title = tray.get("title") or ""
        try:
            result = menu_extract.extract_highlight_menu(
                db,
                handle=handle,
                tray_id=tray_id,
                max_slides=args.max_slides,
                force=True,  # selected as due; always refresh this tray
                from_stored=from_stored,
                max_age_days=every_days,
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  ✗ @{handle} {title!r}: {exc}")
            continue
        n = int(result.get("itemCount") or 0)
        if result.get("skipped"):
            # Should be rare when force=True.
            print(
                f"  skip @{handle} {title!r} slides={result.get('slideCount')} "
                f"items={n}"
            )
            continue
        if n:
            ok += 1
        else:
            empty += 1
        print(
            f"  ok @{handle} {title!r} slides={result.get('slideCount')} "
            f"items={n}"
        )
        if args.show and result.get("items"):
            for item in result["items"][:12]:
                price = item.get("price") or 0
                print(
                    f"      - {item.get('itemName')}  "
                    f"{price if price else '—'}  [{item.get('type')}/{item.get('category')}] "
                    f"§{item.get('section')}"
                )

    print(f"menus backfill trays={len(trays)} ok={ok} empty={empty} error={errors}")
    store.record_run(
        db,
        {
            "kind": "menu_backfill",
            "trays": len(trays),
            "ok": ok,
            "empty": empty,
            "error": errors,
            "fromStored": from_stored,
            "everyDays": every_days,
        },
    )
    return 0 if errors == 0 else 1


def cmd_backfill_web_menus(args: argparse.Namespace) -> int:
    """Discover menus from Linktree / restaurant websites → MenuType drafts."""
    from pipeline import web_menu

    db = store.get_db()
    stats = web_menu.backfill_web_menus(
        db,
        limit=args.limit,
        handle=args.handle,
        force=args.force,
        dry_run=args.dry_run,
        every_days=args.every_days,
    )
    print(
        f"web menus accounts={stats['accounts']} sources={stats['sources']} "
        f"updated={stats['updated']} ok={stats['ok']} empty={stats['empty']} "
        f"error={stats['error']} skipped={stats['skipped']}"
        + (" (dry-run)" if args.dry_run else "")
    )
    store.record_run(
        db,
        {
            "kind": "web_menu_backfill",
            "dryRun": bool(args.dry_run),
            **stats,
        },
    )
    return 0 if stats.get("error", 0) == 0 or stats.get("ok", 0) > 0 else 1


def cmd_search_handles(args: argparse.Namespace) -> int:
    """Logged-in topsearch → candidate handles (optionally seed ig_accounts)."""
    from ig import logged_in_search

    if not logged_in_search.session_configured():
        print("  ✗ cookies required (cookies.txt or IG_COOKIES)")
        return 1

    queries: list[str] = []
    if args.query:
        queries.append(args.query)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            queries.extend(
                line.strip()
                for line in fh
                if line.strip() and not line.startswith("#")
            )
    if not queries:
        print("provide --query or --file")
        return 1

    try:
        if args.all_hits:
            import time

            hits: list[dict] = []
            for q in queries:
                hits.extend(logged_in_search.topsearch_users(q, limit=args.limit))
                if len(queries) > 1:
                    time.sleep(settings.IG_SEARCH_GAP_S)
        else:
            hits = logged_in_search.search_many(queries, min_score=args.min_score)
    except logged_in_search.LoggedInAuthError as exc:
        print(f"  ✗ {exc}")
        return 1

    if not hits:
        print("no confident handles")
        return 0

    for h in hits:
        print(
            f"  @{h['handle']:<24} score={h.get('score', 0):.2f}  "
            f"{(h.get('fullName') or '')[:40]}  ← {h.get('query')}"
        )

    db = store.get_db()
    store.ensure_indexes(db)
    upserted = store.upsert_handle_candidates(db, hits)
    print(f"stored {len(hits)} candidates ({upserted} new)")

    if args.seed:
        handles = [h["handle"] for h in hits]
        added = store.upsert_accounts(db, handles)
        store.mark_candidates_seeded(db, handles)
        print(f"seeded {len(handles)} into ig_accounts ({added} new)")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Places → Instagram handles → seed accounts (no manual venue list)."""
    from discover.run_discover import run_discover
    from ig import logged_in_search

    if not args.dry_run and not logged_in_search.session_configured():
        print("  ✗ logged-in cookies required (cookies.txt) to resolve handles")
        return 1

    discover_ok = True
    try:
        summary = run_discover(
            city=args.city,
            limit_places=args.place_limit,
            resolve_limit=args.resolve_limit,
            min_score=args.min_score,
            seed=not args.no_seed,
            backend=args.backend,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {exc}")
        discover_ok = False
        if not (args.ingest_after and not args.dry_run):
            return 1
        log.warning("discover failed; still running post-discover ingest")
    else:
        print(f"city:            {summary.get('city')}")
        print(f"places found:    {summary.get('placesFound')}")
        if summary.get("dryRun"):
            print("dry-run sample:")
            for row in summary.get("sample") or []:
                print(f"  - {row.get('name')}  ig={row.get('ig')}")
            return 0
        print(f"places upserted: {summary.get('placesUpserted')}")
        print(f"resolve tried:   {summary.get('resolveAttempted')}")
        print(f"resolved:        {summary.get('resolved')}")
        print(f"skipped:         {summary.get('skipped')}")
        print(f"seeded:          {summary.get('seeded')}")
        for h in summary.get("handles") or []:
            print(f"  @{h}")

    if args.ingest_after and not args.dry_run:
        from run_ingest import ingest

        log.info("post-discover ingest starting")
        try:
            asyncio.run(ingest(limit=args.ingest_limit, tier=None, dry_run=False))
        except Exception as exc:  # noqa: BLE001
            log.error("post-discover ingest failed: %s", exc)
            return 1
    return 0 if discover_ok else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from discover.run_discover import run_discover
    from run_ingest import ingest

    scheduler = BlockingScheduler(timezone="UTC")

    def ingest_job() -> None:
        asyncio.run(ingest(limit=args.limit, tier=None, dry_run=False))

    def discover_job() -> None:
        try:
            summary = run_discover(
                city=args.discover_city,
                limit_places=args.place_limit,
                resolve_limit=args.resolve_limit,
                seed=True,
            )
            log.info("discover job done: %s", summary)
        except Exception as exc:  # noqa: BLE001
            log.error("discover job failed: %s", exc)
        try:
            log.info("post-discover ingest starting")
            asyncio.run(ingest(limit=args.limit, tier=None, dry_run=False))
        except Exception as exc:  # noqa: BLE001
            log.error("post-discover ingest failed: %s", exc)

    def menu_job() -> None:
        try:
            rc = cmd_backfill_menus(
                argparse.Namespace(
                    limit=args.menu_limit,
                    handle=None,
                    tray=None,
                    max_slides=24,
                    force=False,
                    from_stored=False,
                    show=False,
                    every_days=args.menu_every_days,
                )
            )
            log.info("menu job done rc=%s", rc)
        except Exception as exc:  # noqa: BLE001
            log.error("menu job failed: %s", exc)

    scheduler.add_job(
        ingest_job, "interval", minutes=args.every, max_instances=1, coalesce=True
    )
    if args.discover_every > 0:
        scheduler.add_job(
            discover_job,
            "interval",
            hours=args.discover_every,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(discover_job, "date")
    if args.menu_every_days > 0:
        scheduler.add_job(
            menu_job,
            "interval",
            days=args.menu_every_days,
            max_instances=1,
            coalesce=True,
        )

    log.info(
        "scheduler started — ingest every %d min (limit %d); discover every %s h "
        "(+ ingest after); menus every %s d (limit %d)",
        args.every,
        args.limit,
        args.discover_every if args.discover_every > 0 else "off",
        args.menu_every_days if args.menu_every_days > 0 else "off",
        args.menu_limit,
    )
    scheduler.start()
    return 0


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="ig", description="Instagram restaurant ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="check config and connectivity").set_defaults(
        func=cmd_preflight
    )

    p_seed = sub.add_parser("seed", help="add handles to the account list")
    p_seed.add_argument("--file", help="newline-delimited handles")
    p_seed.add_argument("--handles", nargs="*", help="handles inline")
    p_seed.set_defaults(func=cmd_seed)

    p_search = sub.add_parser(
        "search-handles",
        help="logged-in Instagram topsearch → venue handles",
    )
    p_search.add_argument("--query", help="single search string, e.g. 'shiro lagos'")
    p_search.add_argument("--file", help="newline-delimited venue queries")
    p_search.add_argument("--min-score", type=float, default=None)
    p_search.add_argument("--all-hits", action="store_true")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--seed", action="store_true")
    p_search.set_defaults(func=cmd_search_handles)

    p_disc = sub.add_parser(
        "discover",
        help="auto: Places (OSM/Google) → IG handles → seed accounts",
    )
    p_disc.add_argument("--city", default=settings.DISCOVER_CITY, help="lagos | abuja")
    p_disc.add_argument("--place-limit", type=int, default=settings.DISCOVER_PLACE_LIMIT)
    p_disc.add_argument(
        "--resolve-limit",
        type=int,
        default=settings.DISCOVER_RESOLVE_LIMIT,
        help="max IG searches this run",
    )
    p_disc.add_argument("--backend", choices=["auto", "osm", "google"], default="auto")
    p_disc.add_argument("--min-score", type=float, default=None)
    p_disc.add_argument("--no-seed", action="store_true")
    p_disc.add_argument("--dry-run", action="store_true")
    p_disc.add_argument(
        "--ingest-after",
        action="store_true",
        help="run one ingest cycle after seeding (picks up new handles immediately)",
    )
    p_disc.add_argument(
        "--ingest-limit",
        type=int,
        default=settings.INGEST_LIMIT,
        help="account cap for --ingest-after",
    )
    p_disc.set_defaults(func=cmd_discover)

    sub.add_parser("capacity", help="project daily calls vs rate ceiling").set_defaults(
        func=cmd_capacity
    )

    p_ing = sub.add_parser("ingest", help="run one ingest cycle")
    p_ing.add_argument("--limit", type=int, default=settings.INGEST_LIMIT)
    p_ing.add_argument("--tier", choices=tiers.TIER_ORDER)
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.set_defaults(func=cmd_ingest)

    p_ocr = sub.add_parser(
        "backfill-ocr",
        help="OCR flyer images onto existing posts (stores ocrTitle for Experiences)",
    )
    p_ocr.add_argument("--limit", type=int, default=100)
    p_ocr.add_argument("--handle", help="only this @handle")
    p_ocr.add_argument(
        "--force",
        action="store_true",
        help="re-OCR even when ocrAt already set",
    )
    p_ocr.add_argument("--dry-run", action="store_true")
    p_ocr.set_defaults(func=cmd_backfill_ocr)

    p_llm = sub.add_parser(
        "backfill-llm",
        help="DeepSeek refine experience posts (stores llmName/llmExtract)",
    )
    p_llm.add_argument(
        "--limit",
        type=int,
        default=settings.LLM_BACKFILL_LIMIT,
        help="max live API calls this run",
    )
    p_llm.add_argument("--handle", help="only this @handle")
    p_llm.add_argument(
        "--force",
        action="store_true",
        help="re-refine even when llmName already set",
    )
    p_llm.add_argument("--dry-run", action="store_true")
    p_llm.set_defaults(func=cmd_backfill_llm)

    p_menu = sub.add_parser(
        "backfill-menus",
        help="logged-in highlight slides → OCR → MenuType drafts",
    )
    p_menu.add_argument(
        "--limit",
        type=int,
        default=settings.MENU_BACKFILL_LIMIT,
        help="max trays this run",
    )
    p_menu.add_argument("--handle", help="only this @handle")
    p_menu.add_argument("--tray", help="only this highlight tray id")
    p_menu.add_argument("--max-slides", type=int, default=24)
    p_menu.add_argument(
        "--every-days",
        type=int,
        default=settings.MENU_EVERY_DAYS,
        help="re-extract trays older than this many days (default weekly)",
    )
    p_menu.add_argument(
        "--force",
        action="store_true",
        help="re-extract even fresh trays (ignore weekly age gate)",
    )
    p_menu.add_argument(
        "--from-stored",
        action="store_true",
        help="reuse stored slide OCR; only re-run DeepSeek (no IG refetch)",
    )
    p_menu.add_argument("--show", action="store_true", help="print sample items")
    p_menu.set_defaults(func=cmd_backfill_menus)

    p_web = sub.add_parser(
        "backfill-web-menus",
        help="Linktree / website PDFs & menu pages → MenuType drafts",
    )
    p_web.add_argument(
        "--limit",
        type=int,
        default=settings.WEB_MENU_BACKFILL_LIMIT,
        help="max menu sources this run",
    )
    p_web.add_argument("--handle", help="only this @handle")
    p_web.add_argument(
        "--every-days",
        type=int,
        default=settings.WEB_MENU_EVERY_DAYS,
        help="re-fetch sources older than this many days",
    )
    p_web.add_argument("--force", action="store_true")
    p_web.add_argument("--dry-run", action="store_true")
    p_web.set_defaults(func=cmd_backfill_web_menus)

    p_sch = sub.add_parser("schedule", help="run continuously")
    p_sch.add_argument(
        "--every",
        type=int,
        default=settings.INGEST_EVERY_MINUTES,
        help="minutes between ingest",
    )
    p_sch.add_argument("--limit", type=int, default=settings.INGEST_LIMIT)
    p_sch.add_argument(
        "--discover-every",
        type=int,
        default=settings.DISCOVER_EVERY_HOURS,
        help="hours between place→handle discovery (0=off)",
    )
    p_sch.add_argument("--discover-city", default=settings.DISCOVER_CITY)
    p_sch.add_argument("--place-limit", type=int, default=settings.DISCOVER_PLACE_LIMIT)
    p_sch.add_argument("--resolve-limit", type=int, default=settings.DISCOVER_RESOLVE_LIMIT)
    p_sch.add_argument(
        "--menu-every-days",
        type=int,
        default=settings.MENU_EVERY_DAYS,
        help="days between menu highlight backfill (0=off)",
    )
    p_sch.add_argument(
        "--menu-limit",
        type=int,
        default=settings.MENU_BACKFILL_LIMIT,
        help="max menu trays per scheduled backfill",
    )
    p_sch.set_defaults(func=cmd_schedule)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
