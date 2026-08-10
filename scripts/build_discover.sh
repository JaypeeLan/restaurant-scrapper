#!/usr/bin/env bash
# Discover cron only needs httpx + mongo (no Chromium).
set -euo pipefail
pip install -r requirements.txt
