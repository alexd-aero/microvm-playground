#!/usr/bin/env bash
# One-time host provisioning for the microVM playground.
#   sudo ./setup.sh                     Debian guest (apt, systemd) -- the default
#   sudo DISTRO=alpine ./setup.sh       Alpine guest (apk, ~4x faster boot)
#
# Installs the Firecracker binary, a guest kernel, and builds the golden rootfs
# that every playground is cloned from.
set -euo pipefail

STATE_DIR=${MVMP_STATE_DIR:-/var/lib/mvmp}
IMAGES="$STATE_DIR/images"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH=$(uname -m)

DISTRO=${DISTRO:-debian}
SUITE=${SUITE:-bookworm}                 # neofetch exists in bookworm; dropped in trixie
DEB_MIRROR=${DEB_MIRROR:-http://deb.debian.org/debian}
ALPINE_BRANCH=${ALPINE_BRANCH:-v3.21}
ALPINE_VER=${ALPINE_VER:-3.21.3}

# The full userland you actually want in a playground.
DEB_PKGS=${DEB_PKGS:-"systemd-sysv,udev,dbus,iproute2,iputils-ping,net-tools,\
dnsutils,ca-certificates,curl,wget,git,openssh-client,rsync,vim,nano,less,\
htop,tmux,jq,unzip,zip,tar,gzip,file,tree,procps,psmisc,sudo,locales,\
bash-completion,python3,python3-pip,python3-venv,build-essential,man-db"}
# Installed after the bootstrap so a missing one cannot fail the whole build.
DEB_EXTRA=${DEB_EXTRA:-"neofetch lsb-release ncdu bsdextrautils"}

ALPINE_PKGS=${ALPINE_PKGS:-"bash coreutils util-linux ncurses ncurses-terminfo curl wget \
ca-certificates openssh-client git nano vim htop tmux jq bind-tools iproute2 tzdata \
python3 py3-pip file less findutils grep sed tar gzip neofetch sudo build-base"}

ARCH_PKGS=${ARCH_PKGS:-"base systemd bash coreutils util-linux procps-ng git curl wget openssh rsync vim nano less tree file jq unzip zip tar gzip htop tmux fastfetch sudo which python iputils inetutils bind net-tools iproute2 base-devel"}

# Debian and Arch carry a full userland; Alpine does not.
case "$DISTRO" in
  debian) ROOTFS_MB=${ROOTFS_MB:-3072} ;;
  arch)   ROOTFS_MB=${ROOTFS_MB:-4096} ;;
  *)      ROOTFS_MB=${ROOTFS_MB:-1024} ;;
esac

# Debian and Alpine build *the* default rootfs. Arch is an additional option, so
# it is written under its own name -- the catalogue lists every ext4 it finds,
# which is what makes it appear in the OS dropdown with no further wiring.
if [ "$DISTRO" = "arch" ]; then ROOTFS_OUT=rootfs-arch.ext4; else ROOTFS_OUT=rootfs.ext4; fi

c()    { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
step() { c '1;36' "==> $1"; }
ok()   { c '32' "    ok  $1"; }
warn() { c '33' "    !   $1"; }
die()  { c '1;31' "    x   $1"; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
step "Checking the host"
[ "$(uname -s)" = "Linux" ] || die "This must run on Linux. On Windows use WSL2 (see README)."
[ "$(id -u)" = "0" ] || die "Run as root: sudo ./setup.sh"
[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "aarch64" ] || die "Unsupported arch: $ARCH"
case "$DISTRO" in debian|alpine|arch) ;; *) die "DISTRO must be debian, alpine or arch" ;; esac

if [ -e /dev/kvm ] && [ -w /dev/kvm ]; then
  ok "/dev/kvm present and writable"
else
  warn "/dev/kvm missing or not writable."
  warn "On WSL2 this means nested virtualization is off. In C:\\Users\\<you>\\.wslconfig:"
  warn "    [wsl2]"
  warn "    nestedVirtualization=true"
  warn "then run 'wsl --shutdown' in PowerShell and reopen the distro."
fi

step "Installing host packages"
HOST_PKGS="curl ca-certificates iproute2 iptables e2fsprogs python3 python3-venv python3-pip"
[ "$DISTRO" = "debian" ] && HOST_PKGS="$HOST_PKGS debootstrap"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq $HOST_PKGS >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q curl ca-certificates iproute iptables e2fsprogs python3 python3-pip debootstrap
elif command -v apk >/dev/null; then
  apk add --no-cache curl ca-certificates iproute2 iptables e2fsprogs python3 py3-pip debootstrap
