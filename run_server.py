#!/usr/bin/env python3
"""Entry point. Running this file puts its directory on sys.path, so the
`server` package imports cleanly no matter where you invoke it from.

    sudo python3 run_server.py              # real Firecracker microVMs (Linux/WSL2)
    python3 run_server.py --mock            # simulated VMs, no KVM needed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --mock has to land in the environment before `server.config` is imported,
# because config reads it at module load.
if "--mock" in sys.argv:
    os.environ["MVMP_MOCK"] = "1"

from server.main import main  # noqa: E402

if __name__ == "__main__":
    main()
