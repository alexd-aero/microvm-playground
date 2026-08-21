#!/usr/bin/env python3
"""Bridge a pty to a playground's console websocket.

ttyd runs this as its command, handing it a real pty. Everything typed on that
pty is forwarded to the playground's console, and everything the playground
emits is written back. One bridge works for every backend -- container, QEMU
and Firecracker -- because they all publish through the same console hub, and
because ttyd starts a fresh copy per browser connection, several people can
attach at once without fighting over a single serial line.

    python3 tools/attach.py ws://127.0.0.1:8080/api/vms/<id>/console
"""
import asyncio
import fcntl
import json
import os
import signal
import struct
import sys
import termios

import websockets


def winsize(fd=0):
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        if rows and cols:
            return rows, cols
    except Exception:
        pass
    return 24, 80


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: attach.py <console-websocket-url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    loop = asyncio.get_running_loop()

    try:
        ws = await websockets.connect(url, max_size=None, open_timeout=30,
                                      ping_interval=20)
    except Exception as exc:
        # ttyd shows whatever the command prints, so make the failure legible.
        sys.stdout.write("\r\n\x1b[1;31mcould not attach to the playground:\x1b[0m %s\r\n"
                         % exc)
        sys.stdout.flush()
        await asyncio.sleep(3)
        return 1

    # On a serial console a resize is written to the guest as a visible `stty`
    # line, so sending an unchanged size is not merely wasteful -- it prints
    # another line of noise. ttyd can emit several SIGWINCHs for one change
    # (entering fullscreen does), which is why duplicates showed up at all.
    last_size: list = [None]

    async def send_size():
        size = winsize()
        if size == last_size[0]:
            return
        last_size[0] = size
        rows, cols = size
        try:
            await ws.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))
        except Exception:
            pass

    # ttyd resizes our pty, which arrives as SIGWINCH.
    resized = asyncio.Event()
    with_signal = True
    try:
        loop.add_signal_handler(signal.SIGWINCH, resized.set)
    except (NotImplementedError, AttributeError):
        with_signal = False

    async def watch_resize():
        while with_signal:
            await resized.wait()
            resized.clear()
            # A drag or a fullscreen transition produces a burst; let it settle
            # so the guest is told once, not once per intermediate size.
            await asyncio.sleep(0.25)
            resized.clear()
            await send_size()

    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader),
                                 sys.stdin.buffer)

    async def to_playground():
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await ws.send(data)

    async def from_playground():
        async for msg in ws:
            if isinstance(msg, str):
                msg = msg.encode("utf-8", "replace")
            os.write(1, msg)

    await send_size()
    tasks = [asyncio.create_task(t()) for t in (to_playground, from_playground)]
    if with_signal:
        tasks.append(asyncio.create_task(watch_resize()))

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    with_closed = getattr(ws, "close", None)
    if with_closed:
        await ws.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
