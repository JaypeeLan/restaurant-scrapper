# Instagram ingest

Pulls Instagram posts from Nigerian venues and hosts into MongoDB for a later
event-extraction pass. This repo is the ingest layer only — it does not parse
events, menus, or dress codes yet.

## Docs

| Doc | Contents |
|---|---|
| [docs/setup.md](docs/setup.md) | Install, Graph API credentials, run commands |
| [docs/rate-limits.md](docs/rate-limits.md) | Exact ceilings per window |
| [docs/dashboard.md](docs/dashboard.md) | Read-only UI and API |
| [docs/experiences.md](docs/experiences.md) | ExperienceType field coverage from IG |
| [docs/handle-discovery.md](docs/handle-discovery.md) | Auto Places → IG handles → seed |
| [docs/deploy-render.md](docs/deploy-render.md) | Render API + cron blueprint |
| [docs/deploy-vercel.md](docs/deploy-vercel.md) | Dashboard on Vercel (recommended) |

## Quick start

```bash
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-ingest.txt
playwright install chromium

python main.py preflight
python main.py seed --file handles.txt
python main.py ingest --limit 20

uvicorn serve:app --reload --port 8000
cd web && npm install && npm run dev   # http://localhost:5173
```

`MONGODB_DB_NAME` in `.env` is the database the UI/API connect to. If the header
shows a name you do not recognise, check that variable.

## What this stores

- `ig_posts_raw` — captions, media URLs, timestamps, full upstream payload
- `ig_accounts` — handle health, tier, next fetch time
- `ig_highlights_raw` — highlight tray titles only (not slides)
- `ig_runs` — per-run counters

Event objects are not produced here.
