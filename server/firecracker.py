"""Firecracker backend: one VMM process per playground, serial console on a pty."""
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from . import config as C
from . import images, net
from .console import ConsoleHub

# Unix-only modules; imported defensively so this file can still be *loaded* on
# Windows when the server runs in mock mode.
try:
    import fcntl
    import pty
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    fcntl = pty = termios = tty = None  # type: ignore


class VMMError(RuntimeError):
    pass


class FirecrackerVM:
    """Owns the lifecycle of exactly one microVM."""

    def __init__(self, vm_id: str, slot: int, vcpus: int, mem_mib: int, disk_gb: int,
                 name: str = "", image: Optional[str] = None):
        self.id = vm_id
        self.name = name
        self.slot = slot
        self.vcpus = vcpus
        self.mem_mib = mem_mib
        self.disk_gb = disk_gb

        self.dir = C.VMS_DIR / vm_id
        self.api_sock = self.dir / "api.sock"
        self.rootfs = self.dir / "rootfs.ext4"
        self.base_rootfs = Path(image) if image else C.BASE_ROOTFS
        self.log_path = self.dir / "firecracker.log"

        self.proc: Optional[subprocess.Popen] = None
        self.console = ConsoleHub()
        self.ip: Optional[str] = None
        self.gateway: Optional[str] = None
        self.boot_ms: Optional[int] = None

        self._master_fd: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        if pty is None:
            raise VMMError("Firecracker mode requires Linux (WSL2). Use mock mode on Windows.")
        if not Path(C.FIRECRACKER_BIN).exists():
            raise VMMError("firecracker binary not found at " + str(C.FIRECRACKER_BIN))
        if not C.KERNEL_PATH.exists():
            raise VMMError("kernel not found at " + str(C.KERNEL_PATH) + " (run setup.sh)")

        self._loop = asyncio.get_running_loop()
        self.dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        await asyncio.to_thread(images.provision, self.rootfs, self.disk_gb,
                                self.base_rootfs)
        self.ip, self.gateway = await asyncio.to_thread(net.setup, self.slot)

        self._spawn()
        await self._wait_for_api()
        await self._configure()
        await self._api("PUT", "/actions", {"action_type": "InstanceStart"})

        self.boot_ms = int((time.monotonic() - t0) * 1000)

    def _spawn(self) -> None:
        master, slave = pty.openpty()
        tty.setraw(slave)
        tty.setraw(master)
        # Seed a sane geometry. The guest learns its real size from busybox
        # `resize` at login, and from stty when the browser window changes.
        with contextlib.suppress(Exception):
            winsz = int(24).to_bytes(2, "little") + int(80).to_bytes(2, "little") + b"\x00" * 4
            fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)

        self.log_path.touch()
        with contextlib.suppress(FileNotFoundError):
            self.api_sock.unlink()

        self.proc = subprocess.Popen(
            [C.FIRECRACKER_BIN, "--api-sock", str(self.api_sock), "--id", self.id[:8]],
            stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)

        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._master_fd = master
        self._loop.add_reader(master, self._drain)

    def _drain(self) -> None:
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:  # VMM exited, pty hung up
            self._detach_reader()
            self.console.close()
            return
        self.console.publish(data)

    # ------------------------------------------------------------- config API
    async def _wait_for_api(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.api_sock.exists():
                return
            if self.proc and self.proc.poll() is not None:
                raise VMMError("firecracker exited early (code %s)" % self.proc.returncode)
            await asyncio.sleep(0.02)
        raise VMMError("timed out waiting for the firecracker API socket")

    async def _api(self, method: str, path: str, body: dict) -> None:
        transport = httpx.AsyncHTTPTransport(uds=str(self.api_sock))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
            r = await c.request(method, path, content=json.dumps(body),
                                headers={"Content-Type": "application/json"}, timeout=10.0)
        if r.status_code >= 300:
            raise VMMError("%s %s -> %s: %s" % (method, path, r.status_code, r.text))

    async def _configure(self) -> None:
        # Logger first, so VMM chatter lands in a file rather than the console pty.
        with contextlib.suppress(VMMError):
            await self._api("PUT", "/logger", {
                "log_path": str(self.log_path), "level": "Warn",
                "show_level": True, "show_log_origin": False,
            })

        # mvmp.host is read by the Alpine netup script; systemd.hostname is the
        # equivalent for the Debian guest. Harmless on whichever is not in use.
        host = self.name or self.id[:8]
        boot_args = " ".join([
            C.BOOT_ARGS_BASE,
            net.ip_boot_arg(self.slot),
            "mvmp.host=" + host,
            "systemd.hostname=" + host,
        ])
        await self._api("PUT", "/boot-source", {
            "kernel_image_path": str(C.KERNEL_PATH), "boot_args": boot_args,
        })
        await self._api("PUT", "/drives/rootfs", {
            "drive_id": "rootfs", "path_on_host": str(self.rootfs),
            "is_root_device": True, "is_read_only": False,
        })
        await self._api("PUT", "/machine-config", {
            "vcpu_count": self.vcpus, "mem_size_mib": self.mem_mib, "smt": False,
        })
        await self._api("PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "host_dev_name": net.tap_name(self.slot),
            "guest_mac": net.guest_mac(self.slot),
        })

    # -------------------------------------------------------------------- I/O
    def write(self, data: bytes) -> None:
        if self._master_fd is None:
            return
        with contextlib.suppress(OSError):
            os.write(self._master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        """Serial lines carry no SIGWINCH, so tell the guest shell directly."""
        if self._master_fd is None:
            return
        self.write(("stty rows %d cols %d\n" % (int(rows), int(cols))).encode())

    # --------------------------------------------------------------- teardown
    def _detach_reader(self) -> None:
        if self._master_fd is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._master_fd)

    async def _halt(self, graceful: bool = True) -> None:
        if self.proc and self.proc.poll() is None:
            if graceful:
                with contextlib.suppress(Exception):
                    await self._api("PUT", "/actions", {"action_type": "SendCtrlAltDel"})
                for _ in range(50):
                    if self.proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    self.proc.wait(timeout=3)
        self._detach_reader()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def suspend(self) -> None:
        """Halt the VM but keep its rootfs and its tap device."""
        await self._halt(graceful=True)
        self.console.close()

    async def resume(self) -> None:
        if not self.rootfs.exists():
            raise VMMError("this playground's disk is gone; it cannot be resumed")
        self._loop = asyncio.get_running_loop()
        self.console = ConsoleHub()
        self._spawn()
        await self._wait_for_api()
        await self._configure()
        await self._api("PUT", "/actions", {"action_type": "InstanceStart"})

    async def stop(self, graceful: bool = True) -> None:
        if self.proc and self.proc.poll() is None:
            if graceful:
                with contextlib.suppress(Exception):
                    await self._api("PUT", "/actions", {"action_type": "SendCtrlAltDel"})
                for _ in range(50):  # up to ~5s for a clean poweroff
                    if self.proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    self.proc.wait(timeout=3)

        self._detach_reader()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

        self.console.close()
        await asyncio.to_thread(net.teardown, self.slot)
        await asyncio.to_thread(images.destroy, self.dir)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
