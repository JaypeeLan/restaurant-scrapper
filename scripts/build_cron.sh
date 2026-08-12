#!/usr/bin/env bash
# Cron build for Playwright ingest (+ optional flyer OCR).
# Do NOT use `playwright install --with-deps` or `apt-get` on Render native
# runtimes — both fail with permission / "Authentication failure".
# System tesseract is not available here; OCR at ingest no-ops and titles can
# be filled via `python main.py backfill-ocr` wherever tesseract is installed.
#
# Browsers must live under the project tree. Render's ~/.cache does not reliably
# survive from build → cron runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/ms-playwright}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

if command -v tesseract >/dev/null 2>&1; then
  tesseract --version 2>/dev/null | head -1 || true
else
  echo "NOTE: tesseract not on PATH — ingest will skip flyer OCR (use backfill-ocr locally)"
fi

export PIP_NO_CACHE_DIR=1
pip install -r requirements-ingest.txt
playwright install chromium
