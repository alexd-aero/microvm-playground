"""QEMU backend: real Linux guests on any host, no KVM and no WSL required.

Chosen for Windows, where /dev/kvm does not exist. Three things make this work
without privileges of any kind:

  * TCG software emulation runs the guest with no hypervisor at all. WHPX is
    used instead when Windows exposes it, which is roughly 5-10x faster.
  * User-mode (SLIRP) networking gives the guest full outbound internet through
    the host's own sockets -- no tap device, no NAT rules, no admin rights.
  * qcow2 backing files make each playground an instant copy-on-write clone of
    the golden image, so a "3 GB" disk costs a few hundred KB until written to.

The serial console and the QMP control channel are both TCP sockets that QEMU
connects *out* to, which sidesteps the fact that Windows has no ptys and avoids
any port-allocation race.
"""
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import config as C
from .console import ConsoleHub


class QemuError(RuntimeError):
    pass


def find_binary(name: str) -> Optional[str]:
    """Locate a QEMU executable on PATH or in the usual Windows install dirs."""
    return C.find_qemu_binary(name)


_accel_cache: Optional[str] = None


def detect_accelerator(qemu_bin: str) -> str:
    """Pick the fastest accelerator that actually *works* on this host.

    `-accel help` is not enough: a QEMU built with WHPX support still fails at
    runtime when the Windows hypervisor refuses it (typically hr=80370302,
    "failed to enable nested virtualization", when VBS/Memory Integrity already
    owns the hypervisor). So each candidate is really launched once, frozen at
    startup with -S, and kept only if it survives init.
    """
    global _accel_cache
    if C.QEMU_ACCEL:
        return C.QEMU_ACCEL
    if _accel_cache is not None:
        return _accel_cache

    try:
        out = subprocess.run([qemu_bin, "-accel", "help"],
                             capture_output=True, text=True, timeout=15)
        available = (out.stdout + out.stderr).lower()
    except Exception:
        available = ""

    for cand in ("kvm", "whpx", "hvf"):
        if cand not in available:
            continue
        try:
            p = subprocess.Popen(
                [qemu_bin, "-accel", cand, "-m", "128", "-display", "none",
                 "-nodefaults", "-S", "-monitor", "none", "-serial", "none"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            try:
                stdout, _ = p.communicate(timeout=8)
                # Exited on its own within the window: it failed to initialise.
                if b"rror" in stdout or b"ailed" in stdout or p.returncode not in (0, None):
                    continue
            except subprocess.TimeoutExpired:
                p.kill()          # still running == accelerator came up fine
                _accel_cache = cand
                return cand
        except Exception:
            continue

    _accel_cache = "tcg"
    return _accel_cache


class QemuVM:
    """Owns the lifecycle of exactly one QEMU guest."""

    def __init__(self, vm_id: str, slot: int, vcpus: int, mem_mib: int, disk_gb: int,
                 name: str = "", image: Optional[str] = None):
        self.id = vm_id
        self.name = name or vm_id[:8]
        self.slot = slot
        self.vcpus = vcpus
        self.mem_mib = mem_mib
        self.disk_gb = disk_gb

        self.dir = C.VMS_DIR / vm_id
        self.overlay = self.dir / "disk.qcow2"
        self.base_image = Path(image) if image else C.BASE_IMAGE

        self.proc: Optional[subprocess.Popen] = None
        self.console = ConsoleHub()
        self.ip: Optional[str] = None
        self.gateway: Optional[str] = None
        self.boot_ms: Optional[int] = None
        self.accel: str = "tcg"
        self.ssh_port: Optional[int] = None

        self._serial_srv: Optional[asyncio.AbstractServer] = None
        self._qmp_srv: Optional[asyncio.AbstractServer] = None
        self._ser_w: Optional[asyncio.StreamWriter] = None
        self._qmp_r: Optional[asyncio.StreamReader] = None
        self._qmp_w: Optional[asyncio.StreamWriter] = None
        self._tasks: list[asyncio.Task] = []
        self._connected = asyncio.Event()

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        qemu = find_binary(C.QEMU_SYSTEM)
        qemu_img = find_binary("qemu-img")
        if not qemu:
            raise QemuError(
                "QEMU not found. Install it with:  winget install "
                "SoftwareFreedomConservancy.QEMU   (then restart the server)")
        if not qemu_img:
            raise QemuError("qemu-img not found next to " + qemu)
        if not self.base_image.exists():
            raise QemuError("image missing at %s -- run setup.ps1" % self.base_image)

        self.accel = detect_accelerator(qemu)
        self.dir.mkdir(parents=True, exist_ok=True)

        await self._make_overlay(qemu_img)
        await self._launch(qemu)

    async def _launch(self, qemu: str) -> None:
        """Bring the VM up against whatever disk is already on the filesystem.

        Split out of start() so resume() can reuse it: a suspended playground
        keeps its overlay, so coming back is exactly this minus the disk
        creation.
        """
        t0 = time.monotonic()
        self._connected = asyncio.Event()

        # Both channels: we listen, QEMU dials out. No races, no port guessing.
        loop = asyncio.get_running_loop()
        self._serial_srv = await asyncio.start_server(self._on_serial, "127.0.0.1", 0)
        self._qmp_srv = await asyncio.start_server(self._on_qmp, "127.0.0.1", 0)
        ser_port = self._serial_srv.sockets[0].getsockname()[1]
        qmp_port = self._qmp_srv.sockets[0].getsockname()[1]

        self.proc = subprocess.Popen(
            self._argv(qemu, ser_port, qmp_port),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self._tasks.append(loop.create_task(self._watch_stderr()))

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30)
        except asyncio.TimeoutError:
            raise QemuError("QEMU did not connect its serial console within 30s")

        # SLIRP hands out a fixed layout; no DHCP snooping required.
        self.ip, self.gateway = "10.0.2.15", "10.0.2.2"
        self.boot_ms = int((time.monotonic() - t0) * 1000)

    async def _make_overlay(self, qemu_img: str) -> None:
        """Copy-on-write clone of the golden image -- instant, near-zero bytes."""
        args = [qemu_img, "create", "-q", "-f", "qcow2",
                "-F", "qcow2", "-b", str(self.base_image), str(self.overlay)]
        target_gb = max(self.disk_gb, C.base_image_gb())
        args.append("%dG" % target_gb)
        p = await asyncio.to_thread(
            subprocess.run, args, capture_output=True, text=True)
        if p.returncode != 0:
            raise QemuError("qemu-img create failed: " + (p.stderr or p.stdout).strip())

    def _argv(self, qemu: str, ser_port: int, qmp_port: int) -> list[str]:
        accel = self.accel
        # Multi-threaded TCG parallelises vCPUs across host threads; it is only
        # valid for tcg, and only helps when there is more than one vCPU.
        if accel == "tcg" and self.vcpus > 1:
            accel = "tcg,thread=multi"

        argv = [
            qemu,
            # -nodefaults is deliberately NOT used: it strips devices the stock
            # cloud image expects and the guest then boots to a black hole.
            "-display", "none",
            "-machine", "q35",
            "-accel", accel,
            "-cpu", "max",
            "-smp", str(self.vcpus),
            "-m", str(self.mem_mib),
            # virtio everywhere: far less emulation work than legacy devices
            "-drive", "file=%s,if=virtio,format=qcow2,cache=writeback,discard=unmap" % self.overlay,
            "-netdev", "user,id=net0" + (",hostfwd=tcp:127.0.0.1:%d-:22" % self.ssh_port
                                         if self.ssh_port else ""),
            "-device", "virtio-net-pci,netdev=net0",
            # Without an RNG the guest stalls for ages waiting on entropy.
            "-object", "rng-random,id=rng0,filename=/dev/urandom" if os.name != "nt"
                       else "rng-builtin,id=rng0",
            "-device", "virtio-rng-pci,rng=rng0",
            "-serial", "tcp:127.0.0.1:%d,server=off" % ser_port,
            "-qmp", "tcp:127.0.0.1:%d,server=off" % qmp_port,
            "-rtc", "base=utc",
        ]
        return argv

    # -------------------------------------------------------------- channels
    async def _on_serial(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._ser_w = writer
        self._connected.set()
        with contextlib.suppress(Exception):
            sock = writer.get_extra_info("socket")
            if sock is not None:
                import socket as _s
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.console.publish(data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.console.close()

    async def _on_qmp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._qmp_r, self._qmp_w = reader, writer
        with contextlib.suppress(Exception):
            await reader.readline()                      # greeting
            await self._qmp_cmd("qmp_capabilities")
        # Drain events so the socket buffer never fills and blocks QEMU.
        with contextlib.suppress(Exception):
            while True:
                if not await reader.readline():
                    break

    async def _qmp_cmd(self, command: str, **args) -> None:
        if self._qmp_w is None:
            return
        payload = {"execute": command}
        if args:
            payload["arguments"] = args
        self._qmp_w.write((json.dumps(payload) + "\r\n").encode())
        with contextlib.suppress(Exception):
            await self._qmp_w.drain()

    async def _watch_stderr(self) -> None:
        """QEMU's own diagnostics -- surfaced on the console if it dies early."""
        if self.proc is None or self.proc.stdout is None:
            return
        buf = []
        while True:
            line = await asyncio.to_thread(self.proc.stdout.readline)
            if not line:
                break
            buf.append(line.decode("utf-8", "replace").rstrip())
            if len(buf) > 40:
                buf.pop(0)
        if self.proc.poll() not in (0, None) and buf:
            msg = "\r\n\x1b[91mQEMU exited (%s):\x1b[0m\r\n" % self.proc.returncode
            self.console.publish(msg.encode() + "\r\n".join(buf).encode() + b"\r\n")
            self.console.close()

    # ------------------------------------------------------------------- I/O
    def write(self, data: bytes) -> None:
        if self._ser_w is None:
            return
        with contextlib.suppress(Exception):
            self._ser_w.write(data)

    def resize(self, rows: int, cols: int) -> None:
        """Serial lines carry no SIGWINCH; tell the guest shell directly."""
        self.write(("stty rows %d cols %d\n" % (int(rows), int(cols))).encode())

    # -------------------------------------------------------------- teardown
    async def _halt(self, graceful: bool = True) -> None:
        """Stop the VM process and release its channels, leaving the disk."""
        if self.proc and self.proc.poll() is None:
            if graceful:
                await self._qmp_cmd("system_powerdown")     # ACPI, guest halts cleanly
                for _ in range(int(C.SHUTDOWN_WAIT_S * 10)):
                    if self.proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
            if self.proc.poll() is None:
                await self._qmp_cmd("quit")
                await asyncio.sleep(0.3)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.proc.wait, 5)

        for t in self._tasks:
            t.cancel()
        for w in (self._ser_w, self._qmp_w):
            if w is not None:
                with contextlib.suppress(Exception):
                    w.close()
        for srv in (self._serial_srv, self._qmp_srv):
            if srv is not None:
                with contextlib.suppress(Exception):
                    srv.close()

        self._tasks = []
        self._ser_w = self._qmp_w = None
        self._serial_srv = self._qmp_srv = None

    async def suspend(self) -> None:
        """Shut the guest down cleanly but keep its disk, so it can come back."""
        await self._halt(graceful=True)
        self.console.close()

    async def resume(self) -> None:
        qemu = find_binary(C.QEMU_SYSTEM)
        if not qemu:
            raise QemuError("QEMU not found")
        if not self.overlay.exists():
            raise QemuError("this playground's disk is gone; it cannot be resumed")
        self.accel = detect_accelerator(qemu)
        self.console = ConsoleHub()      # the old hub was closed on suspend
        await self._launch(qemu)

    async def stop(self, graceful: bool = True) -> None:
        await self._halt(graceful)
        self.console.close()
        # Destroying is the disposable path: take the whole directory.
        await asyncio.to_thread(shutil.rmtree, self.dir, True)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
