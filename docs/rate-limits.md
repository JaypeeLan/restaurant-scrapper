# Rate limits

Two independent ceilings. Graph is enforced in code. Fallback is enforced by
Instagram against your IP — the knobs below only slow you down enough to stay
under the empirical limit.

## Graph API (Meta)

| Item | Value | Source |
|---|---|---|
| Meta hard ceiling | **200 calls / hour / user** | Meta docs; errors `4` / `17` if exceeded |
| Our limiter capacity | **180 calls / hour** | `IG_GRAPH_CALLS_PER_HOUR` (default 180) |
| Window | **3600 seconds sliding** | `RateLimiter` in `ig/graph_client.py` |
| Cost per account | **1+ calls** | One `business_discovery` call plus pagination if needed |
| Daily budget at 180/hr | **4320 calls / day** | `180 * 24` |
| Media page size | **25 posts** | `IG_GRAPH_MEDIA_LIMIT` |

Behaviour when the window is full: the limiter **blocks and waits** until the
oldest call ages out of the 1-hour window (+0.25s). It does not drop work.

Hitting Meta's real 200/hr ceiling returns error 4/17 and locks the remainder of
the window — that is why the code stays at 180.

## Playwright fallback (Instagram HTML)

Not a documented API quota. Observed from one residential IP, logged-out:

| Item | Value | Source |
|---|---|---|
| Soft ceiling | **~100–150 profile fetches / hour** | Empirical; above this, interstitials |
| Max accounts per run | **40** | `IG_FALLBACK_MAX_PER_RUN` |
| Gap between fetches | **20–55 seconds** random | `IG_FALLBACK_MIN_GAP_S` / `IG_FALLBACK_MAX_GAP_S` |
| Browser concurrency | **2** | Hardcoded `_MAX_CONCURRENCY` in `ig/playwright_fallback.py` |
| Abandon run after | **3 consecutive blocks** | Interstitial / HTTP 429 |
| Circuit breaker (pipeline) | **12 consecutive failures** | `IG_CIRCUIT_THRESHOLD` |

Rough throughput at default gaps: about **1 account / 20–55s**, so a full run of
40 accounts takes roughly **15–35 minutes**, then you should stop and wait for
the next cycle rather than looping.

Datacenter IPs (Render, Fly, AWS, etc.) get blocked much faster than residential
ones. Empty `IG_PROXY_URL` means your own machine's IP.

## Graph pacing (httpx path)

| Item | Value |
|---|---|
| Concurrent Graph workers | `IG_CONCURRENCY` default **4** |
| Delay between Graph requests | `IG_MIN_DELAY_MS`–`IG_MAX_DELAY_MS` default **200–700 ms** |

These sit under the 180/hr sliding window; they do not replace it.

## Tier refresh cadence

How often an account becomes due again (hours between fetches):

| Tier | Default hours | Env |
|---|---|---|
| hot | 12 | `IG_TIER_HOT_HOURS` |
| warm | 24 | `IG_TIER_WARM_HOURS` |
| cold | 96 | `IG_TIER_COLD_HOURS` |
| dormant | 336 (2 weeks) | `IG_TIER_DORMANT_HOURS` |

Check projected Graph demand against the ceiling:

```bash
python main.py capacity
```

## What is unavailable for free

| Content | Graph | Logged-out fallback |
|---|---|---|
| Grid posts | yes (business/creator) | yes (public) |
| Live stories | no | no |
| Highlight slides | no | no |
| Highlight tray titles | no | yes (titles/covers only) |

Stories-only event announcements cannot be collected on the free path.
