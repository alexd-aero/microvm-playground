"""Fan-out hub between a VM's serial console and any number of websocket clients.

Keeps a scrollback ring so a browser that reconnects (or a second tab) sees the
boot log instead of a blank screen.
"""
import asyncio
import re
from typing import Optional

SCROLLBACK_BYTES = 256 * 1024

# Terminal *queries* the emulator is expected to answer: Device Status Report
# (ESC[...n, e.g. ESC[6n for cursor position) and Device Attributes (ESC[...c).
#
# These must never be replayed. A guest that asked "where is the cursor?" during
# boot gets its answer at boot; if the same bytes are replayed to a browser that
# attaches ten minutes later, xterm.js answers again and the reply arrives as
# stray keyboard input on the shell prompt -- producing junk like ";151R" and a
# bash syntax error. Live output is passed through untouched so interactive
# queries still work.
# The parameter class must include the private markers < = > ? so that forms
# like ESC[>0c (secondary Device Attributes) are matched too.
_QUERY_RE = re.compile(rb"\x1b\[[0-9;:?<=>]*[nc]")


def strip_queries(data: bytes) -> bytes:
    return _QUERY_RE.sub(b"", data)


class ConsoleHub:
    def __init__(self, scrollback: int = SCROLLBACK_BYTES):
        self._subs: set[asyncio.Queue] = set()
        self._buf = bytearray()
        self._limit = scrollback
        self.closed = False

    # --- producer side -------------------------------------------------------
    def publish(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        if len(self._buf) > self._limit:
            del self._buf[: len(self._buf) - self._limit]
        for q in list(self._subs):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                # Slow client: drop it rather than stalling the console.
                self._subs.discard(q)

    def close(self) -> None:
        self.closed = True
        for q in list(self._subs):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subs.clear()

    # --- consumer side -------------------------------------------------------
    def scrollback(self) -> bytes:
        """History for a newly attached client, with queries defused."""
        return strip_queries(bytes(self._buf))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
