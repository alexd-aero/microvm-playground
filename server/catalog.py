"""What operating systems this host can actually launch.

Two sources, depending on the backend:

* container -- a curated list of OS images. The project's own image carries the
  full toolset; the stock ones are plain upstream distributions and are pulled
  on first use, which is slow exactly once.
* qemu / firecracker -- whatever disk images are sitting in the images
  directory. Dropping another qcow2 or ext4 in there makes it appear in the UI
  with no code change, which is the point.

`ready` distinguishes "you can have this now" from "this will be fetched
first", so the UI can say so instead of appearing to hang.
"""
import contextlib
import subprocess
from typing import Optional

from . import config as C

# id -> (label, image ref, note)
CONTAINER_IMAGES: dict[str, tuple[str, str, str]] = {
    "default": ("Debian 12 · full toolset", C.CONTAINER_IMAGE,
                "git, curl, wget, neofetch, python3, build-essential"),
    "debian":  ("Debian 12 · stock", "debian:bookworm", "upstream image, apt only"),
    "ubuntu":  ("Ubuntu 24.04", "ubuntu:24.04", "upstream image, apt only"),
    "alpine":  ("Alpine 3.21", "alpine:3.21", "tiny, apk, ash instead of bash"),
    "fedora":  ("Fedora 41", "fedora:41", "upstream image, dnf"),
    "arch":    ("Arch Linux", "archlinux:latest", "upstream image, pacman"),
    "rocky":   ("Rocky Linux 9", "rockylinux:9", "upstream image, dnf"),
}

DEFAULT_ID = "default"


def _docker_has(ref: str) -> bool:
    from .container import docker_bin
    exe = docker_bin()
    if not exe:
        return False
    with contextlib.suppress(Exception):
        p = subprocess.run([exe, "image", "inspect", ref],
                           capture_output=True, text=True, timeout=20)
        return p.returncode == 0
    return False


def _disk_images(pattern: str, default_name: str) -> list[dict]:
    out: list[dict] = []
    if not C.IMAGES_DIR.exists():
        return out
    for path in sorted(C.IMAGES_DIR.glob(pattern)):
        stem = path.stem
        out.append({
            "id": "default" if path.name == default_name else stem,
            "label": "%s · %s" % (stem, _human_size(path)),
            "ref": str(path),
            "ready": True,
            "note": "default image" if path.name == default_name else "found in images/",
        })
    # The default should lead the list.
    out.sort(key=lambda i: (i["id"] != "default", i["label"]))
    return out


def _human_size(path) -> str:
    with contextlib.suppress(Exception):
        n = path.stat().st_size
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return "%.0f %s" % (n, unit)
            n /= 1024.0
        return "%.1f TB" % n
    return "?"


def list_images(backend: str) -> list[dict]:
    if backend == "container":
        items = []
        for key, (label, ref, note) in CONTAINER_IMAGES.items():
            ready = _docker_has(ref)
            items.append({
                "id": key, "label": label, "ref": ref, "ready": ready,
                "note": note if ready else note + " · pulled on first use",
            })
        return items
    if backend == "qemu":
        return _disk_images("*.qcow2", C.BASE_IMAGE.name)
    if backend == "firecracker":
        return _disk_images("*.ext4", C.BASE_ROOTFS.name)
    return [{"id": "default", "label": "simulated", "ref": "-", "ready": True,
             "note": "mock mode has no operating system"}]


def resolve(backend: str, image_id: Optional[str]) -> Optional[str]:
    """Turn a catalogue id into something the backend can launch.

    Unknown ids fall back to the default rather than failing: the catalogue can
    change between the page loading and the request arriving.
    """
    if not image_id or image_id == DEFAULT_ID:
        return None                      # the backend's own default
    if backend == "container":
        entry = CONTAINER_IMAGES.get(image_id)
        return entry[1] if entry else None
    for item in list_images(backend):
        if item["id"] == image_id:
            return item["ref"]
    return None
