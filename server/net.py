"""Per-VM TAP device + NAT so guests get full outbound internet.

Layout, one /30 per VM slot:
    <base>.<slot>.1  host side  (the tap device)
    <base>.<slot>.2  guest side (eth0, configured via kernel ip= param)
"""
import re
import subprocess
from typing import Optional

from . import config as C


class NetError(RuntimeError):
    pass


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(args, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise NetError(f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def detect_egress() -> str:
    """Interface that carries the host's default route."""
    if C.EGRESS_IFACE:
        return C.EGRESS_IFACE
    p = _run("ip", "-o", "route", "get", "1.1.1.1", check=False)
    if p.returncode == 0:
        parts = p.stdout.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    raise NetError("could not autodetect egress interface; set MVMP_EGRESS")


def slot_addrs(slot: int) -> tuple[str, str, str]:
    """(host_ip, guest_ip, cidr_network) for a slot."""
    host = f"{C.SUBNET_BASE}.{slot}.1"
    guest = f"{C.SUBNET_BASE}.{slot}.2"
    net = f"{C.SUBNET_BASE}.{slot}.0/30"
    return host, guest, net


def egress_mtu(egress: str) -> int:
    """MTU of the interface we NAT out of.

    This matters more than it looks. A tap device defaults to 1500, but the
    host's own egress is often smaller -- 1450 or less inside a Codespace,
    a container, or any overlay network. The guest then emits frames the path
    cannot carry: small requests succeed, anything that fills a TCP window
    stalls, and it presents as "the internet is broken" rather than as an MTU
    problem.
    """
    p = _run("ip", "-o", "link", "show", "dev", egress, check=False)
    m = re.search(r"mtu\s+(\d+)", p.stdout or "")
    return int(m.group(1)) if m else 1500


def tap_name(slot: int) -> str:
    return f"{C.TAP_PREFIX}{slot}"


def guest_mac(slot: int) -> str:
    return f"02:FC:00:00:{slot >> 8 & 0xFF:02X}:{slot & 0xFF:02X}"


def enable_forwarding() -> None:
    _run("sysctl", "-w", "-q", "net.ipv4.ip_forward=1")


# iptables rules are tagged with a comment so teardown is surgical -- we never
# flush the user's own chains.
def _rule_args(slot: int, egress: str, mtu: int = 1500) -> list[list[str]]:
    tap = tap_name(slot)
    _, _, net = slot_addrs(slot)
    tag = ["-m", "comment", "--comment", f"mvmp:{slot}"]
    # IPv4 header + TCP header = 40 bytes.
    mss = max(536, mtu - 40)
    return [
        ["-t", "nat", "POSTROUTING", "-s", net, "-o", egress, "-j", "MASQUERADE", *tag],
        ["-t", "filter", "FORWARD", "-i", tap, "-o", egress, "-j", "ACCEPT", *tag],
        ["-t", "filter", "FORWARD", "-o", tap, "-i", egress,
         "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT", *tag],
        # Rewrite the MSS the guest advertises so it never builds a segment the
        # path cannot carry. --set-mss rather than --clamp-mss-to-pmtu because
        # nested container networks frequently cannot discover a PMTU at all,
        # and a clamp that finds nothing silently does nothing.
        ["-t", "mangle", "FORWARD", "-o", egress, "-p", "tcp",
         "--tcp-flags", "SYN,RST", "SYN", "-s", net,
         "-j", "TCPMSS", "--set-mss", str(mss), *tag],
        ["-t", "mangle", "FORWARD", "-o", tap, "-p", "tcp",
         "--tcp-flags", "SYN,RST", "SYN", "-d", net,
         "-j", "TCPMSS", "--set-mss", str(mss), *tag],
    ]


def _iptables(op: str, rule: list[str], check: bool = True) -> None:
    table, chain, rest = rule[1], rule[2], rule[3:]
    _run("iptables", "-t", table, op, chain, *rest, check=check)


def setup(slot: int, egress: Optional[str] = None) -> tuple[str, str]:
    """Create the tap and NAT rules. Returns (guest_ip, host_ip)."""
    egress = egress or detect_egress()
    tap = tap_name(slot)
    host_ip, guest_ip, _ = slot_addrs(slot)

    teardown(slot, egress)  # idempotent: clear any stale leftovers

    mtu = egress_mtu(egress)
    _run("ip", "tuntap", "add", "dev", tap, "mode", "tap")
    _run("ip", "addr", "add", f"{host_ip}/30", "dev", tap)
    _run("ip", "link", "set", "dev", tap, "mtu", str(mtu), check=False)
    _run("ip", "link", "set", "dev", tap, "up")

    enable_forwarding()
    for rule in _rule_args(slot, egress, mtu):
        # Insert rather than append: Docker sets the FORWARD policy to DROP and
        # installs its own chains, and an appended rule can sit behind one that
        # already decided. These are scoped to this tap, so jumping the queue is
        # safe.
        _iptables("-I" if rule[1] == "filter" else "-A", rule)
    return guest_ip, host_ip


def teardown(slot: int, egress: Optional[str] = None) -> None:
    """Remove tap + rules. Never raises -- teardown runs on error paths."""
    try:
        egress = egress or detect_egress()
    except NetError:
        egress = None
    if egress:
        for rule in _rule_args(slot, egress, egress_mtu(egress)):
            # -D repeatedly in case a crash left duplicates behind
            for _ in range(4):
                p = _run("iptables", "-t", rule[1], "-D", rule[2], *rule[3:], check=False)
                if p.returncode != 0:
                    break
    _run("ip", "link", "del", tap_name(slot), check=False)


def ip_boot_arg(slot: int) -> str:
    """Kernel IP autoconfiguration -- no guest agent needed."""
    host_ip, guest_ip, _ = slot_addrs(slot)
    return f"ip={guest_ip}::{host_ip}:255.255.255.252::eth0:off"
