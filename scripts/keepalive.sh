#!/usr/bin/env bash
# Ping the API so the free Render web service stays warm.
set -euo pipefail

URL="${KEEPALIVE_URL:-https://ig-ingest-api.onrender.com/api/health}"
curl -fsS --max-time 90 "$URL" >/dev/null
echo "keepalive ok: $URL"