else
  warn "Unknown package manager; ensure curl, iproute2, iptables, e2fsprogs, python3 exist."
fi
[ "$DISTRO" != "debian" ] || command -v debootstrap >/dev/null || die "debootstrap is required for DISTRO=debian"
ok "host packages"

mkdir -p "$IMAGES" "$STATE_DIR/vms"

# ── ttyd (the second terminal) ───────────────────────────────────────────────
# Deliberately early: it takes seconds, while the rootfs build below takes
# minutes. Installing it last meant that abandoning a slow build also skipped
# ttyd, and the terminal chooser then silently offered only one option.
# No sudo needed -- this script already requires root, and get-ttyd.sh skips
# sudo when it is already uid 0.
step "Installing ttyd"
TTYD_LOG=$(mktemp)
if bash "$HERE/tools/get-ttyd.sh" >"$TTYD_LOG" 2>&1 && command -v ttyd >/dev/null 2>&1; then
  ok "$(ttyd --version 2>&1 | head -1)"
else
  warn "could not install ttyd -- the built-in terminal will be the only option."
  warn "Reason:"
  sed 's/^/        /' "$TTYD_LOG" | tail -6
  warn "Retry on its own with:  sudo bash tools/get-ttyd.sh"
fi
rm -f "$TTYD_LOG"

# ── firecracker ──────────────────────────────────────────────────────────────
step "Installing Firecracker"
FC_VER=${FC_VERSION:-}
if [ -z "$FC_VER" ]; then
  FC_VER=$(curl -fsSL https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
           | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)
fi
[ -n "$FC_VER" ] || FC_VER="v1.13.1"   # fallback if the API is unreachable

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-${ARCH}.tgz"
curl -fsSL "$URL" -o "$TMP/fc.tgz" || die "could not download $URL"
tar -xzf "$TMP/fc.tgz" -C "$TMP"
install -m0755 "$TMP/release-${FC_VER}-${ARCH}/firecracker-${FC_VER}-${ARCH}" /usr/local/bin/firecracker
ok "firecracker $(/usr/local/bin/firecracker --version | head -1)"

# ── guest kernel ─────────────────────────────────────────────────────────────
step "Fetching an uncompressed guest kernel"
if [ -s "$IMAGES/vmlinux" ] && [ -z "${FORCE_KERNEL:-}" ]; then
  ok "kernel already present (FORCE_KERNEL=1 to re-fetch)"
else
  CANDIDATES=(
    "${MVMP_KERNEL_URL:-}"
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/${ARCH}/vmlinux-6.1.141"
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.11/${ARCH}/vmlinux-6.1.128"
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${ARCH}/vmlinux-6.1.102"
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.9/${ARCH}/vmlinux-6.1.102"
  )
  got=""
  for u in "${CANDIDATES[@]}"; do
    [ -n "$u" ] || continue
    if curl -fsSL "$u" -o "$IMAGES/vmlinux"; then got="$u"; break; fi
  done
  [ -n "$got" ] || die "no kernel URL worked; set MVMP_KERNEL_URL to an uncompressed vmlinux"
  ok "kernel from $got"
fi

# ── shared guest configuration ───────────────────────────────────────────────
MNT=""
cleanup_mnt() {
  [ -n "$MNT" ] || return 0
  for m in dev/pts dev proc sys; do umount -l "$MNT/$m" 2>/dev/null || true; done
  umount -l "$MNT" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
  MNT=""
}
trap 'cleanup_mnt; rm -rf "$TMP"' EXIT

write_motd() {
  local flavour="$1"
  {
    printf '\n'
    printf '  \033[38;5;51m╭────────────────────────────────────────────────╮\033[0m\n'
    printf '  \033[38;5;51m│\033[0m\033[1;97m   microvm playground · firecracker · disposable\033[0m \033[38;5;51m│\033[0m\n'
    printf '  \033[38;5;51m╰────────────────────────────────────────────────╯\033[0m\n'
    printf '\n'
    printf '   \033[90m%s · UTF-8 · truecolor · full outbound internet\033[0m\n' "$flavour"
    printf '   \033[33m⚠\033[0m  \033[90mThis VM and its disk are destroyed on shutdown.\033[0m\n'
    printf '\n'
  } > "$MNT/etc/motd"
}

