#!/usr/bin/env python3
"""Bake the golden guest image.

Boots the stock Debian cloud image once with a cloud-init seed that installs a
real userland, sets up autologin on the serial console, and then disables
cloud-init so every later boot goes straight to a shell. The result is the
image every playground is cloned from.

    python tools/bake.py                     # fetch + bake, defaults
    python tools/bake.py --packages "git curl jq"
    python tools/bake.py --disk-gb 12 --force

This is the slow step: under TCG software emulation, apt inside an emulated
CPU takes a while. It happens once.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config as C          # noqa: E402
from server.qemu import find_binary     # noqa: E402

DEBIAN_URL = ("https://cloud.debian.org/images/cloud/bookworm/latest/"
              "debian-12-genericcloud-amd64.qcow2")

# Kept deliberately lean: every package is installed inside an emulated CPU, so
# the bake time is roughly proportional to this list. build-essential and
# python3-pip are the obvious additions once you have a working image --
#   python tools/bake.py --force --packages "... build-essential"
DEFAULT_PACKAGES = ("git curl wget neofetch htop vim nano tmux jq unzip zip "
                    "ca-certificates python3 less tree file "
                    "iputils-ping dnsutils net-tools")

# Arch publishes a cloud image with cloud-init, so the same bake works -- only
# the package names differ. neofetch was dropped from the Arch repositories
# after upstream archived it; fastfetch is the maintained replacement.
ARCH_URL = ("https://geo.mirror.pkgbuild.com/images/latest/"
            "Arch-Linux-x86_64-cloudimg.qcow2")
ARCH_PACKAGES = ("git curl wget fastfetch htop vim nano tmux jq unzip zip "
                 "ca-certificates python less tree file "
                 "iputils bind inetutils net-tools base-devel")

DISTROS = {
    "debian": {"url": DEBIAN_URL, "packages": DEFAULT_PACKAGES,
               "base": "debian-base.qcow2", "out": None},
    "arch":   {"url": ARCH_URL, "packages": ARCH_PACKAGES,
               "base": "arch-base.qcow2", "out": "arch.qcow2"},
}

# cloud-init's NoCloud datasource reads these two files off a volume labelled
# CIDATA. %I below is a systemd specifier, not a Python placeholder.
USER_DATA = """#cloud-config
hostname: playground
manage_etc_hosts: true
disable_root: false
ssh_pwauth: true

users:
  - name: root
    lock_passwd: false

chpasswd:
  expire: false
  users:
    - name: root
      password: playground
      type: text

write_files:
  - path: /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
    permissions: '0644'
    content: |
      [Service]
      ExecStart=
      ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I xterm-256color
      Type=idle

  - path: /etc/profile.d/00-mvmp.sh
    permissions: '0644'
    content: |
      export LANG=C.UTF-8
      export LC_ALL=C.UTF-8
      export TERM=xterm-256color
      export COLORTERM=truecolor
      export PAGER=less
      export LESS="-R"
      export PS1='\\[\\e[1;38;5;48m\\]\\u@\\h\\[\\e[0m\\]:\\[\\e[1;38;5;75m\\]\\w\\[\\e[0m\\]# '
      alias ls='ls --color=auto'
      alias grep='grep --color=auto'
      alias ll='ls -alF --color=auto'
      # A serial line carries no SIGWINCH: ask the terminal where the cursor
      # lands after a huge move, which reveals the real window size.
      mvmp_resize() {
        local old rows cols
        old=$(stty -g 2>/dev/null) || return 0
        stty raw -echo min 0 time 10 2>/dev/null
        printf '\\033[s\\033[999;999H\\033[6n' > /dev/tty
        IFS='[;R' read -r _ rows cols < /dev/tty
        printf '\\033[u' > /dev/tty
        stty "$old" 2>/dev/null
        case "$rows$cols" in *[!0-9]*|"") return 0 ;; esac
        stty rows "$rows" cols "$cols" 2>/dev/null
      }
      mvmp_resize

runcmd:
__MOTD__
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, "serial-getty@ttyS0.service" ]
  - [ sed, -i, 's/^#*PrintMotd.*/PrintMotd yes/', /etc/ssh/sshd_config ]
  - export DEBIAN_FRONTEND=noninteractive; apt-get update -q
  - export DEBIAN_FRONTEND=noninteractive; apt-get install -y -q --no-install-recommends __PACKAGES__
  - [ apt-get, clean ]
  - [ passwd, -d, root ]
  - [ systemctl, mask, "systemd-networkd-wait-online.service" ]
  - [ touch, /etc/cloud/cloud-init.disabled ]
  - [ sh, -c, "echo BAKE-COMPLETE > /dev/ttyS0" ]

power_state:
  mode: poweroff
  timeout: 60
  condition: true
