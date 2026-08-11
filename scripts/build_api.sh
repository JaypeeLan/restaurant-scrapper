#!/usr/bin/env bash
# Build the API for Render's native Python runtime.
#
# The dashboard lives on Vercel now — do not install Node / build Vite here
# unless BUILD_WEB=1 (keeps free-tier deploys fast and less failure-prone).
set -euo pipefail

pip install -r requirements.txt

if [[ "${BUILD_WEB:-0}" == "1" ]]; then
  NODE_VERSION="${NODE_VERSION:-20.18.1}"
  NODE_DIR="${HOME}/.cache/node-v${NODE_VERSION}-linux-x64"
  if [[ ! -x "${NODE_DIR}/bin/node" ]]; then
    mkdir -p "${HOME}/.cache"
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
      | tar -xJ -C "${HOME}/.cache"
  fi
  export PATH="${NODE_DIR}/bin:${PATH}"

  (
    cd web
    npm ci
    npm run build
  )
else
  echo "skipping web SPA build (hosted on Vercel); set BUILD_WEB=1 to enable"
fi