write_profile() {
  cat > "$MNT/etc/profile.d/00-mvmp.sh" <<'PROFILE'
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TERM=xterm-256color
export COLORTERM=truecolor
export PAGER=less
export LESS="-R"
export PS1='\[\e[1;38;5;48m\]\u@\h\[\e[0m\]:\[\e[1;38;5;75m\]\w\[\e[0m\]# '

alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias ll='ls -alF --color=auto'

# A serial console carries no SIGWINCH, so ask the terminal where its cursor
# lands after a huge move -- that reveals the real window size.
mvmp_resize() {
  local old rows cols
  old=$(stty -g 2>/dev/null) || return 0
  stty raw -echo min 0 time 3 2>/dev/null
  printf '\033[s\033[999;999H\033[6n' > /dev/tty
  IFS='[;R' read -r _ rows cols < /dev/tty
  printf '\033[u' > /dev/tty
  stty "$old" 2>/dev/null
  case "$rows$cols" in *[!0-9]*|"") return 0 ;; esac
  stty rows "$rows" cols "$cols" 2>/dev/null
}
mvmp_resize
PROFILE
  chmod 0644 "$MNT/etc/profile.d/00-mvmp.sh"
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$MNT/etc/resolv.conf"
  printf '127.0.0.1 localhost\n' > "$MNT/etc/hosts"
}

mount_pseudo() {
  mount --bind /dev "$MNT/dev"
  mount -t proc none "$MNT/proc"
  mount -t sysfs none "$MNT/sys"
  mkdir -p "$MNT/dev/pts" && mount -t devpts none "$MNT/dev/pts" 2>/dev/null || true
}

# ── debian guest ─────────────────────────────────────────────────────────────
build_debian() {
  echo "    debootstrap $SUITE (this takes a few minutes)"
  debootstrap --variant=minbase --include="$DEB_PKGS" "$SUITE" "$MNT" "$DEB_MIRROR" \
    || die "debootstrap failed"

  cat > "$MNT/etc/apt/sources.list" <<APT
deb $DEB_MIRROR $SUITE main contrib
deb $DEB_MIRROR ${SUITE}-updates main contrib
deb http://security.debian.org/debian-security ${SUITE}-security main contrib
APT

  cp /etc/resolv.conf "$MNT/etc/resolv.conf"
  mount_pseudo

  echo "    installing extras: $DEB_EXTRA"
  chroot "$MNT" /bin/bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq" || true
  for p in $DEB_EXTRA; do
    chroot "$MNT" /bin/bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $p >/dev/null 2>&1" \
      && echo "      + $p" || warn "skipped $p (not in $SUITE)"
  done

  # root logs in without a password; the console is already an authenticated path
  chroot "$MNT" /bin/bash -c "passwd -d root" >/dev/null 2>&1 || true

  echo "/dev/vda / ext4 defaults,noatime 0 1" > "$MNT/etc/fstab"
  echo "LANG=C.UTF-8" > "$MNT/etc/default/locale"

  # systemd spawns serial-getty@ttyS0 because of console=ttyS0; make it autologin
  mkdir -p "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d"
  cat > "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I xterm-256color
Type=idle
GETTY

  # Trim boot: nothing here needs periodic apt work or disk scrubbing, and
  # wait-online would stall the boot on a link the kernel already configured.
  chroot "$MNT" /bin/bash -c "
    systemctl mask apt-daily.timer apt-daily-upgrade.timer e2scrub_all.timer \
      e2scrub_reap.service systemd-networkd-wait-online.service >/dev/null 2>&1
    systemctl disable man-db.timer >/dev/null 2>&1
    true" || true

  write_profile
  write_motd "Debian ${SUITE} · apt"
  # Debian ships a dynamic MOTD banner; replace it with ours only.
  rm -f "$MNT/etc/update-motd.d/"* 2>/dev/null || true
  echo "playground" > "$MNT/etc/hostname"

  chroot "$MNT" /bin/bash -c "apt-get clean" >/dev/null 2>&1 || true
  rm -rf "$MNT/var/lib/apt/lists/"* "$MNT/var/cache/apt/archives/"*.deb 2>/dev/null || true
}

