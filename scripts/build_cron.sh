#!/usr/bin/env bash
# Cron build for Playwright ingest.
# Do NOT use `playwright install --with-deps` on Render — it tries `su` for
# apt packages and fails with "Authentication failure".
#
# Browsers must live under the project tree. Render's ~/.cache does not reliably
# survive from build → cron runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/ms-playwright}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

pip install -r requirements.txt
playwright install chromium
