# Dashboard

Read-only inspection UI for what ingest collected. It does not start scrapes.

## Run

```bash
uvicorn serve:app --reload --port 8000
cd web && npm install && npm run dev
```

http://localhost:5173 proxies `/api` to the FastAPI process.

For a production-style split (static UI → remote API):

```bash
cd web
VITE_API_BASE=https://ig-ingest-api.onrender.com/api npm run dev
```

Production UI is intended for **Vercel** — see [deploy-vercel.md](deploy-vercel.md).
The API remains on Render.

## Views

| View | Purpose |
|---|---|
| Experiences | Caption drafts shaped like product ExperienceType |
| Posts | All stored posts |
| Accounts | Handle health, tier, failures, next fetch |
| Runs | Cadence + per-run log + Graph/fallback charts |
| Capacity | Live utilization vs Graph ceiling |

Tab state lives in the URL hash (`#posts`, `#accounts`, …).

## Status chip

The header shows Mongo connectivity from `GET /api/health`. The `db` field is
`MONGODB_DB_NAME` from `.env` (for example `validds`). It is not a product brand
string — change the env var if you want a different database.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/summary` | Headline counters |
| `GET /api/posts` | Filtered posts (`limit`/`skip` + `handle`, `q`, `since`, `until`, `source`, `media_type`, `sort`) |
| `GET /api/posts/{id}` | One post including `source.raw` |
| `GET /api/events` | Experience drafts (`limit`/`skip`; grouped by handle by default) |
| `GET /api/accounts` | Account health (`limit`/`skip`) |
| `GET /api/runs` | Runs + schedule meta (`limit`/`skip`/`kind`) |
| `GET /api/capacity` | Same projection as `main.py capacity` |
| `GET /api/health` | Mongo reachability + database name |

`GET /api/runs` includes configured cadence (`schedule`), last finished timestamps,
and observed gaps between recent ingest/discover cycles. Each row is tagged
`kind: ingest | discover`.

List endpoints strip `source.raw` (20–40 KB per post). Interactive docs: `/docs`.

## Notes

- Instagram CDN URLs expire after a few days; broken thumbnails show a placeholder.
- The Events view is heuristic (times, prices, brunch/buffet/ticket language). It is
  not full structured extraction yet.
