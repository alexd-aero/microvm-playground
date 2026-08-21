"""Container backend: millisecond startup at native speed.

This is the backend that works in GitHub Codespaces, where /dev/kvm is not
exposed and therefore no VM backend can be hardware-accelerated. Containers
execute directly on the host CPU -- no emulation, no hypervisor -- so they are
both instant and full speed.

The honest trade-off, stated plainly: a container shares the host kernel. It is
a weaker boundary than a microVM. Use it for disposable scratch environments,
not for running code you actively distrust.

Startup is kept in the milliseconds by a warm pool: containers are created and
started ahead of time, then adopted on demand and resized in place with
`docker update`, so a launch costs an exec rather than a create+start.
"""
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from typing import Optional

from . import config as C
from .console import ConsoleHub

try:
    import fcntl
    import pty
    import struct
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    fcntl = pty = struct = termios = tty = None  # type: ignore


class ContainerError(RuntimeError):
    pass


def docker_bin() -> Optional[str]:
    return shutil.which(C.DOCKER_BIN) or shutil.which("podman")


def has_docker() -> bool:
    """Docker present *and* its daemon reachable."""
    exe = docker_bin()
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


def docker_version() -> Optional[str]:
    exe = docker_bin()
    if not exe:
        return None
    with contextlib.suppress(Exception):
        p = subprocess.run([exe, "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            return "%s %s" % (os.path.basename(exe), p.stdout.strip())
    return None


def image_present() -> bool:
    exe = docker_bin()
    if not exe:
        return False
    with contextlib.suppress(Exception):
        p = subprocess.run([exe, "image", "inspect", C.CONTAINER_IMAGE],
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    return False


def _run(*args: str, timeout: int = 60) -> str:
    exe = docker_bin()
    if not exe:
        raise ContainerError("docker not found")
    p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise ContainerError("docker %s failed: %s" % (args[0], (p.stderr or p.stdout).strip()))
    return p.stdout.strip()


class WarmPool:
    """Keeps a few containers started so a launch is just an exec."""

    def __init__(self, size: int):
        self.size = size
        self._ready: list[str] = []
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def _create(self) -> str:
        return _run(
            "run", "-d",
            "--label", "mvmp=1",
            "--label", "mvmp-role=pool",
            "--hostname", "playground",
            "--cpus", str(C.DEFAULT_VCPUS),
            "--memory", "%dm" % C.DEFAULT_MEM_MIB,
            "--pids-limit", str(C.CONTAINER_PIDS_LIMIT),
            "--security-opt", "no-new-privileges",
            C.CONTAINER_IMAGE, "sleep", "infinity",
        )

    async def acquire(self) -> tuple[str, bool]:
        """Returns (container_id, came_from_pool)."""
        async with self._lock:
            while self._ready:
                cid = self._ready.pop()
                if await asyncio.to_thread(self._alive, cid):
                    self.refill_soon()
                    return cid, True
        cid = await asyncio.to_thread(self._create)
        self.refill_soon()
        return cid, False

    def _alive(self, cid: str) -> bool:
        with contextlib.suppress(Exception):
            return _run("inspect", "-f", "{{.State.Running}}", cid, timeout=20) == "true"
        return False

    def refill_soon(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refill())

    async def _refill(self) -> None:
        with contextlib.suppress(Exception):
            while len(self._ready) < self.size:
                cid = await asyncio.to_thread(self._create)
                self._ready.append(cid)

    async def drain(self) -> None:
        async with self._lock:
            ids, self._ready = self._ready, []
        for cid in ids:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_run, "rm", "-f", cid)


_pool: Optional[WarmPool] = None


def pool() -> WarmPool:
    global _pool
    if _pool is None:
        _pool = WarmPool(C.CONTAINER_POOL)
    return _pool


class ContainerVM:
    """Owns the lifecycle of exactly one playground container."""

    def __init__(self, vm_id: str, slot: int, vcpus: int, mem_mib: int, disk_gb: int,
                 name: str = "", image: Optional[str] = None):
        self.id = vm_id
        self.name = name or vm_id[:8]
        self.image = image or C.CONTAINER_IMAGE
        self.slot = slot
        self.vcpus = vcpus
        self.mem_mib = mem_mib
        self.disk_gb = disk_gb

        self.cid: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self.console = ConsoleHub()
        self.ip: Optional[str] = None
        self.gateway: Optional[str] = None
        self.boot_ms: Optional[int] = None

        self._master_fd: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        if pty is None:
            raise ContainerError(
                "The container backend needs a POSIX pty (Linux/macOS). "
                "On Windows use the QEMU backend.")
        if not has_docker():
            raise ContainerError("Docker is not available or its daemon is not running.")
        if self.image == C.CONTAINER_IMAGE and not image_present():
            raise ContainerError(
                "Image %s not built. Run: docker build -t %s docker/"
                % (C.CONTAINER_IMAGE, C.CONTAINER_IMAGE))

        self._loop = asyncio.get_running_loop()
        t0 = time.monotonic()

        if self.image == C.CONTAINER_IMAGE:
            self.cid, _from_pool = await pool().acquire()
        else:
            # The warm pool holds the default image only, so a different OS has
            # to be started for real. Docker pulls it on first use, which is why
            # the catalogue marks un-pulled images as not ready.
            self.cid = await asyncio.to_thread(self._create_own)

        # Apply this playground's limits. Pool members were created with the
        # defaults, so adopting one means resizing it in place.
        with contextlib.suppress(ContainerError):
            await asyncio.to_thread(
                _run, "update", "--cpus", str(self.vcpus),
                "--memory", "%dm" % self.mem_mib, "--memory-swap", "%dm" % self.mem_mib,
                self.cid)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_run, "rename", self.cid, "mvmp-" + self.id)

        self.ip = await asyncio.to_thread(self._address)
        self._attach()
        self.boot_ms = int((time.monotonic() - t0) * 1000)

    def _create_own(self) -> str:
        return _run(
            "run", "-d",
            "--label", "mvmp=1",
            "--hostname", "playground",
            "--cpus", str(self.vcpus),
            "--memory", "%dm" % self.mem_mib,
            "--pids-limit", str(C.CONTAINER_PIDS_LIMIT),
            "--security-opt", "no-new-privileges",
            self.image, "sleep", "infinity",
            timeout=600,          # includes a possible image pull
        )

    def _address(self) -> Optional[str]:
        with contextlib.suppress(Exception):
            out = _run("inspect", "-f",
                       "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{.Gateway}}{{end}}",
                       self.cid, timeout=20)
            parts = out.split()
            if parts:
                if len(parts) > 1:
                    self.gateway = parts[1]
                return parts[0]
        return None

    def _attach(self) -> None:
        """A login shell inside the container, wired to a pty."""
        master, slave = pty.openpty()
        tty.setraw(slave)
        self._set_winsize(slave, 24, 80)

        exe = docker_bin()
        self.proc = subprocess.Popen(
            [exe, "exec", "-it", "-e", "TERM=xterm-256color",
             "-e", "COLORTERM=truecolor", self.cid,
             # Alpine and other minimal images have no bash.
             "/bin/sh", "-lc", "exec /bin/bash --login 2>/dev/null || exec /bin/sh -l"],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)

        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._master_fd = master
        self._loop.add_reader(master, self._drain)

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        with contextlib.suppress(Exception):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _drain(self) -> None:
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._detach_reader()
            self.console.close()
            return
        self.console.publish(data)

    # -------------------------------------------------------------------- I/O
    def write(self, data: bytes) -> None:
        if self._master_fd is None:
            return
        with contextlib.suppress(OSError):
            os.write(self._master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        """Unlike a serial console, this path has a real pty: resizing it makes
        the docker client propagate SIGWINCH into the container. No stty
        injection, so no stray line in the shell."""
        if self._master_fd is None:
            return
        self._set_winsize(self._master_fd, int(rows), int(cols))
        with contextlib.suppress(Exception):
            if self.proc and self.proc.poll() is None:
                os.killpg(os.getpgid(self.proc.pid), 28)  # SIGWINCH

    # --------------------------------------------------------------- teardown
    def _detach_reader(self) -> None:
        if self._master_fd is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._master_fd)

    def _detach_console(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.kill()
        self._detach_reader()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
        self.console.close()

    async def suspend(self) -> None:
        """Stop the container without removing it: `docker stop` keeps the
        writable layer, so everything the user wrote is still there on start."""
        self._stopped = True
        self._detach_console()
        if self.cid:
            await asyncio.to_thread(_run, "stop", "-t", "5", self.cid, timeout=90)

    async def resume(self) -> None:
        if not self.cid:
            raise ContainerError("this playground no longer exists")
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(_run, "start", self.cid, timeout=90)
        self.console = ConsoleHub()          # the old hub was closed on suspend
        self.ip = await asyncio.to_thread(self._address)
        self._stopped = False
        self._attach()

    async def stop(self, graceful: bool = True) -> None:
        self._stopped = True
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.kill()
        self._detach_reader()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
        self.console.close()
        if self.cid:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_run, "rm", "-f", "-v", self.cid, timeout=60)

    @property
    def alive(self) -> bool:
        if self._stopped:
            return False
        return self.proc is not None and self.proc.poll() is None


async def cleanup_orphans() -> None:
    """Remove containers left behind by a previous run."""
    with contextlib.suppress(Exception):
        out = await asyncio.to_thread(_run, "ps", "-aq", "--filter", "label=mvmp=1")
        for cid in [l for l in out.splitlines() if l.strip()]:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_run, "rm", "-f", "-v", cid)
