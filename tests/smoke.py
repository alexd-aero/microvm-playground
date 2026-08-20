#!/usr/bin/env python3
"""End-to-end smoke test against a running playground server.

Exercises the real thing: creates playgrounds over the HTTP API, attaches to
their consoles over the websocket, runs commands in the guest, checks the output
and the byte-level integrity of the stream, then destroys everything and
verifies nothing leaked.

    python tests/smoke.py --url http://127.0.0.1:8080 --expect-backend container
"""
import argparse
import asyncio
import codecs
import json
import re
import subprocess
import sys
import time
import urllib.request

import websockets

ANSI = re.compile(r"\x1b\[[0-9;:?<=>]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")
PROMPT = re.compile(r"root@[\w.-]+:[^\r\n]*[#$]\s*$")

PASS, FAIL = "\033[32mPASS\033[0m", "\033[1;31mFAIL\033[0m"
failures = []


def check(name, ok, detail=""):
    print("  %s  %s%s" % (PASS if ok else FAIL, name,
                          ("  -- " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


def api(url, path, method="GET", body=None):
    req = urllib.request.Request(url + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


class Shell:
    """A console websocket with a stateful UTF-8 decoder, like xterm.js."""

    def __init__(self, ws):
        self.ws = ws
        self.dec = codecs.getincrementaldecoder("utf-8")()
        self.text = ""
        self.replacements = 0
        self.chunks = 0

    async def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=end - time.monotonic())
            except Exception:
                return
            if isinstance(msg, bytes):
                self.chunks += 1
                piece = self.dec.decode(msg)
                self.replacements += piece.count("�")
                self.text += piece

    async def wait_prompt(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await self.pump(2)
            if PROMPT.search(ANSI.sub("", self.text)[-500:]):
                return True
        return False

    async def run(self, cmd, wait=25):
        self.text = ""
        await self.ws.send((cmd + "\n").encode())
        await self.pump(wait)
        clean = ANSI.sub("", self.text)
        out = []
        for line in clean.splitlines():
            # Strip a trailing prompt rather than dropping the whole line:
            # output without a trailing newline (curl -w, printf -n) shares a
            # line with the next prompt, and deleting it loses the result.
            line = PROMPT.sub("", line).rstrip()
            if line.strip() and cmd not in line:
                out.append(line)
        return "\n".join(out)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--expect-backend", default=None)
    ap.add_argument("--boot-budget-ms", type=int, default=5000)
    ap.add_argument("--shell-timeout", type=int, default=120)
    args = ap.parse_args()
    ws_url = args.url.replace("http://", "ws://").replace("https://", "wss://")

    # ---------------------------------------------------------------- host
    print("\n\033[1mhost\033[0m")
    for _ in range(60):
        try:
            host = api(args.url, "/api/host")
            break
        except Exception:
            time.sleep(1)
    else:
        print("server never became reachable at " + args.url)
        return 1

    print("  backend=%s accel=%s" % (host["mode"], host.get("accel")))
    if args.expect_backend:
        check("backend is %s" % args.expect_backend,
              host["mode"] == args.expect_backend, "got " + host["mode"])
    check("no host problems", not host["problems"], "; ".join(host["problems"]))
    check("backend is not mock", host["mode"] != "mock")

    # -------------------------------------------------------------- create
    print("\n\033[1mlaunch\033[0m")
    t0 = time.monotonic()
    vm = api(args.url, "/api/vms", "POST",
             {"name": "smoke1", "vcpus": 2, "mem_mib": 1024,
              "disk_gb": host["defaults"]["disk_gb"], "ttl": "15m"})
    wall = int((time.monotonic() - t0) * 1000)
    check("state is running", vm["state"] == "running", str(vm.get("error")))
    print("      reported boot_ms=%s, wall=%dms" % (vm["boot_ms"], wall))
    check("startup within %dms" % args.boot_budget_ms,
          vm["boot_ms"] is not None and vm["boot_ms"] < args.boot_budget_ms,
          "boot_ms=%s" % vm["boot_ms"])

    # warm pool: a second launch should be no slower
    t0 = time.monotonic()
    vm2 = api(args.url, "/api/vms", "POST",
              {"name": "smoke2", "vcpus": 1, "mem_mib": 512,
               "disk_gb": host["defaults"]["disk_gb"], "ttl": "15m"})
    wall2 = int((time.monotonic() - t0) * 1000)
    print("      second launch wall=%dms (warm pool)" % wall2)
    check("second launch also running", vm2["state"] == "running")

    # ------------------------------------------------------------- console
    print("\n\033[1mguest\033[0m")
    async with websockets.connect(ws_url + "/api/vms/%s/console" % vm["id"],
                                  max_size=None, open_timeout=30) as ws:
        sh = Shell(ws)
        got = await sh.wait_prompt(args.shell_timeout)
        if not check("reached a shell", got, ANSI.sub("", sh.text)[-300:]):
            return 1

        out = await sh.run("id -u; echo MARK-$((6*7))")
        check("shell executes commands", "MARK-42" in out, out[:120])
        check("running as root", out.strip().startswith("0"), out[:120])

        for tool, needle in (("git --version", "git version"),
                             ("curl --version", "curl "),
                             ("wget --version", "GNU Wget"),
                             ("python3 -V", "Python 3"),
                             ("jq --version", "jq-"),
                             ("neofetch --version", "neofetch")):
            out = await sh.run(tool, 30)
            check("has %s" % tool.split()[0], needle.lower() in out.lower(), out[:100])

        out = await sh.run("apt-get --version", 30)
        check("has apt", "apt " in out.lower(), out[:100])

        # real outbound internet from inside the playground
        out = await sh.run(
            r"curl -s -o /dev/null -w 'HTTP:%{http_code}\n' https://deb.debian.org/ "
            r"|| echo NONET:$?", 60)
        if not check("outbound internet works", "HTTP:200" in out, repr(out[:200])):
            # Distinguish DNS failure from routing failure -- they need
            # different fixes, and "no internet" alone does not say which.
            diag = await sh.run("getent hosts deb.debian.org || echo DNS-FAIL", 30)
            print("      dns: %s" % (diag.strip()[:100] or "(no output)"))
            diag = await sh.run("ip route 2>/dev/null | head -2 || echo NO-ROUTE", 20)
            print("      route: %s" % (diag.strip()[:100] or "(no output)"))

        # UTF-8 and colour integrity across the byte stream
        before = sh.replacements
        out = await sh.run(
            r"printf 'U8 \344\275\240\345\245\275 \360\237\232\200 \342\224\214\342\224\254\342\224\220 "
            r"\316\261\316\262\316\263 \342\210\200\342\210\210\342\204\235\n'", 20)
        check("utf-8 round-trips", "你好" in out and "🚀" in out and "┌┬┐" in out, repr(out[:120]))
        check("no replacement chars", sh.replacements == before,
              "%d new U+FFFD" % (sh.replacements - before))

        raw_before = len(sh.text)
        sh.text = ""
        await ws.send(b"printf '\\033[1;32mGREEN\\033[0m \\033[38;5;208mC256\\033[0m "
                      b"\\033[38;2;255;0;255mTRUE\\033[0m\\n'\n")
        await sh.pump(10)
        check("ansi colour passes through",
              "\x1b[1;32m" in sh.text and "\x1b[38;5;208m" in sh.text
              and "\x1b[38;2;255;0;255m" in sh.text, repr(sh.text[:150]))

        # scrollback must not contain terminal queries: replaying them makes a
        # newly attached client answer, and the answer lands on the shell.
        check("no DSR/DA queries in stream",
              not re.search(r"\x1b\[[0-9;:?<=>]*[nc]", sh.text), "found a query sequence")
        print("      %d websocket chunks, %d replacement chars total"
              % (sh.chunks, sh.replacements))

    # reattach: scrollback replay must not inject junk
    async with websockets.connect(ws_url + "/api/vms/%s/console" % vm["id"],
                                  max_size=None, open_timeout=30) as ws:
        sh2 = Shell(ws)
        await sh2.pump(6)
        clean = ANSI.sub("", sh2.text)
        check("reattach is clean", not re.search(r";\d+R|syntax error", clean),
              [l for l in clean.splitlines() if re.search(r";\d+R|syntax error", l)][:2])

    # ------------------------------------------------------------- destroy
    print("\n\033[1mteardown\033[0m")
    for v in (vm, vm2):
        api(args.url, "/api/vms/" + v["id"], "DELETE")
    remaining = api(args.url, "/api/vms")
    check("all playgrounds destroyed", remaining == [], str(remaining))

    if host["mode"] == "container":
        # The warm pool is allowed to survive; adopted playgrounds are not.
        out = subprocess.run(["docker", "ps", "-a", "--filter", "label=mvmp=1",
                              "--filter", "name=mvmp-", "--format", "{{.Names}}"],
                             capture_output=True, text=True)
        leaked = [l for l in out.stdout.splitlines() if l.strip()]
        check("no leaked containers", not leaked, ", ".join(leaked))

    print()
    if failures:
        print("\033[1;31m%d check(s) failed:\033[0m %s" % (len(failures), ", ".join(failures)))
        return 1
    print("\033[1;32mall checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
