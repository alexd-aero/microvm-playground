"""Per-VM disk provisioning: copy the golden rootfs, grow it to the requested size."""
import shutil
import subprocess
from pathlib import Path

from . import config as C


class ImageError(RuntimeError):
    pass


def _run(*args: str) -> None:
    p = subprocess.run(args, capture_output=True, text=True)
    # e2fsck exits 1/2 when it *fixed* something -- that is success for us.
    ok = {0} if args[0] != "e2fsck" else {0, 1, 2}
    if p.returncode not in ok:
        raise ImageError(f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")


def base_size_bytes() -> int:
    return C.BASE_ROOTFS.stat().st_size


def provision(dest: Path, disk_gb: int) -> None:
    """Sparse-copy the base image and resize the filesystem to fill it."""
    if not C.BASE_ROOTFS.exists():
        raise ImageError(f"base rootfs missing: {C.BASE_ROOTFS} (run setup.sh)")

    target = disk_gb * 1024 * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)

    # --sparse=always keeps the copy cheap; the base is mostly holes.
    subprocess.run(
        ["cp", "--sparse=always", str(C.BASE_ROOTFS), str(dest)],
        capture_output=True, text=True, check=True,
    )

    if target > dest.stat().st_size:
        _run("truncate", "-s", str(target), str(dest))
        _run("e2fsck", "-f", "-p", str(dest))
        _run("resize2fs", str(dest))


def destroy(vm_dir: Path) -> None:
    shutil.rmtree(vm_dir, ignore_errors=True)
