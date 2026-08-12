# Setup

## Requirements

- Python 3.11+
- MongoDB (local or Atlas URI in `MONGODB_URI`)
- Node 18+ (dashboard only)
- Optional: Meta app for Graph API

## Environment

```bash
cp .env.example .env
```

Important variables:

| Variable | Purpose |
|---|---|
| `MONGODB_URI` | Mongo connection string |
| `MONGODB_DB_NAME` | Database name (shown by `/api/health` as `db`) |
| `IG_GRAPH_ACCESS_TOKEN` | Optional long-lived Page token (add later) |
| `IG_GRAPH_USER_ID` | Optional IG Business account numeric id |
| `IG_FALLBACK_ENABLED` | Playwright scrape (primary when Graph is empty) |

Leave Graph empty to run Playwright-only. When you add Graph later, business
handles move onto it automatically and Playwright stays the fallback.

## Graph API credentials (optional — later)

Skip this section while running Playwright-only. When you are ready:

1. Create a Business-type app at https://developers.facebook.com
2. Add the Instagram Graph API product
3. Convert an Instagram account you own to Business or Creator, link it to a Facebook Page
4. Grant `instagram_basic`, `pages_show_list`, `pages_read_engagement`
5. Exchange the short-lived user token for a long-lived token (~60 days):

```
GET https://graph.facebook.com/v21.0/oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id={app-id}
  &client_secret={app-secret}
  &fb_exchange_token={short-lived-token}
```

6. Resolve your IG user id:

```
GET /me/accounts
GET /{page_id}?fields=instagram_business_account
```

Put the long-lived token in `IG_GRAPH_ACCESS_TOKEN` and the IG id in
`IG_GRAPH_USER_ID`. Refresh the token before it expires.

You query as your own account about other public business/creator accounts.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-ingest.txt
playwright install chromium   # required for Playwright-only ingest
```

## Run

```bash
python main.py preflight
python main.py seed --file handles.txt
python main.py capacity
python main.py ingest --limit 200
python main.py schedule --every 30
```

Offline checks (no network/creds):

```bash
pip install -r requirements-dev.txt
python -m tests.test_dryrun
python -m tests.test_api
```

## Dashboard

```bash
uvicorn serve:app --reload --port 8000
cd web && npm install && npm run dev
```

Open http://localhost:5173. Vite proxies `/api` to port 8000.
