#!/usr/bin/env bash
# Cron build for Playwright ingest + flyer OCR.
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

# System tesseract for flyer OCR at ingest (apt works as root on Render builds).
if ! command -v tesseract >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq tesseract-ocr
  else
    echo "WARNING: tesseract not installed; flyer OCR will be empty until available"
  fi
fi
tesseract --version 2>/dev/null | head -1 || true

export PIP_NO_CACHE_DIR=1
pip install -r requirements-ingest.txt
playwright install chromium
