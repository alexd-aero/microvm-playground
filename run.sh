#!/usr/bin/env bash
# Start the playground server.
#
#   ./run.sh                       auto: firecracker if /dev/kvm, else qemu
#   sudo ./run.sh                  needed only for the firecracker backend
#   MVMP_BACKEND=qemu ./run.sh     unprivileged QEMU guests
#   ./run.sh --mock                simulated VMs, UI work only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3 || command -v python)

backend="${MVMP_BACKEND:-auto}"
case " $* " in *" --mock "*) backend=mock ;; esac
if [ "$backend" = "auto" ]; then
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then backend=firecracker; else backend=qemu; fi
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
