#!/usr/bin/env bash
# Build the offline UI stylesheet with pinned standalone Tailwind and daisyUI
# artifacts. No Node.js or npm installation is used.
set -euo pipefail
cd "$(dirname "$0")/.."

TAILWIND_VERSION=4.3.3
DAISYUI_VERSION=5.7.47

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    PLATFORM=macos-arm64
    TAILWIND_SHA256=cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d
    ;;
  Darwin-x86_64)
    PLATFORM=macos-x64
    TAILWIND_SHA256=7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1
    ;;
  Linux-aarch64)
    PLATFORM=linux-arm64
    TAILWIND_SHA256=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195
    ;;
  Linux-x86_64)
    PLATFORM=linux-x64
    TAILWIND_SHA256=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
    ;;
  *)
    echo "unsupported platform $(uname -s)-$(uname -m)" >&2
    exit 1
    ;;
esac

if [[ "$PLATFORM" == linux-* ]] && ! getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
  echo "unsupported platform: the pinned Linux CLI requires GNU libc" >&2
  exit 1
fi

DAISYUI_SHA256=85819d3fe86a852237b439b13f481481aac58e562ac47694cd11c3038e994f2a
TAILWIND_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${PLATFORM}"
DAISYUI_URL="https://github.com/saadeghi/daisyui/releases/download/v${DAISYUI_VERSION}/daisyui.mjs"
CACHE="${CSS_TOOL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/cpi-log-lens}"
TAILWIND="$CACHE/tailwindcss-${TAILWIND_VERSION}-${PLATFORM}"
DAISYUI="$CACHE/daisyui-${DAISYUI_VERSION}.mjs"
OUTPUT=frontend/tailwind.css
WATCH=()

while (($#)); do
  case "$1" in
    --watch) WATCH+=(--watch=always); shift ;;
    --output|-o)
      if (($# < 2)); then
        echo "missing path after $1" >&2
        exit 2
      fi
      OUTPUT=$2
      shift 2
      ;;
    *)
      echo "usage: $0 [--watch] [--output PATH]" >&2
      exit 2
      ;;
  esac
done

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f 1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d ' ' -f 1
  else
    echo "sha256sum or shasum is required" >&2
    return 1
  fi
}

verify() {
  local file=$1 expected=$2 actual
  actual=$(sha256 "$file")
  if [[ "$actual" != "$expected" ]]; then
    echo "SHA-256 mismatch for $file: expected $expected, got $actual" >&2
    return 1
  fi
}

fetch_verified() {
  local url=$1 file=$2 expected=$3 temporary="${2}.tmp.$$"
  if [[ -e "$file" ]]; then
    verify "$file" "$expected"
    return
  fi
  echo "downloading $url"
  if ! curl -fsSL "$url" -o "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  if ! verify "$temporary" "$expected"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$file"
}

mkdir -p "$CACHE"
fetch_verified "$TAILWIND_URL" "$TAILWIND" "$TAILWIND_SHA256"
fetch_verified "$DAISYUI_URL" "$DAISYUI" "$DAISYUI_SHA256"
chmod +x "$TAILWIND"

BUILD_DIR=scripts/.build
mkdir -p "$BUILD_DIR"
cp "$DAISYUI" "$BUILD_DIR/daisyui.mjs"
cleanup() {
  rm -f "$BUILD_DIR/daisyui.mjs"
  rmdir "$BUILD_DIR" 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$(dirname "$OUTPUT")"
if ((${#WATCH[@]})); then
  "$TAILWIND" -i scripts/tailwind.input.css -o "$OUTPUT" --minify "${WATCH[@]}"
else
  "$TAILWIND" -i scripts/tailwind.input.css -o "$OUTPUT" --minify
fi

if ((${#WATCH[@]} == 0)); then
  GZIP_BYTES=$(gzip -n -c "$OUTPUT" | wc -c | tr -d ' ')
  if ((GZIP_BYTES > 61440)); then
    echo "generated CSS is ${GZIP_BYTES} bytes gzip; maximum is 61440" >&2
    exit 1
  fi
  echo "generated $OUTPUT (${GZIP_BYTES} bytes gzip)"
fi
