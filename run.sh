#!/usr/bin/env bash
# Start the playground server.
#
#   ./run.sh                          auto-select the best ready backend
#   sudo ./run.sh                     needed only for the firecracker backend
#   MVMP_BACKEND=container ./run.sh   native speed, millisecond launches
#   MVMP_BACKEND=qemu ./run.sh        unprivileged VM, slow without KVM
#   ./run.sh --mock                   simulated VMs, UI work only
#
# The server prints the backend it chose and what to expect from it on startup.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3 || command -v python)

# Mirror the server's own resolution closely enough to know whether root is
# needed. The server is the authority -- this only decides whether to stop early
# with a useful message, so it must not forget the container backend exists.
backend="${MVMP_BACKEND:-auto}"
case " $* " in *" --mock "*) backend=mock ;; esac
if [ "$backend" = "auto" ]; then
  if [ -r /dev/kvm ] && [ -w /dev/kvm ] && command -v firecracker >/dev/null 2>&1; then
    backend=firecracker
  elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    backend=container
  else
    backend=qemu
  fi
fi

# Only firecracker needs privileges: it creates tap devices and NAT rules.
# QEMU's user-mode networking runs entirely in your own account.
if [ "$backend" = "firecracker" ] && [ "$(id -u)" != "0" ]; then
  echo "The firecracker backend needs root (tap devices + NAT rules)." >&2
  echo "  sudo ./run.sh                  run it privileged" >&2
  echo "  MVMP_BACKEND=qemu ./run.sh     unprivileged QEMU guests instead" >&2
  exit 1
fi

exec "$PY" "$HERE/run_server.py" "$@"