# ── alpine guest ─────────────────────────────────────────────────────────────
build_alpine() {
  MINIROOT="alpine-minirootfs-${ALPINE_VER}-${ARCH}.tar.gz"
  MIRROR="https://dl-cdn.alpinelinux.org/alpine/${ALPINE_BRANCH}/releases/${ARCH}/${MINIROOT}"
  curl -fsSL "$MIRROR" -o "$TMP/$MINIROOT" || die "could not download $MIRROR"
  tar -xzf "$TMP/$MINIROOT" -C "$MNT"

  printf 'https://dl-cdn.alpinelinux.org/alpine/%s/main\nhttps://dl-cdn.alpinelinux.org/alpine/%s/community\n' \
    "$ALPINE_BRANCH" "$ALPINE_BRANCH" > "$MNT/etc/apk/repositories"
  cp /etc/resolv.conf "$MNT/etc/resolv.conf"
  mount_pseudo

  echo "    installing: $ALPINE_PKGS"
  chroot "$MNT" /sbin/apk update -q
  chroot "$MNT" /sbin/apk add --no-cache $ALPINE_PKGS >/dev/null
  chroot "$MNT" /usr/bin/passwd -d root >/dev/null 2>&1 || true

  cat > "$MNT/etc/inittab" <<'INITTAB'
::sysinit:/bin/mount -t proc proc /proc
::sysinit:/bin/mount -t sysfs sysfs /sys
::sysinit:/bin/mount -t devtmpfs devtmpfs /dev
::sysinit:/bin/mkdir -p /dev/pts /dev/shm
::sysinit:/bin/mount -t devpts devpts /dev/pts
::sysinit:/bin/mount -t tmpfs tmpfs /dev/shm
::sysinit:/bin/mount -t tmpfs tmpfs /tmp
::sysinit:/sbin/ip link set lo up
::sysinit:/usr/local/sbin/netup
ttyS0::respawn:/sbin/getty -L -n -l /usr/local/bin/console-login 115200 ttyS0 xterm-256color
::ctrlaltdel:/sbin/poweroff
::shutdown:/bin/umount -a -r
INITTAB

  # busybox getty execs the login program with argv[0]="console-login", which is
  # not a login shell -- so start bash as one explicitly.
  cat > "$MNT/usr/local/bin/console-login" <<'LOGIN'
#!/bin/sh
exec /bin/bash --login
LOGIN
  chmod 0755 "$MNT/usr/local/bin/console-login"

  # The kernel ip= parameter configures eth0 before init runs; this is the
  # belt-and-braces path, and it applies the per-VM hostname.
  cat > "$MNT/usr/local/sbin/netup" <<'NETUP'
#!/bin/sh
IPSPEC=""; HN=""
for tok in $(cat /proc/cmdline); do
  case "$tok" in
    ip=*)        IPSPEC=${tok#ip=} ;;
    mvmp.host=*) HN=${tok#mvmp.host=} ;;
  esac
done

if [ -n "$HN" ]; then
  echo "$HN" > /etc/hostname
  hostname "$HN"
fi

ip link set eth0 up 2>/dev/null
# ip=<guest>::<gateway>:<netmask>::<dev>:off  -- the server always hands out /30
if [ -n "$IPSPEC" ] && ! ip -4 addr show dev eth0 2>/dev/null | grep -q "inet "; then
  GUEST=$(echo "$IPSPEC" | cut -d: -f1)
  GW=$(echo "$IPSPEC" | cut -d: -f3)
  [ -n "$GUEST" ] && ip addr add "$GUEST/30" dev eth0 2>/dev/null
  [ -n "$GW" ] && ip route add default via "$GW" 2>/dev/null
fi
exit 0
NETUP
  chmod 0755 "$MNT/usr/local/sbin/netup"

  write_profile
  write_motd "Alpine Linux · apk"
  echo '[ -f /etc/motd ] && cat /etc/motd' >> "$MNT/etc/profile.d/00-mvmp.sh"
  echo "playground" > "$MNT/etc/hostname"
}

