# Automated venue discovery

No manual `handles.txt` editing required for growth.

## Flow

1. **Find places** — OpenStreetMap (free) or Google Places (if API key set)
2. **Resolve Instagram** — logged-in topsearch for each place name
3. **Seed** — confident handles → `ig_accounts`
4. **Ingest** — existing Graph / logged-out post scrape

## One-shot

```bash
# Preview OSM places only (no IG calls)
python main.py discover --city lagos --dry-run

# Find places + resolve up to 40 handles + seed accounts
python main.py discover --city lagos --resolve-limit 40
```

Uses `cookies.txt` for Instagram search. Caps IG lookups per run so you don’t
burn the session (`DISCOVER_RESOLVE_LIMIT`, default 40). Re-run later to drain
the pending queue.

## Cron / long-running

```bash
python main.py schedule --every 30 --discover-every 24 --discover-city lagos
```

- Ingest due accounts every 30 minutes  
- Discover new places→handles once per day (and once at startup)

## Backends

| `PLACES_BACKEND` | Source | Needs |
|---|---|---|
| `auto` (default) | Google if key set, else OSM | — |
| `osm` | OpenStreetMap Overpass | network |
| `google` | Places API (New) text search | `GOOGLE_PLACES_API_KEY` |

OSM coverage in Lagos is decent but incomplete. Google is better for “what’s
open now” style lists if you pay for Places.

## Mongo

- `places_raw` — discovered venues + `handleStatus` (`pending` / `resolved` / …)
- `ig_handle_candidates` — search hits
- `ig_accounts` — seeded for ingest
