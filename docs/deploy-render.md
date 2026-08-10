# Deploy on Render

Blueprint: [`render.yaml`](../render.yaml)

## What gets deployed

| Service | Type | Schedule | Command |
|---|---|---|---|
| `ig-ingest-api` | Web | always on | `uvicorn serve:app` |
| `ig-ingest-cron` | Cron | every 30 min UTC | `python main.py ingest` |
| `ig-discover-cron` | Cron | daily 03:15 UTC | `python main.py discover --backend osm` |

Dashboard static UI (`web/`) is optional — the API alone is enough; point the
Vite app at the Render URL via `VITE_API_BASE`, or build the SPA into `web/dist`
later if you want it served from the same host.

## Current mode: Playwright-first

Graph credentials are **optional**. Leave `IG_GRAPH_ACCESS_TOKEN` /
`IG_GRAPH_USER_ID` empty until you have a Meta app — ingest uses Playwright
only. When you add Graph later, business/creator handles move to Graph and
Playwright stays the fallback.

## Critical Render constraints

1. **Datacenter IP** — Instagram rate-limits Playwright hard from Render IPs.
   Prefer running ingest on a home/residential machine, or set `IG_PROXY_URL`
   to a residential proxy. Graph (when you add it later) is safer on Render.
2. **Atlas Network Access** — allow Render’s outbound IPs, or temporarily
   `0.0.0.0/0` for the cluster.
3. **Cookies are ephemeral** — do not rely on uploading `cookies.txt`.
   Paste the Netscape file into secret `IG_COOKIES_NETSCAPE` (multiline), or a
   Cookie header into `IG_COOKIES`. The app writes `cookies.txt` at runtime.
4. **Session expiry** — when discover starts 401’ing, refresh cookies in the
   Environment Group.
5. **Chromium** — Playwright needs browser binaries on the cron service
   (`playwright install chromium` in the build, or a Docker image with deps).

## Steps

1. Push this repo to GitHub/GitLab.
2. [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**
   → select the repo (`render.yaml`).
3. Create Environment Group **`ig-ingest-env`** with:

| Key | Notes |
|---|---|
| `MONGODB_URI` | Atlas SRV URI |
| `MONGODB_DB_NAME` | e.g. `validds` |
| `IG_FALLBACK_ENABLED` | `true` (Playwright primary for now) |
| `IG_GRAPH_ACCESS_TOKEN` | leave empty until Meta app is ready |
| `IG_GRAPH_USER_ID` | leave empty until Meta app is ready |
| `IG_PROXY_URL` | residential proxy if ingest runs on Render |
| `IG_COOKIES_NETSCAPE` | full Netscape cookie file text (for discover) |
| `IG_COOKIES_FILE` | `cookies.txt` |
| `DEEPSEEK_API_KEY` | optional |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` |
| `DISCOVER_CITY` | `lagos` |
| `INGEST_EVERY_MINUTES` | `30` (shown on Runs UI; match cron) |
| `INGEST_LIMIT` | `40` |
| `DISCOVER_EVERY_HOURS` | `24` |
| `LOG_LEVEL` | `INFO` |

4. Apply the Blueprint. Confirm all three services share `ig-ingest-env`.
5. Trigger **ig-discover-cron** once from the dashboard (“Trigger Run”).
6. Hit `https://<api>.onrender.com/api/health` — expect `{"ok": true, ...}`.

## Local vs Render

| Concern | Local | Render |
|---|---|---|
| Post scrape | Playwright (Graph optional later) | Playwright + proxy, or Graph later |
| Handle discover | `cookies.txt` file | `IG_COOKIES_NETSCAPE` secret |
| Scheduler | `python main.py schedule` | Cron services |
| Mongo | Atlas + your home IP allowlisted | Atlas + Render IPs / `0.0.0.0/0` |

## Costs

Starter web + 2 crons bill separately (crons prorated by runtime). Keep
`INGEST_LIMIT` / `DISCOVER_RESOLVE_LIMIT` modest so each cron finishes quickly.
