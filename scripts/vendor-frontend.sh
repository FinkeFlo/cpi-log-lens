#!/usr/bin/env bash
# Download the pinned JavaScript libraries into frontend/vendor/ (verified by
# SHA-256). The CSS is built separately with scripts/build-css.sh.
#
# Run after changing a version below or refreshing the vendored JavaScript:
#   scripts/vendor-frontend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VENDOR=frontend/vendor
mkdir -p "$VENDOR"

ALPINE_VERSION=3.17.4
CHARTJS_VERSION=4.4.0

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

echo "vendored: alpine ${ALPINE_VERSION}, chart.js ${CHARTJS_VERSION}"
