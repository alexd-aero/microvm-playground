"""Mock backend: a simulated microVM with an in-process shell.

Exists only to exercise the UI -- creation, cards, console, colours, Unicode --
without starting a machine. It is never selected automatically: the QEMU backend
runs real guests on any host, so there is no reason to fall back to a simulation.
Nothing here executes host commands, and there are no real binaries in it.
"""
import asyncio
import random
import time
from typing import Optional

from .console import ConsoleHub

ESC = "\x1b"
RESET = ESC + "[0m"


def _c(code: str, text: str) -> str:
    return ESC + "[" + code + "m" + text + RESET


BOOT_LINES = [
    (0.02, "[    0.000000] Linux version 6.1.141 (firecracker) #1 SMP"),
    (0.01, "[    0.000000] Command line: console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw"),
    (0.02, "[    0.011232] KVM setup paravirtual spinlock"),
    (0.01, "[    0.018904] Memory: {mem}K/{memk}K available"),
    (0.02, "[    0.031117] virtio_blk virtio0: [vda] {sectors} 512-byte logical blocks"),
    (0.01, "[    0.037551] virtio_net virtio1: eth0: renamed from enp0s2"),
    (0.02, "[    0.044019] IP-Config: Guest address {ip}, gateway {gw}"),
    (0.03, "[    0.051884] EXT4-fs (vda): mounted filesystem with ordered data mode"),
    (0.01, "[    0.058233] Freeing unused kernel memory: 1360K"),
    (0.02, "[    0.061007] Run /sbin/init as init process"),
    (0.03, "systemd: mounting /proc /sys /dev/pts /dev/shm ... " + _c("32", "ok")),
    (0.02, "systemd: configuring eth0 via kernel ip= ... " + _c("32", "ok")),
    (0.02, "systemd: starting serial-getty@ttyS0 ... " + _c("32", "ok")),
    (0.02, _c("33", "note: simulated boot -- no kernel was actually started")),
]

MOTD = "\r\n".join([
    "",
    _c("38;5;214", "  ╭────────────────────────────────────────────────────────╮"),
    _c("38;5;214", "  │") + _c("1;97", "   MOCK MODE  ·  this is a simulation, not a real VM   ") + _c("38;5;214", " │"),
    _c("38;5;214", "  ╰────────────────────────────────────────────────────────╯"),
    "",
    "   " + _c("90", "There is no kernel, no filesystem and no network here. This"),
    "   " + _c("90", "shell understands a fixed list of demo commands and nothing"),
    "   " + _c("90", "else -- no apt, no git, no curl, because no binaries exist."),
    "",
    "   " + _c("1;97", "For a real Debian VM with apt/git/curl/wget/neofetch, just"),
    "   " + _c("1;97", "restart without --mock: ") + _c("1;93", ".\\setup.ps1")
          + _c("1;97", " then ") + _c("1;93", ".\\run.ps1") + _c("1;97", "."),
    "   " + _c("90", "No WSL, no admin, no Hyper-V -- QEMU handles it."),
    "",
    "   " + _c("90", "Useful here: ") + _c("1;93", "help") + _c("90", " · ") + _c("1;93", "colortest")
          + _c("90", " · ") + _c("1;93", "unicode") + _c("90", " · ") + _c("1;93", "fetch")
          + _c("90", "  (these test the terminal itself)"),
    "",
])


