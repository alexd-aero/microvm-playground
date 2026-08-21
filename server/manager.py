"""VM registry: backend selection, slot allocation, lifecycle, TTL reaping."""
import asyncio
import contextlib
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config as C
from . import net
from . import ttyd as _ttyd
from . import catalog
from .models import TTL_CHOICES, CreateVM, HostInfo, VMView

ADJECTIVES = ["brisk", "amber", "quiet", "lucid", "nimble", "vivid", "candid",
              "stellar", "arctic", "ember", "cobalt", "swift"]
NOUNS = ["otter", "falcon", "cinder", "harbor", "quartz", "vector", "lantern",
         "meadow", "cipher", "pylon", "beacon", "mantis"]


def _friendly_name() -> str:
    return "%s-%s" % (ADJECTIVES[uuid.uuid4().int % len(ADJECTIVES)],
                      NOUNS[uuid.uuid4().int // 13 % len(NOUNS)])


def has_kvm() -> bool:
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def _firecracker_ready() -> bool:
    return (has_kvm() and Path(C.FIRECRACKER_BIN).exists()
            and C.KERNEL_PATH.exists() and C.BASE_ROOTFS.exists())


def _container_ready() -> bool:
    if os.name == "nt":          # needs a POSIX pty
        return False
    from .container import has_docker, image_present
    return has_docker() and image_present()


def _qemu_ready() -> bool:
    return C.find_qemu_binary(C.QEMU_SYSTEM) is not None and C.BASE_IMAGE.exists()


def resolve_backend() -> str:
    """Pick the best backend that is actually ready to run something.

    Order is isolation first, then speed:

      firecracker  a real VM at ~125 ms -- but needs /dev/kvm, which neither
                   Codespaces nor Windows provides
      container    native speed and millisecond startup, shared kernel
      qemu         a real VM anywhere, but software-emulated without KVM/WHPX

    Readiness matters as much as capability: a backend whose images are missing
    is not a candidate, because selecting it would only produce errors later.
    Mock is never selected automatically -- silently falling back to a
    simulation is how you end up thinking you have a machine when you do not.
    """
    if C.MOCK:
        return "mock"
    choice = C.BACKEND
    if choice in ("firecracker", "qemu", "container", "mock"):
        return choice

    if _firecracker_ready():
        return "firecracker"
    if _container_ready():
        return "container"
    if _qemu_ready():
        return "qemu"
    # Nothing is provisioned yet. Name the one the host could actually support
    # so host_info() can explain what to install.
    if os.name != "nt" and shutil.which(C.DOCKER_BIN):
        return "container"
    return "qemu"


@dataclass
class Record:
    id: str
    name: str
    spec: CreateVM
    slot: int
    state: str = "starting"
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    error: Optional[str] = None
    vm: object = None

    def view(self) -> VMView:
        return VMView(
            id=self.id, name=self.name, state=self.state,
            vcpus=self.spec.vcpus, mem_mib=self.spec.mem_mib, disk_gb=self.spec.disk_gb,
            ip=getattr(self.vm, "ip", None), gateway=getattr(self.vm, "gateway", None),
            created_at=self.created_at, boot_ms=getattr(self.vm, "boot_ms", None),
            expires_at=self.expires_at, error=self.error,
            terminal_url=self.terminal_url,
            image=self.spec.image, image_label=self.image_label,
            # Stopping only makes sense for something that is not on a timer:
            # a VM that will be destroyed at expiry gains nothing from being
            # paused, and pausing it would silently outlive its own deadline.
            can_suspend=self.spec.ttl == "never",
        )

    image_label: Optional[str] = None

    @property
    def terminal_url(self) -> Optional[str]:
        """Same-origin path to this playground's ttyd, when ttyd is serving.

        A path rather than a URL on purpose: the proxy keeps everything on the
        one port, which is what makes it reachable through Codespaces port
        forwarding.
        """
        from . import ttyd
        sess = ttyd.get(self.id)
        return sess.base_path + "/" if sess and sess.alive else None


class Manager:
    def __init__(self, backend: Optional[str] = None):
        self.backend = backend or resolve_backend()
        self.mock = self.backend == "mock"
        self._vms: dict[str, Record] = {}
        self._slots: set[int] = set()
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ probe
    def min_disk_gb(self) -> int:
        """A playground disk can never be smaller than the image it clones."""
        if self.backend == "container":
            return C.DISK_MIN_GB      # containers share the host filesystem
        if self.backend == "qemu":
            return C.base_image_gb()
        if self.backend == "firecracker" and C.BASE_ROOTFS.exists():
            gb = -(-C.BASE_ROOTFS.stat().st_size // (1024 ** 3))
            return max(C.DISK_MIN_GB, int(gb))
        return C.DISK_MIN_GB

    def host_info(self) -> HostInfo:
        problems: list[str] = []
        notes: list[str] = []
        kvm = has_kvm()
        root = hasattr(os, "geteuid") and os.geteuid() == 0
        fc_version = qemu_version = accel = None

        if self.backend == "mock":
            problems.append("Mock mode: VMs are simulated. No kernel, no filesystem, no network.")

        elif self.backend == "container":
            from .container import docker_version, has_docker, image_present
            if not has_docker():
                problems.append("Docker is not available or its daemon is not running.")
            else:
                qemu_version = docker_version()
                if not image_present():
                    problems.append("Playground image %s is not built. Run: "
                                    "docker build -t %s docker/"
                                    % (C.CONTAINER_IMAGE, C.CONTAINER_IMAGE))
            accel = "native"
            notes.append("Containers run directly on the host CPU: native speed and "
                         "millisecond startup. They share the host kernel, so this is "
                         "weaker isolation than a VM -- fine for scratch work, not for "
                         "code you actively distrust.")
            if C.CONTAINER_POOL:
                notes.append("A warm pool of %d keeps launches instant."
                             % C.CONTAINER_POOL)

        elif self.backend == "qemu":
            from .qemu import detect_accelerator, find_binary
            qemu_bin = find_binary(C.QEMU_SYSTEM)
            if not qemu_bin:
                problems.append("QEMU not found. Run tools/get-qemu.ps1 (no admin rights needed).")
            else:
                with contextlib.suppress(Exception):
                    out = subprocess.run([qemu_bin, "--version"], capture_output=True,
                                         text=True, timeout=15)
                    qemu_version = (out.stdout or out.stderr).strip().splitlines()[0]
                accel = detect_accelerator(qemu_bin)
                if accel == "tcg":
                    notes.append("Running under TCG software emulation: correct but slow, "
                                 "roughly 5-10x slower than hardware acceleration. "
                                 "Boots take tens of seconds.")
                else:
                    notes.append("Hardware acceleration active via %s." % accel)
            if not C.BASE_IMAGE.exists():
                problems.append("Golden image missing at %s -- run: python tools/bake.py"
                                % C.BASE_IMAGE)

        else:  # firecracker
            if net.icmp_state() is False:
                notes.append("This host cannot send ICMP -- ping will not work inside "
                             "a playground, and no firewall rule can change that. "
                             "TCP (apt, curl, git, ssh) is unaffected. Common on "
                             "Azure and therefore in Codespaces.")
            if not kvm:
                problems.append("/dev/kvm is missing or not writable -- Firecracker cannot start.")
            if not Path(C.FIRECRACKER_BIN).exists():
                problems.append("firecracker binary not found at " + str(C.FIRECRACKER_BIN))
            else:
                with contextlib.suppress(Exception):
                    out = subprocess.run([C.FIRECRACKER_BIN, "--version"],
                                         capture_output=True, text=True, timeout=5)
                    fc_version = (out.stdout or out.stderr).strip().splitlines()[0]
            if not C.KERNEL_PATH.exists():
                problems.append("guest kernel missing at " + str(C.KERNEL_PATH))
            if not C.BASE_ROOTFS.exists():
                problems.append("base rootfs missing at " + str(C.BASE_ROOTFS))
            if not root:
                problems.append("not running as root -- tap devices and NAT rules will fail.")
            for tool in ("ip", "iptables", "resize2fs"):
                if shutil.which(tool) is None:
                    problems.append("missing host tool: " + tool)
            accel = "kvm" if kvm else None

        if os.name != "nt" and C.USE_TTYD != "off" and not _ttyd.available():
            notes.append("ttyd is not installed, so the built-in terminal is the only "
                         "option. Install it for the second choice: "
                         "sudo bash tools/get-ttyd.sh")

        return HostInfo(
            mode=self.backend,
            kvm=kvm, firecracker=fc_version, qemu=qemu_version, accel=accel,
            kernel=self.backend != "firecracker" or C.KERNEL_PATH.exists(),
            rootfs=self.backend != "firecracker" or C.BASE_ROOTFS.exists(),
            image=self.backend != "qemu" or C.BASE_IMAGE.exists(),
            root=root, max_vms=C.MAX_VMS,
            limits={
                "vcpus": [C.VCPU_MIN, C.VCPU_MAX],
                "mem_mib": [C.MEM_MIN_MIB, C.MEM_MAX_MIB],
                "disk_gb": [self.min_disk_gb(), C.DISK_MAX_GB],
            },
            defaults={
                "vcpus": C.DEFAULT_VCPUS, "mem_mib": C.DEFAULT_MEM_MIB,
                "disk_gb": max(C.DEFAULT_DISK_GB, self.min_disk_gb()),
            },
            problems=problems, notes=notes,
            terminal="ttyd" if _ttyd.available() else "builtin",
            ttyd=_ttyd.version(),
        )

    # ----------------------------------------------------------------- create
    def _alloc_slot(self) -> int:
        for s in range(1, C.MAX_VMS + 1):
            if s not in self._slots:
                self._slots.add(s)
                return s
        raise RuntimeError("no free slots (MVMP_MAX_VMS=%d)" % C.MAX_VMS)

    def _make_vm(self, vm_id: str, slot: int, spec: CreateVM, name: str):
        disk = max(spec.disk_gb, self.min_disk_gb())
        img = catalog.resolve(self.backend, spec.image)
        if self.backend == "mock":
            from .mock import MockVM
            return MockVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name)
        if self.backend == "container":
            from .container import ContainerVM
            return ContainerVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img)
        if self.backend == "qemu":
            from .qemu import QemuVM
            return QemuVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img)
        from .firecracker import FirecrackerVM
        return FirecrackerVM(vm_id, slot, spec.vcpus, spec.mem_mib, disk, name, img)

    def _image_label(self, image_id: str) -> Optional[str]:
        return catalog.label_for(self.backend, image_id)

    async def create(self, spec: CreateVM) -> Record:
        async with self._lock:
            if len(self._vms) >= C.MAX_VMS:
                raise RuntimeError("playground limit reached (%d)" % C.MAX_VMS)
            slot = self._alloc_slot()

        vm_id = uuid.uuid4().hex[:12]
        name = spec.name or _friendly_name()
        ttl = TTL_CHOICES[spec.ttl]
        rec = Record(id=vm_id, name=name, spec=spec, slot=slot,
                     expires_at=(time.time() + ttl) if ttl else None)
        self._vms[vm_id] = rec

        try:
            rec.image_label = self._image_label(spec.image)
            rec.vm = self._make_vm(vm_id, slot, spec, name)
            await rec.vm.start()
            rec.state = "running"
            # ttyd serves the terminal where it exists; the built-in console
            # websocket stays as the fallback (and as ttyd's own transport).
            from . import ttyd
            await asyncio.to_thread(ttyd.start_for, vm_id, C.RUNTIME_PORT)
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
            with contextlib.suppress(Exception):
                if rec.vm is not None:
                    await rec.vm.stop(graceful=False)
            self._slots.discard(slot)
        return rec

    # ------------------------------------------------------- rename / suspend
    async def rename(self, vm_id: str, name: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None:
            return None
        rec.name = name
        # The guest's own hostname was fixed at boot and cannot follow; this is
        # the label you see in the UI.
        with contextlib.suppress(Exception):
            if rec.vm is not None:
                rec.vm.name = name
        return rec

    async def suspend(self, vm_id: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None or rec.vm is None:
            return None
        if rec.state != "running":
            return rec
        if not rec.view().can_suspend:
            raise RuntimeError("only playgrounds with auto-destroy set to 'never' "
                               "can be stopped -- one on a timer would outlive it")
        rec.state = "stopping"
        from . import ttyd
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ttyd.stop_for, vm_id)
        try:
            await rec.vm.suspend()
            rec.state = "stopped"
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
        return rec

    async def resume(self, vm_id: str) -> Optional[Record]:
        rec = self._vms.get(vm_id)
        if rec is None or rec.vm is None:
            return None
        if rec.state == "running":
            return rec
        rec.state = "starting"
        rec.error = None
        try:
            await rec.vm.resume()
            rec.state = "running"
            from . import ttyd
            await asyncio.to_thread(ttyd.start_for, vm_id, C.RUNTIME_PORT)
        except Exception as exc:
            rec.state = "error"
            rec.error = str(exc)
        return rec

    # ---------------------------------------------------------------- destroy
    async def destroy(self, vm_id: str) -> bool:
        rec = self._vms.get(vm_id)
        if rec is None:
            return False
        rec.state = "stopping"
        from . import ttyd
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ttyd.stop_for, vm_id)
        with contextlib.suppress(Exception):
            if rec.vm is not None:
                await rec.vm.stop()
        self._vms.pop(vm_id, None)
        self._slots.discard(rec.slot)
        return True

    def get(self, vm_id: str) -> Optional[Record]:
        return self._vms.get(vm_id)

    def list(self) -> list[VMView]:
        for rec in self._vms.values():
            if rec.state == "running" and rec.vm is not None and not rec.vm.alive:
                rec.state = "stopped"
        return [r.view() for r in sorted(self._vms.values(), key=lambda r: r.created_at)]

    # ----------------------------------------------------------------- reaper
    async def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())
        if self.backend == "container":
            from .container import cleanup_orphans, pool
            await cleanup_orphans()      # containers a previous run left behind
            pool().refill_soon()
        if self.backend == "firecracker":
            # Off the startup path: the probe costs a couple of seconds and
            # nothing needs the answer until someone asks why ping fails.
            asyncio.create_task(asyncio.to_thread(net.probe_icmp))

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                now = time.time()
                expired = [r.id for r in self._vms.values()
                           if r.expires_at and now >= r.expires_at]
                for vm_id in expired:
                    await self.destroy(vm_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        from . import ttyd
        with contextlib.suppress(Exception):
            ttyd.stop_all()
        for vm_id in list(self._vms):
            await self.destroy(vm_id)
        if self.backend == "container":
            from .container import pool
            await pool().drain()         # do not leak the warm pool
