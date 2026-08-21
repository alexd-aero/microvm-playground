"""ttyd as the terminal.

One ttyd process per playground, each bound to localhost on an ephemeral port
and serving under its own base path. The application reverse-proxies those
paths, so only the single server port ever needs to be reachable -- which is
what makes this work in Codespaces, where per-VM ports could not be forwarded
individually.

ttyd runs `tools/attach.py`, which bridges the pty it provides to the
playground's console websocket. That keeps every backend working through one
code path, and because ttyd starts a fresh command per browser connection,
multiple viewers each get their own bridge.
"""
import contextlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import config as C

ROOT = Path(__file__).resolve().parent.parent

# Pitch black, classic xterm ANSI palette -- the same look as the built-in
# terminal, expressed as ttyd client options. Normal blue is lifted from the
# authentic #0000ee, which is unreadable on black.
THEME = (
    '{"background":"#000000","foreground":"#e5e5e5","cursor":"#e5e5e5",'
    '"cursorAccent":"#000000","selectionBackground":"rgba(94,234,212,0.28)",'
    '"black":"#000000","brightBlack":"#7f7f7f",'
    '"red":"#cd0000","brightRed":"#ff0000",'
    '"green":"#00cd00","brightGreen":"#00ff00",'
    '"yellow":"#cdcd00","brightYellow":"#ffff00",'
    '"blue":"#3b3bff","brightBlue":"#5c5cff",'
    '"magenta":"#cd00cd","brightMagenta":"#ff00ff",'
    '"cyan":"#00cdcd","brightCyan":"#00ffff",'
    '"white":"#e5e5e5","brightWhite":"#ffffff"}'
)


def ttyd_bin() -> Optional[str]:
    if C.TTYD_BIN and Path(C.TTYD_BIN).exists():
        return C.TTYD_BIN
    found = shutil.which(C.TTYD_BIN or "ttyd")
    if found:
        return found
    local = C.STATE_DIR / "bin" / ("ttyd.exe" if os.name == "nt" else "ttyd")
    return str(local) if local.exists() else None


def available() -> bool:
    """ttyd needs a POSIX pty for the bridge; Windows keeps the built-in term."""
    if C.USE_TTYD == "off":
        return False
    if os.name == "nt":
        return False
    return ttyd_bin() is not None


def version() -> Optional[str]:
    exe = ttyd_bin()
    if not exe:
        return None
    with contextlib.suppress(Exception):
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        out = (p.stdout or p.stderr).strip().splitlines()
        if out:
            return out[0]
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TtydSession:
    """A ttyd process serving one playground's terminal."""

    def __init__(self, vm_id: str, server_port: int):
        self.vm_id = vm_id
        self.server_port = server_port
        self.port: Optional[int] = None
        self.proc: Optional[subprocess.Popen] = None

    @property
    def base_path(self) -> str:
        return "/terminal/%s" % self.vm_id

    @property
    def upstream(self) -> str:
        return "127.0.0.1:%d" % self.port

    def start(self) -> None:
        exe = ttyd_bin()
        if not exe:
            raise RuntimeError("ttyd not found")
        self.port = _free_port()

        # replay=0: ttyd hands every connection a clean screen, so replaying the
        # boot log would land it on top of whatever the shell is showing.
        console = ("ws://127.0.0.1:%d/api/vms/%s/console?replay=0"
                   % (self.server_port, self.vm_id))
        argv = [
            exe,
            "--port", str(self.port),
            "--interface", "127.0.0.1",     # only the proxy may reach it
            "--base-path", self.base_path,
            "--writable",
            "--max-clients", str(C.TTYD_MAX_CLIENTS),
            "--client-option", "theme=" + THEME,
            "--client-option", "fontSize=14",
            "--client-option", "fontFamily=Cascadia Mono,JetBrains Mono,Consolas,DejaVu Sans Mono,monospace",
            "--client-option", "cursorStyle=block",
            "--client-option", "cursorBlink=true",
            "--client-option", "rendererType=webgl",
            "--client-option", "disableLeaveAlert=true",
            "--client-option", "titleFixed=playground",
            sys.executable, str(ROOT / "tools" / "attach.py"), console,
        ]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(self.proc.pid), 15)
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=5)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(self.proc.pid), 9)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


_sessions: dict[str, TtydSession] = {}


def get(vm_id: str) -> Optional[TtydSession]:
    return _sessions.get(vm_id)


def start_for(vm_id: str, server_port: int) -> Optional[TtydSession]:
    if not available():
        return None
    stop_for(vm_id)
    s = TtydSession(vm_id, server_port)
    try:
        s.start()
    except Exception:
        return None
    _sessions[vm_id] = s
    return s


def stop_for(vm_id: str) -> None:
    s = _sessions.pop(vm_id, None)
    if s:
        s.stop()


def stop_all() -> None:
    for vm_id in list(_sessions):
        stop_for(vm_id)