class MockVM:
    """Same surface as FirecrackerVM, backed by a toy shell."""

    def __init__(self, vm_id: str, slot: int, vcpus: int, mem_mib: int, disk_gb: int,
                 name: str = ""):
        self.id = vm_id
        self.slot = slot
        self.vcpus = vcpus
        self.mem_mib = mem_mib
        self.disk_gb = disk_gb
        self.name = name or vm_id[:8]

        self.console = ConsoleHub()
        self.ip = "172.16.%d.2" % slot
        self.gateway = "172.16.%d.1" % slot
        self.boot_ms: Optional[int] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # shell state
        self._line = ""
        self._pos = 0
        self._hist: list[str] = []
        self._hist_idx = 0
        self._cwd = "/root"
        self._rows, self._cols = 24, 80
        self._started = time.time()
        self._pending = b""
        self._ready = False

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        t0 = time.monotonic()
        self._running = True
        self._task = asyncio.create_task(self._boot())
        # A real Firecracker boot is ~125 ms of VMM work; imitate the shape.
        await asyncio.sleep(0.12 + random.random() * 0.08)
        self.boot_ms = int((time.monotonic() - t0) * 1000)

    async def _boot(self) -> None:
        memk = self.mem_mib * 1024
        sectors = self.disk_gb * 1024 * 1024 * 2
        for delay, line in BOOT_LINES:
            await asyncio.sleep(delay)
            if not self._running:
                return
            text = (line.replace("{mem}", str(int(memk * 0.86)))
                        .replace("{memk}", str(memk))
                        .replace("{sectors}", str(sectors))
                        .replace("{ip}", self.ip)
                        .replace("{gw}", self.gateway))
            self._emit(_c("90", text) if text.startswith("[") else text)
        await asyncio.sleep(0.05)
        self._emit(MOTD, newline=False)
        self._ready = True
        self._prompt()

    # -------------------------------------------------------------------- I/O
    def _emit(self, text: str, newline: bool = True) -> None:
        self.console.publish((text + ("\r\n" if newline else "")).encode("utf-8"))

    def _prompt(self) -> None:
        p = (_c("1;38;5;48", "root@" + self.name) + _c("97", ":")
             + _c("1;38;5;75", self._cwd) + _c("97", "# "))
        self.console.publish(p.encode("utf-8"))

    def _redraw(self) -> None:
        # \r, clear to EOL, reprint line, then park the cursor.
        out = "\r" + ESC + "[K"
        p = (_c("1;38;5;48", "root@" + self.name) + _c("97", ":")
             + _c("1;38;5;75", self._cwd) + _c("97", "# "))
        out += p + self._line
        back = len(self._line) - self._pos
        if back > 0:
            out += ESC + "[" + str(back) + "D"
        self.console.publish(out.encode("utf-8"))

    def resize(self, rows: int, cols: int) -> None:
        self._rows, self._cols = rows, cols

    def write(self, data: bytes) -> None:
        if not self._ready:
            return
        self._pending += data
        try:
            text = self._pending.decode("utf-8")
            self._pending = b""
        except UnicodeDecodeError:
            # Hold a partial multi-byte sequence until the rest arrives.
            if len(self._pending) > 8:
                self._pending = b""
            return
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == ESC and text[i:i + 3].startswith(ESC + "["):
                self._csi(text[i + 2:i + 3])
                i += 3
                continue
            self._key(ch)
            i += 1

    def _csi(self, final: str) -> None:
        if final == "A":       # up
            if self._hist and self._hist_idx > 0:
                self._hist_idx -= 1
                self._line = self._hist[self._hist_idx]
                self._pos = len(self._line)
                self._redraw()
        elif final == "B":     # down
            if self._hist_idx < len(self._hist) - 1:
                self._hist_idx += 1
                self._line = self._hist[self._hist_idx]
            else:
                self._hist_idx = len(self._hist)
                self._line = ""
            self._pos = len(self._line)
            self._redraw()
        elif final == "C" and self._pos < len(self._line):
            self._pos += 1
            self.console.publish((ESC + "[C").encode())
        elif final == "D" and self._pos > 0:
            self._pos -= 1
            self.console.publish((ESC + "[D").encode())

    def _key(self, ch: str) -> None:
        if ch in ("\r", "\n"):
            self.console.publish(b"\r\n")
            line = self._line.strip()
            self._line, self._pos = "", 0
            if line:
                self._hist.append(line)
            self._hist_idx = len(self._hist)
            self._exec(line)
        elif ch in ("\x7f", "\b"):
            if self._pos > 0:
                at_end = self._pos == len(self._line)
                self._line = self._line[: self._pos - 1] + self._line[self._pos:]
                self._pos -= 1
                if at_end:
                    self.console.publish(b"\b \b")   # cheaper than a line repaint
                else:
                    self._redraw()
        elif ch == "\x03":
            self.console.publish(_c("90", "^C").encode() + b"\r\n")
            self._line, self._pos = "", 0
            self._prompt()
        elif ch == "\x0c":
            self.console.publish((ESC + "[2J" + ESC + "[H").encode())
            self._redraw()
        elif ch == "\x01":
            self._pos = 0
            self._redraw()
        elif ch == "\x05":
            self._pos = len(self._line)
            self._redraw()
        elif ch == "\x15":
            self._line, self._pos = "", 0
            self._redraw()
        elif ch.isprintable():
            at_end = self._pos == len(self._line)
            self._line = self._line[: self._pos] + ch + self._line[self._pos:]
            self._pos += 1
            if at_end:
                self.console.publish(ch.encode("utf-8"))   # plain echo, no repaint
            else:
                self._redraw()

    # ---------------------------------------------------------------- commands
    def _exec(self, line: str) -> None:
        if not line:
            self._prompt()
            return
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        fn = getattr(self, "_cmd_" + cmd.replace("-", "_"), None)
        if fn is None:
            self._emit(_c("91", cmd + ": not available in mock mode"))
            self._emit(_c("90", "This shell is a simulation with no real binaries."))
            self._emit(_c("90", "Restart the server without --mock for a real Debian VM where ")
                       + _c("1;93", cmd) + _c("90", " exists."))
        else:
            try:
                fn(args)
            except Exception as exc:  # keep the shell alive whatever happens
                self._emit(_c("91", "error: " + str(exc)))
        self._prompt()

    def _cmd_help(self, args):
        rows = [
            ("help", "this list"),
            ("colortest", "16 / 256 / truecolor ramps"),
            ("unicode", "UTF-8, CJK, emoji, combining marks, box drawing"),
            ("fetch", "system summary"),
            ("ls / cat / pwd / cd", "virtual filesystem"),
            ("free / df / nproc / uptime", "the specs you provisioned"),
            ("uname / whoami / date / echo", "the usual"),
            ("history / clear / exit", "shell control"),
        ]
        self._emit("")
        for k, v in rows:
            self._emit("  " + _c("1;93", k.ljust(28)) + _c("90", v))
        self._emit("")
        self._emit("  " + _c("90", "This is the mock backend. On WSL2 with /dev/kvm you get a real"))
        self._emit("  " + _c("90", "Alpine shell in a Firecracker microVM with outbound internet."))
        self._emit("")

    def _cmd_colortest(self, args):
        self._emit("")
        self._emit("  " + _c("1;97", "standard 16"))
        row = "  "
        for i in list(range(30, 38)) + list(range(90, 98)):
            row += _c(str(i), " ██")
        self._emit(row)
        self._emit("")
        self._emit("  " + _c("1;97", "256-colour cube"))
        for base in range(16, 232, 36):
            row = "  "
            for i in range(base, min(base + 36, 232)):
                row += _c("38;5;" + str(i), "█")
            self._emit(row)
        self._emit("")
        self._emit("  " + _c("1;97", "24-bit truecolor"))
        row = "  "
        for i in range(64):
            r = int(255 * abs((i / 64) * 2 - 1))
            g = int(255 * (i / 64))
            b = 255 - g
            row += ESC + "[38;2;%d;%d;%dm█" % (r, g, b)
        self._emit(row + RESET)
        self._emit("")
        self._emit("  " + _c("1;97", "attributes") + "  " + _c("1", "bold") + " " + _c("2", "dim")
                   + " " + _c("3", "italic") + " " + _c("4", "underline") + " " + _c("7", "reverse")
                   + " " + _c("9", "strike"))
        self._emit("")

    def _cmd_unicode(self, args):
        samples = [
            ("box drawing", "┌─┬─┐ ├─┼─┤ └─┴─┘ ═║╔╝ ░▒▓█"),
            ("braille/blocks", "⣿⣾⣼⣸⣰⣠⣀ ▁▂▃▄▅▆▇█"),
            ("latin + accents", "àéîõü æøå ßçñ řžščď"),
            ("greek / cyrillic", "αβγδεζηθ  АБВГДЕЖЗ"),
            ("cjk (wide)", "你好世界  こんにちは  안녕하세요"),
            ("rtl", "مرحبا بالعالم  שלום עולם"),
            ("emoji (wide)", "\U0001f680 \U0001f525 \U0001f9ca \U0001f4e6 ✨ \U0001f427 \U0001f512"),
            ("zwj sequence", "\U0001f468‍\U0001f4bb  \U0001f469‍\U0001f680  \U0001f3f4󠁧󠁢󠁳󠁣󠁴󠁿"),
            ("combining", "é ä õ ñ z̧  A⃝"),
            ("math / misc", "∀x∈ℝ ∃y √2 ≈ 1.414 ≠ ≤ ≥ ∫ ∑ ∏ π"),
            ("currency", "€ £ ¥ ₹ ₩ ₽ ₺ ₿ ¢"),
        ]
        self._emit("")
        for label, text in samples:
            self._emit("  " + _c("38;5;245", label.ljust(18)) + _c("97", text))
        self._emit("")
        self._emit("  " + _c("90", "Wide glyphs should occupy exactly two cells (unicode11 addon)."))
        self._emit("")

    def _cmd_fetch(self, args):
        up = int(time.time() - self._started)
        art = [
            _c("38;5;51", "      /\\        "),
            _c("38;5;51", "     /  \\       "),
            _c("38;5;51", "    / /\\ \\      "),
            _c("38;5;51", "   / /  \\ \\     "),
            _c("38;5;51", "  /_/    \\_\\    "),
            _c("38;5;51", "                "),
        ]
        info = [
            _c("1;38;5;48", "root") + _c("97", "@") + _c("1;38;5;48", self.name),
            _c("90", "─" * 24),
            _c("1;97", "os      ") + "Debian 12 " + _c("33", "(simulated)"),
            _c("1;97", "host    ") + "mock backend " + _c("33", "(no VM, no KVM)"),
            _c("1;97", "kernel  ") + "6.1.141 " + _c("33", "(pretend)"),
            _c("1;97", "uptime  ") + str(up) + "s",
            _c("1;97", "cpu     ") + str(self.vcpus) + " vCPU",
            _c("1;97", "memory  ") + str(self.mem_mib) + " MiB",
            _c("1;97", "disk    ") + str(self.disk_gb) + " GB",
            _c("1;97", "net     ") + str(self.ip) + " via " + str(self.gateway),
            _c("1;97", "boot    ") + str(self.boot_ms) + " ms",
        ]
        self._emit("")
        for i in range(max(len(art), len(info))):
            left = art[i] if i < len(art) else " " * 16
            right = info[i] if i < len(info) else ""
            self._emit("  " + left + right)
        self._emit("")

    _FS = {
        "/root": ["README.txt", "notes.md", ".profile"],
        "/etc": ["alpine-release", "hostname", "resolv.conf", "profile"],
        "/": ["bin", "dev", "etc", "home", "proc", "root", "sys", "tmp", "usr", "var"],
    }
    _FILES = {
        "README.txt": "This playground is disposable.\nAnything you write here is gone when it is destroyed.\n",
        "notes.md": "# scratch\n\n- ephemeral by design\n- full outbound internet in real mode\n",
        "alpine-release": "3.21.0\n",
        "resolv.conf": "nameserver 1.1.1.1\nnameserver 8.8.8.8\n",
    }

    def _cmd_ls(self, args):
        entries = self._FS.get(self._cwd, [])
        if not entries:
            return
        out = "  "
        for e in entries:
            out += (_c("1;38;5;75", e) if "." not in e else _c("97", e)) + "  "
        self._emit(out)

    def _cmd_pwd(self, args):
        self._emit(self._cwd)

    def _cmd_cd(self, args):
        target = args[0] if args else "/root"
        if target == "..":
            target = "/"
        if target in self._FS:
            self._cwd = target
        else:
            self._emit(_c("91", "cd: " + target + ": no such directory"))

    def _cmd_cat(self, args):
        if not args:
            self._emit(_c("91", "cat: missing operand"))
            return
        key = args[0].split("/")[-1]
        if key in self._FILES:
            for line in self._FILES[key].rstrip("\n").split("\n"):
                self._emit(line)
        else:
            self._emit(_c("91", "cat: " + args[0] + ": No such file or directory"))

    def _cmd_echo(self, args):
        self._emit(" ".join(args))

    def _cmd_whoami(self, args):
        self._emit("root")

    def _cmd_uname(self, args):
        if args and args[0] in ("-a", "-all"):
            self._emit("Linux " + self.name + " 6.1.141 #1 SMP x86_64 Linux")
        else:
            self._emit("Linux")

    def _cmd_date(self, args):
        self._emit(time.strftime("%a %b %e %H:%M:%S UTC %Y", time.gmtime()))

    def _cmd_uptime(self, args):
        up = int(time.time() - self._started)
        self._emit(" %s up %dm %ds,  load average: 0.00, 0.01, 0.00"
                   % (time.strftime("%H:%M:%S", time.gmtime()), up // 60, up % 60))

    def _cmd_nproc(self, args):
        self._emit(str(self.vcpus))

    def _cmd_free(self, args):
        total = self.mem_mib * 1024
        used = int(total * 0.11)
        self._emit("               total        used        free")
        self._emit("Mem:      %10d  %10d  %10d" % (total, used, total - used))

    def _cmd_df(self, args):
        total = self.disk_gb * 1024 * 1024
        used = int(total * 0.18)
        self._emit("Filesystem      1K-blocks      Used Available Use%% Mounted on".replace("%%", "%"))
        self._emit("/dev/vda        %9d %9d %9d  18%% /".replace("%%", "%")
                   % (total, used, total - used))

    def _cmd_history(self, args):
        for i, h in enumerate(self._hist, 1):
            self._emit("  %4d  %s" % (i, h))

    def _cmd_clear(self, args):
        self.console.publish((ESC + "[2J" + ESC + "[H").encode())

    def _cmd_exit(self, args):
        self._emit(_c("90", "This playground is disposable -- use Destroy in the UI."))

    def _cmd_curl(self, args):
        self._emit(_c("91", "No network in mock mode."))
        self._emit(_c("90", "A real playground gets its own tap device and NAT out through the"))
        self._emit(_c("90", "host, so curl/wget/git/apt all reach the internet normally."))

    _cmd_ping = _cmd_wget = _cmd_ssh = _cmd_curl

    def _cmd_apt(self, args):
        self._emit(_c("91", "apt is not available in mock mode."))
        self._emit(_c("90", "The real guest is Debian ") + _c("1;97", "bookworm")
                   + _c("90", " with a working apt, plus"))
        self._emit(_c("90", "git, curl, wget, neofetch, python3, build-essential and friends"))
        self._emit(_c("90", "preinstalled. Build it with ") + _c("1;93", r".\setup.ps1")
                   + _c("90", " -- no WSL or admin needed."))

    _cmd_apt_get = _cmd_dpkg = _cmd_apk = _cmd_yum = _cmd_apt

    def _cmd_sudo(self, args):
        if args:
            self._emit(_c("90", "You are already root here; just run ")
                       + _c("1;93", " ".join(args)) + _c("90", " directly."))
            return self._exec_nested(" ".join(args))
        self._emit(_c("90", "usage: sudo <command>   (you are already root)"))

    def _exec_nested(self, line: str) -> None:
        parts = line.split()
        fn = getattr(self, "_cmd_" + parts[0].replace("-", "_"), None)
        if fn:
            fn(parts[1:])

    def _cmd_neofetch(self, args):
        self._cmd_fetch(args)

    def _cmd_lsblk(self, args):
        self._emit("NAME MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS")
        self._emit("vda  254:0    0  %4dG  0 disk /" % self.disk_gb)

    # ---------------------------------------------------------------- teardown
    async def suspend(self) -> None:
        self._running = False
        self._ready = False
        if self._task:
            self._task.cancel()
        self.console.close()

    async def resume(self) -> None:
        self.console = ConsoleHub()
        self._running = True
        self._started = time.time()
        self._task = asyncio.create_task(self._boot())

    async def stop(self, graceful: bool = True) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        self.console.close()

    @property
    def alive(self) -> bool:
        return self._running
