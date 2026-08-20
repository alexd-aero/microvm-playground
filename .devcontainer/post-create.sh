#!/usr/bin/env bash
# Runs once when the Codespace is created.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

c() { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }

c '1;36' "==> Installing Python dependencies"
pip install --quiet --disable-pip-version-check -r requirements.txt
c '32' "    ok"

c '1;36' "==> Installing ttyd (the terminal)"
bash tools/get-ttyd.sh >/dev/null 2>&1 && c '32' "    ok  $(ttyd --version 2>&1 | head -1)"   || c '33' "    !   ttyd unavailable; falling back to the built-in terminal"

c '1;36' "==> Building the playground image"
docker build -q -t "${MVMP_IMAGE_TAG:-mvmp-playground:latest}" docker/ >/dev/null
c '32' "    ok  $(docker image inspect --format '{{.Size}}' "${MVMP_IMAGE_TAG:-mvmp-playground:latest}" | awk '{printf "%.0f MB", $1/1048576}')"

c '1;36' "==> Backend"
if [ -e /dev/kvm ]; then
  c '32' "    /dev/kvm present -- VM backends are available"
else
  c '33' "    no /dev/kvm (expected in Codespaces) -- using the container backend:"
  c '33' "    native speed, millisecond startup, shared kernel."
fi

echo
c '1;32' "Ready. Start it with:  ./run.sh"
c '90'   "Port 8080 will be forwarded automatically."
