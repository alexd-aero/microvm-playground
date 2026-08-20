#!/usr/bin/env bash
# Install ttyd, preferring the distro package and falling back to the official
# static binary. ttyd is not in every suite, and a missing package should not
# be the reason the terminal silently degrades.
set -euo pipefail
DEST=${1:-/usr/local/bin/ttyd}
SUDO=""; [ "$(id -u)" = "0" ] || SUDO="sudo"

if command -v ttyd >/dev/null 2>&1; then
  echo "ttyd already present: $(command -v ttyd) ($(ttyd --version 2>&1 | head -1))"
  exit 0
fi

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq || true
  if $SUDO apt-get install -y -qq ttyd >/dev/null 2>&1 && command -v ttyd >/dev/null 2>&1; then
    echo "installed ttyd from apt: $(ttyd --version 2>&1 | head -1)"
    exit 0
  fi
fi

case "$(uname -m)" in
  x86_64)        ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) echo "no prebuilt ttyd for $(uname -m)" >&2; exit 1 ;;
esac

VER=$(curl -fsSL https://api.github.com/repos/tsl0922/ttyd/releases/latest 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)
[ -n "$VER" ] || VER=1.7.7

echo "downloading ttyd $VER ($ARCH)"
TMP=$(mktemp)
curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${VER}/ttyd.${ARCH}" -o "$TMP"
$SUDO install -m0755 "$TMP" "$DEST"
rm -f "$TMP"
echo "installed $DEST ($("$DEST" --version 2>&1 | head -1))"
