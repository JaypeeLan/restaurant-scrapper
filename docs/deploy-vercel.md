# Deploy the dashboard on Vercel

The Vite UI lives in [`web/`](../web/). Host it on Vercel so the SPA does not
wait on Render’s free-tier wake screen. The API stays on Render
(`https://ig-ingest-api.onrender.com`).

## One-time setup

1. Import the GitHub repo in [Vercel](https://vercel.com/new).
2. Set **Root Directory** to `web`.
3. Framework preset: Vite (or leave defaults — `web/vercel.json` is enough).
4. Add environment variable:

| Name | Value |
|---|---|
| `VITE_API_BASE` | `https://ig-ingest-api.onrender.com/api` |

5. Deploy. Your site will look like `https://<project>.vercel.app`.

Preview deploys work the same way — the API already allows `*.vercel.app`
origins via CORS.

## Local check against production API

```bash
cd web
VITE_API_BASE=https://ig-ingest-api.onrender.com/api npm run dev
```

## Keep the API awake

Render free web sleeps after ~15 minutes idle. Blueprint service
`ig-keepalive-cron` hits `/api/health` every 10 minutes so the first dashboard
API call after idle is less likely to show “APPLICATION LOADING”.

If you skip that cron, the Vercel UI still loads instantly — only data fetch
waits while Render boots.

## Optional: stop serving the SPA from Render

You can leave the API build as-is (static files on Render are a harmless
fallback). To skip the Node install on the API service later, change
`scripts/build_api.sh` to only `pip install -r requirements.txt`.
