#!/usr/bin/env bash
# Build the API image + Vite dashboard for Render's native Python runtime.
set -euo pipefail

pip install -r requirements.txt

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
