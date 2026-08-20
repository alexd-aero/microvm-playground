"""Central configuration. Everything is overridable by environment variable."""
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

WINDOWS = os.name == "nt"


def _p(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


# --- filesystem layout -------------------------------------------------------
def _default_state_dir() -> str:
    """Somewhere we can actually write.

    /var/lib means nothing on Windows, and on Linux it is only writable by
    root -- which is wrong for the container and QEMU backends, since neither
    needs privileges. Fall back to the user's own data directory rather than
    crashing at startup with EACCES.
    """
    if WINDOWS:
        return os.path.expandvars(r"%LOCALAPPDATA%\mvmp")
    if os.access("/var/lib", os.W_OK):
        return "/var/lib/mvmp"
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(xdg, "mvmp")


STATE_DIR = _p("MVMP_STATE_DIR", _default_state_dir())
IMAGES_DIR = STATE_DIR / "images"
VMS_DIR = STATE_DIR / "vms"

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# --- backend -----------------------------------------------------------------
# auto -> firecracker when /dev/kvm exists, else qemu. mock is opt-in only and
# is never selected automatically: it is a UI demo, not a machine.
BACKEND = os.environ.get("MVMP_BACKEND", "auto").lower()

# firecracker assets
KERNEL_PATH = _p("MVMP_KERNEL", str(IMAGES_DIR / "vmlinux"))
BASE_ROOTFS = _p("MVMP_ROOTFS", str(IMAGES_DIR / "rootfs.ext4"))
FIRECRACKER_BIN = os.environ.get(
    "MVMP_FIRECRACKER", shutil.which("firecracker") or "/usr/local/bin/firecracker")

# qemu assets
QEMU_SYSTEM = os.environ.get("MVMP_QEMU", "qemu-system-x86_64")
QEMU_ACCEL = os.environ.get("MVMP_ACCEL", "")     # "" = autodetect
BASE_IMAGE = _p("MVMP_IMAGE", str(IMAGES_DIR / "golden.qcow2"))
SHUTDOWN_WAIT_S = float(os.environ.get("MVMP_SHUTDOWN_WAIT", "20"))

# container assets
DOCKER_BIN = os.environ.get("MVMP_DOCKER", "docker")
CONTAINER_IMAGE = os.environ.get("MVMP_IMAGE_TAG", "mvmp-playground:latest")
# Containers kept started ahead of time so a launch costs an exec, not a
# create+start. This is what makes startup land in the milliseconds.
CONTAINER_POOL = max(0, int(os.environ.get("MVMP_POOL", "2")))
CONTAINER_PIDS_LIMIT = int(os.environ.get("MVMP_PIDS_LIMIT", "512"))

# --- terminal ----------------------------------------------------------------
# ttyd serves the terminal wherever it is installed. Windows keeps the built-in
# xterm.js console, because ttyd's bridge needs a POSIX pty.
TTYD_BIN = os.environ.get("MVMP_TTYD", "ttyd")
TTYD_MAX_CLIENTS = int(os.environ.get("MVMP_TTYD_CLIENTS", "8"))
USE_TTYD = os.environ.get("MVMP_TTYD_ENABLE", "auto").lower()   # auto | on | off

# Set at startup so ttyd can point its bridge back at us.
RUNTIME_PORT = int(os.environ.get("MVMP_PORT", "8080"))

# --- server ------------------------------------------------------------------
# Codespaces forwards ports from inside the container, so bind all interfaces
# there. The forwarded port is private to the user unless they publish it.
IN_CODESPACE = os.environ.get("CODESPACES", "").lower() == "true"
BIND_HOST = os.environ.get("MVMP_HOST", "0.0.0.0" if IN_CODESPACE else "127.0.0.1")
BIND_PORT = int(os.environ.get("MVMP_PORT", "8080"))

# --- networking (firecracker only; qemu uses user-mode SLIRP) ----------------
SUBNET_BASE = os.environ.get("MVMP_SUBNET_BASE", "172.16")
TAP_PREFIX = os.environ.get("MVMP_TAP_PREFIX", "fctap")
GUEST_DNS = os.environ.get("MVMP_DNS", "1.1.1.1 8.8.8.8")
EGRESS_IFACE = os.environ.get("MVMP_EGRESS", "")

# --- limits ------------------------------------------------------------------
# One /30 per slot means the slot number is an octet: 254 is a hard ceiling.
MAX_VMS = max(1, min(int(os.environ.get("MVMP_MAX_VMS", "32")), 254))
VCPU_MIN, VCPU_MAX = 1, 8
MEM_MIN_MIB, MEM_MAX_MIB = 128, 8192
DISK_MIN_GB, DISK_MAX_GB = 1, 40

DEFAULT_VCPUS = 2
DEFAULT_MEM_MIB = 1024
DEFAULT_DISK_GB = 8

# --- mode --------------------------------------------------------------------
MOCK = os.environ.get("MVMP_MOCK", "").lower() in ("1", "true", "yes", "on")

BOOT_ARGS_BASE = (
    "console=ttyS0 reboot=k panic=1 pci=off nomodule "
    "8250.nr_uarts=1 i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd "
    "random.trust_cpu=on root=/dev/vda rw"
)

def find_qemu_binary(name: str) -> Optional[str]:
    """Locate a QEMU executable. Single source of truth for the search order:
    the portable install this project provisioned wins over anything else."""
    exe = name + (".exe" if WINDOWS else "")
    candidates = [STATE_DIR / "qemu" / exe]
    on_path = shutil.which(exe)
    if on_path:
        candidates.append(Path(on_path))
    candidates += [Path(b) / exe for b in (
        r"C:\Program Files\qemu", r"C:\Program Files (x86)\qemu",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\qemu"),
        "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


_image_gb_cache: Optional[int] = None


def base_image_gb() -> int:
    """Virtual size of the golden qcow2, in GB (rounded up).

    A playground disk can never be smaller than this: an overlay inherits its
    backing file's virtual size and qcow2 cannot shrink one.
    """
    global _image_gb_cache
    if _image_gb_cache is not None:
        return _image_gb_cache
    size = 0
    qemu_img = find_qemu_binary("qemu-img")
    if qemu_img and BASE_IMAGE.exists():
        try:
            out = subprocess.run([qemu_img, "info", "--output=json", str(BASE_IMAGE)],
                                 capture_output=True, text=True, timeout=20)
            size = json.loads(out.stdout).get("virtual-size", 0)
        except Exception:
            size = 0
    gb = max(DISK_MIN_GB, -(-size // (1024 ** 3)) if size else DISK_MIN_GB)
    _image_gb_cache = int(gb)
    return _image_gb_cache
