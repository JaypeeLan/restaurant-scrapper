# Architecture

## Sources

1. **Logged-out Playwright** — primary when Graph credentials are empty
   (current default). Rate-limited by Instagram; see [rate-limits.md](rate-limits.md).
2. **Instagram Graph API `business_discovery`** — optional; add later.
   When set, business/creator accounts use Graph first; Playwright remains the
   fallback for handles Graph cannot resolve. No stories.

Both sources normalize to the same post `_id` (`{handle}:{shortcode}`) so the
fallback does not double-write Graph data.

## Layout

```
config/settings.py            env config + preflight
ig/http.py                    retry, backoff, circuit breaker
ig/graph_client.py            business_discovery + sliding-window limiter
ig/playwright_fallback.py     logged-out profile scrape
pipeline/normalizer.py        source shapes → canonical post
pipeline/store.py             Mongo upserts, watermarks, backoff
pipeline/tiers.py             tier classification + capacity plan
run_ingest.py                 orchestrator
main.py                       CLI
serve.py                      read-only FastAPI
tests/                        dry-run + API tests
web/                          Vite + React dashboard
```

## Collections

| Collection | Key | Contents |
|---|---|---|
| `ig_accounts` | `handle` | watermark, tier, `nextFetchAt`, failure state |
| `ig_posts_raw` | `{handle}:{shortcode}` | normalized post + `source.raw` |
| `ig_highlights_raw` | `{handle}:{trayId}` | tray metadata only |
| `ig_runs` | auto | per-run summary |

## Ingest flow

1. Load due accounts (`nextFetchAt` <= now), capped by `--limit`
2. If Graph is configured: try Graph for each; queue misses for Playwright.
   If Graph is empty: queue every account for Playwright.
3. Normalize, content-hash, upsert; advance watermark and tier
4. Record run stats

Watermark early-stop: pagination stops when the newest known post id reappears,
so quiet accounts cost one Graph call.

`contentHash` covers caption + engagement + media type, not media URL (CDN
signatures rotate every fetch).

## Legal

Graph API use is within Meta's terms when you use your own app token.

The Playwright path scrapes instagram.com and violates Instagram's Terms of
Service. Disable it with `IG_FALLBACK_ENABLED=false` for Graph-only operation.