"""

META_DATA = "instance-id: mvmp-bake\nlocal-hostname: playground\n"

# /etc/motd is displayed with `cat`, which does not interpret escapes -- so the
# escapes have to be real bytes in the file. printf writes them; a cloud-init
# write_files block would store the literal text "\e[38;5;51m" instead.
_MOTD_ROWS = [
    r"",
    r"  \033[38;5;51m╭────────────────────────────────────────────────╮\033[0m",
    r"  \033[38;5;51m│\033[0m\033[1;97m   microvm playground · QEMU · disposable       \033[0m\033[38;5;51m│\033[0m",
    r"  \033[38;5;51m╰────────────────────────────────────────────────╯\033[0m",
    r"",
    r"   \033[90mDebian 12 · UTF-8 · truecolor · full outbound internet\033[0m",
    r"   \033[33m⚠\033[0m  \033[90mThis VM and its disk are destroyed on shutdown.\033[0m",
    r"",
]


def _motd_commands() -> str:
    """runcmd lines that build /etc/motd one printf at a time."""
    out = []
    for i, row in enumerate(_MOTD_ROWS):
        redirect = ">" if i == 0 else ">>"
        out.append("  - printf '%s\\n' %s /etc/motd" % (row, redirect))
    # Debian's dynamic MOTD would print on top of ours.
    out.append("  - rm -f /etc/update-motd.d/10-uname")
    return "\n".join(out)


def log(msg, colour="36"):
    print("\033[1;%sm==>\033[0m %s" % (colour, msg), flush=True)


def build_seed(path: Path, packages: str) -> None:
    """A tiny ISO9660 volume labelled CIDATA -- cloud-init's NoCloud source."""
    import pycdlib

    user = (USER_DATA.replace("__PACKAGES__", packages)
                     .replace("__MOTD__", _motd_commands()).encode("utf-8"))
    meta = META_DATA.encode("utf-8")

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
    for blob, iso_name, name in ((user, "/USERDATA.;1", "user-data"),
                                 (meta, "/METADATA.;1", "meta-data")):
        iso.add_fp(io.BytesIO(blob), len(blob), iso_name,
                   rr_name=name, joliet_path="/" + name)
    iso.write(str(path))
    iso.close()


def fetch_base(dest: Path, url: str) -> None:
    if dest.exists():
        log("base image already downloaded (%.0f MB)" % (dest.stat().st_size / 2**20))
        return
    log("downloading the Debian cloud image (~350 MB)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.replace(dest)
    log("downloaded %.0f MB" % (dest.stat().st_size / 2**20))


def main() -> int:
    ap = argparse.ArgumentParser(description="bake the golden playground image")
    ap.add_argument("--disk-gb", type=int, default=8)
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--vcpus", type=int, default=max(2, min(4, os.cpu_count() or 2)))
    ap.add_argument("--distro", choices=sorted(DISTROS), default="debian",
                    help="debian is the default image; arch is offered alongside it")
    ap.add_argument("--packages", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--out", default=None,
                    help="output filename inside the images directory")
    ap.add_argument("--timeout", type=int, default=5400)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    spec = DISTROS[args.distro]
    if args.packages is None:
        args.packages = spec["packages"]
    if args.url is None:
        args.url = spec["url"]

    qemu = find_binary(C.QEMU_SYSTEM)
    qemu_img = find_binary("qemu-img")
    if not qemu or not qemu_img:
        print("QEMU not found. Run tools/get-qemu.ps1 first.", file=sys.stderr)
        return 1

    C.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    base = C.IMAGES_DIR / spec["base"]
    # Non-default distros are written beside the golden image under their own
    # name. The catalogue lists every qcow2 it finds, so a baked arch.qcow2
    # simply appears in the OS dropdown with no further wiring.
    out_name = args.out or spec["out"]
    golden = (C.IMAGES_DIR / out_name) if out_name else C.BASE_IMAGE
    seed = C.IMAGES_DIR / ("seed-%s.iso" % args.distro)
    serial_log = C.IMAGES_DIR / ("bake-%s-serial.log" % args.distro)

    if golden.exists() and not args.force:
        log("%s image already built at %s (use --force to rebuild)"
            % (args.distro, golden), "32")
        return 0

    fetch_base(base, args.url)

    log("preparing a %d GB working copy" % args.disk_gb)
    if golden.exists():
        golden.unlink()
    shutil.copyfile(base, golden)
    subprocess.run([qemu_img, "resize", "-q", str(golden), "%dG" % args.disk_gb], check=True)

    log("building the cloud-init seed")
    build_seed(seed, args.packages)

    log("baking: booting the guest to install %d packages" % len(args.packages.split()))
    log("this is the slow part -- TCG emulates every instruction. Grab a coffee.", "33")

    argv = [
        qemu, "-display", "none", "-machine", "q35",
        "-accel", "tcg,thread=multi" if args.vcpus > 1 else "tcg",
        "-cpu", "max", "-smp", str(args.vcpus), "-m", str(args.mem_mib),
        "-drive", "file=%s,if=virtio,format=qcow2,cache=writeback" % golden,
        "-drive", "file=%s,if=virtio,format=raw,readonly=on" % seed,
        "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
        "-object", "rng-builtin,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
        "-serial", "file:%s" % serial_log,
        "-no-reboot",
    ]

    t0 = time.monotonic()
    proc = subprocess.Popen(argv)
    last = 0
    try:
        while proc.poll() is None:
            time.sleep(5)
            elapsed = time.monotonic() - t0
            if elapsed > args.timeout:
                proc.kill()
                print("\nbake timed out after %d s" % args.timeout, file=sys.stderr)
                return 1
            if serial_log.exists():
                size = serial_log.stat().st_size
                if size != last:
                    last = size
                    tail = serial_log.read_bytes()[-400:].decode("utf-8", "replace")
                    marker = [l for l in tail.splitlines() if l.strip()]
                    note = marker[-1][:90] if marker else ""
                    print("\r    %4ds  %6d KB  %s" % (elapsed, size // 1024, note.ljust(90)[:90]),
                          end="", flush=True)
    except KeyboardInterrupt:
        proc.kill()
        return 130

    print()
    took = time.monotonic() - t0
    body = serial_log.read_bytes().decode("utf-8", "replace") if serial_log.exists() else ""
    if "BAKE-COMPLETE" not in body:
        print("\033[1;31mbake did not report completion\033[0m -- see %s" % serial_log,
              file=sys.stderr)
        return 1

    seed.unlink(missing_ok=True)
    log("golden image ready in %.0f s: %s (%.0f MB on disk)"
        % (took, golden, golden.stat().st_size / 2**20), "32")
    return 0


if __name__ == "__main__":
    sys.exit(main())
