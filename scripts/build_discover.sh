#!/usr/bin/env bash
# Discover cron now chains ingest (--ingest-after), so Chromium is required.
# Kept as a thin wrapper around the ingest cron build.
set -euo pipefail
exec bash "$(dirname "$0")/build_cron.sh"
