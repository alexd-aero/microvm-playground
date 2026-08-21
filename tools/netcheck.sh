#!/usr/bin/env bash
# Find out which layer of guest networking is actually broken.
#
#   bash tools/netcheck.sh
#
# Run it on the host (the Codespace, not inside a playground). The point is to
# separate three very different problems that all present as "the internet is
# broken": the platform blocking traffic, this project's NAT being wrong, and
# an MTU mismatch.
set -uo pipefail

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33mnote\033[0m  %s\n' "$1"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

EGRESS=$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
MTU=$(ip -o link show dev "${EGRESS:-lo}" 2>/dev/null | sed -n 's/.*mtu \([0-9]*\).*/\1/p')

hdr "host"
echo "  egress interface : ${EGRESS:-unknown}"
echo "  egress MTU       : ${MTU:-unknown}"
[ "${MTU:-1500}" -lt 1500 ] 2>/dev/null && \
  warn "below 1500 -- guests need MSS clamping, which this project now applies"

if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ]; then
  ok "ip_forward enabled"
else
  bad "ip_forward disabled -- no guest can route out"
fi

# The decisive question: can the HOST itself do these things? If the host
# cannot, no firewall rule of ours will make a guest able to.
hdr "host reachability (this is the one that matters)"
if ping -c 2 -W 3 1.1.1.1 >/dev/null 2>&1; then
  ok "ICMP works from the host"
  ICMP_HOST=yes
else
  bad "ICMP blocked from the host itself"
  warn "Then ping cannot work inside a guest either -- the platform is dropping"
  warn "it, not this project. Azure/Codespaces commonly does. TCP is unaffected."
  ICMP_HOST=no
fi

if curl -sS -m 10 -o /dev/null https://1.1.1.1 2>/dev/null; then
  ok "TCP/443 works from the host"
else
  bad "TCP/443 blocked from the host -- nothing will reach the internet"
fi

hdr "playground firewall rules"
# grep -c prints 0 and exits 1 when it matches nothing, so an `|| echo 0`
# fallback appended a second line and produced two zeroes, which then broke
# the numeric test below.
if [ "$(id -u)" != "0" ]; then
  warn "not root: cannot read iptables. Re-run with sudo to inspect the rules."
  RULES=-1
else
  RULES=$(iptables-save 2>/dev/null | grep -c 'mvmp:') || RULES=0
fi
if [ "$RULES" -gt 0 ] 2>/dev/null; then
  ok "$RULES rule(s) installed"
  iptables-save 2>/dev/null | grep 'mvmp:' | sed 's/^/      /'
elif [ "$RULES" = "0" ]; then
  warn "no rules found -- is a playground running?"
fi

if [ -r /proc/net/nf_conntrack ] || lsmod 2>/dev/null | grep -q nf_conntrack; then
  ok "conntrack available"
else
  warn "conntrack not visible; return traffic relies on the explicit rules"
fi

hdr "verdict"
if [ "$ICMP_HOST" = "no" ]; then
  echo "  ping will not work inside a playground on this host, and that is not"
  echo "  fixable from here. TCP -- apt, curl, git, ssh -- is unaffected."
  echo "  QEMU's SLIRP fakes ICMP, which is why ping appears to work on Windows."
else
  echo "  the host can reach the internet; if a guest cannot, the fault is in"
  echo "  the rules above. Capture the guest's traffic with:"
  echo "      tcpdump -ni ${EGRESS:-eth0} icmp"
fi
echo
