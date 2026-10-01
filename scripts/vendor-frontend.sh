#!/usr/bin/env bash
# Download the pinned frontend libraries into frontend/vendor/ (verified by
# SHA-256) and rebuild the Tailwind utility CSS from the class names used in
# frontend/index.html and frontend/js/. Everything the UI needs is then
# served by the app itself — no CDN, works offline.
#
# Run after changing classes in the frontend or bumping a version below:
#   scripts/vendor-frontend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VENDOR=frontend/vendor
mkdir -p "$VENDOR"

ALPINE_VERSION=3.17.4
CHARTJS_VERSION=4.4.0
DAISYUI_VERSION=4.12.10
TAILWIND_VERSION=3.4.17

fetch() {  # url target sha256
  curl -fsSL "$1" -o "$2.tmp"
  echo "$3  $2.tmp" | shasum -a 256 -c --quiet -
  mv "$2.tmp" "$2"
}

# ES module build: frontend/js/main.js imports it and starts Alpine.
fetch "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINE_VERSION}/dist/module.esm.min.js" \
  "$VENDOR/alpine.esm.min.js" b8f2b2c60e9409c9b37b70843fd91a45abf5e48beb9a79bef5757b9fcb3bf41b
fetch "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js" \
  "$VENDOR/chart.umd.min.js" 0e2326c6868072bec1592760c6729043caeea2960a2b46cee6a2192aac6abff0
fetch "https://cdn.jsdelivr.net/npm/daisyui@${DAISYUI_VERSION}/dist/full.min.css" \
  "$VENDOR/daisyui.full.min.css" 3c5948b1ebb3a6344521dd01ab3b3660eb95bbdd0b6dfe008cd6f803e99ac054

# Tailwind standalone CLI (no Node.js needed), cached outside the repo.
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  PLATFORM=macos-arm64 ;;
  Darwin-x86_64) PLATFORM=macos-x64 ;;
  Linux-aarch64) PLATFORM=linux-arm64 ;;
  Linux-x86_64)  PLATFORM=linux-x64 ;;
  *) echo "unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/cpi-log-lens"
TAILWIND="$CACHE/tailwindcss-${TAILWIND_VERSION}-${PLATFORM}"
if [ ! -x "$TAILWIND" ]; then
  mkdir -p "$CACHE"
  curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${PLATFORM}" -o "$TAILWIND"
  chmod +x "$TAILWIND"
fi
"$TAILWIND" -i scripts/tailwind.input.css -o "$VENDOR/tailwind.min.css" \
  --content "frontend/index.html,frontend/js/**/*.js" --minify

echo "vendored: alpine ${ALPINE_VERSION}, chart.js ${CHARTJS_VERSION}, daisyUI ${DAISYUI_VERSION}, tailwind ${TAILWIND_VERSION}"
