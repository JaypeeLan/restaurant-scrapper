#!/usr/bin/env bash
# Cron build for Playwright ingest.
# Do NOT use `playwright install --with-deps` on Render — it tries `su` for
# apt packages and fails with "Authentication failure".
set -euo pipefail

pip install -r requirements.txt
playwright install chromium