# ── arch guest ───────────────────────────────────────────────────────────────
build_arch() {
  command -v unzstd >/dev/null 2>&1 || command -v zstd >/dev/null 2>&1     || die "DISTRO=arch needs zstd (apt-get install zstd)"

  TARBALL="archlinux-bootstrap-x86_64.tar.zst"
  MIRROR="https://geo.mirror.pkgbuild.com/iso/latest/${TARBALL}"
  echo "    downloading the Arch bootstrap tarball"
  curl -fsSL "$MIRROR" -o "$TMP/$TARBALL" || die "could not download $MIRROR"
  tar --use-compress-program=unzstd -xf "$TMP/$TARBALL" -C "$MNT" --strip-components=1     || die "could not unpack the bootstrap tarball"

  printf '%s
' 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch'     > "$MNT/etc/pacman.d/mirrorlist"
  cp /etc/resolv.conf "$MNT/etc/resolv.conf"
  mount_pseudo

  # Keyring init wants entropy and can stall on a headless host; bound it rather
  # than letting setup hang with no explanation.
  echo "    initialising the pacman keyring (this can take a minute)"
  timeout 420 chroot "$MNT" /usr/bin/pacman-key --init     || die "pacman-key --init failed or timed out"
  timeout 420 chroot "$MNT" /usr/bin/pacman-key --populate archlinux     || die "pacman-key --populate failed or timed out"

  echo "    installing: $ARCH_PKGS"
  chroot "$MNT" /usr/bin/pacman -Sy --noconfirm >/dev/null || die "pacman -Sy failed"
  chroot "$MNT" /usr/bin/pacman -S --noconfirm --needed $ARCH_PKGS >/dev/null     || die "pacman could not install the package set"
  chroot "$MNT" /usr/bin/passwd -d root >/dev/null 2>&1 || true

  echo "/dev/vda / ext4 defaults,noatime 0 1" > "$MNT/etc/fstab"
  printf '%s
' 'LANG=C.UTF-8' > "$MNT/etc/locale.conf"

  mkdir -p "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d"
  cat > "$MNT/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,38400,9600 %I xterm-256color
Type=idle
GETTY

  chroot "$MNT" /usr/bin/systemctl mask systemd-networkd-wait-online.service >/dev/null 2>&1 || true

  write_profile
  write_motd "Arch Linux · pacman"
  echo "playground" > "$MNT/etc/hostname"

  rm -rf "$MNT/var/cache/pacman/pkg/"* 2>/dev/null || true
}

# ── build the golden rootfs ──────────────────────────────────────────────────
step "Building the $DISTRO rootfs (${ROOTFS_MB} MB)"
if [ -s "$IMAGES/$ROOTFS_OUT" ] && [ -z "${FORCE_ROOTFS:-}" ]; then
  ok "rootfs already present (FORCE_ROOTFS=1 to rebuild)"
else
  rm -f "$IMAGES/$ROOTFS_OUT"
  truncate -s "${ROOTFS_MB}M" "$IMAGES/$ROOTFS_OUT"
  mkfs.ext4 -q -F -L mvmp-root "$IMAGES/$ROOTFS_OUT"

  MNT=$(mktemp -d)
  mount -o loop "$IMAGES/$ROOTFS_OUT" "$MNT"

  case "$DISTRO" in
    debian) build_debian ;;
    arch)   build_arch ;;
    *)      build_alpine ;;
  esac

  sync
  cleanup_mnt
  USED=$(du -h --apparent-size "$IMAGES/$ROOTFS_OUT" 2>/dev/null | cut -f1)
  ok "rootfs built at $IMAGES/$ROOTFS_OUT (${USED:-?})"
fi

# ── python env ───────────────────────────────────────────────────────────────
step "Creating the Python environment"
python3 -m venv "$HERE/.venv" 2>/dev/null || python3 -m venv --system-site-packages "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
ok "venv at $HERE/.venv"

# ── sanity notes ─────────────────────────────────────────────────────────────
step "Notes"
EGRESS=$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
ok "egress interface: ${EGRESS:-unknown}"
HOSTNET=$(ip -o -4 addr show dev "${EGRESS:-lo}" 2>/dev/null | awk '{print $4}' | head -1)
ok "host address: ${HOSTNET:-unknown}"
case "${HOSTNET:-}" in
  172.16.*) warn "Host is on 172.16/16, which collides with the guest range."
            warn "Set MVMP_SUBNET_BASE=10.201 (or similar) before starting the server." ;;
esac
MINGB=$(( (ROOTFS_MB + 1023) / 1024 ))
ok "minimum disk per playground is now ${MINGB} GB (the base image size)"

echo
c '1;32' "Setup complete."
echo "  Start it with:  sudo ./run.sh"
echo "  Then open:      http://127.0.0.1:8080"
