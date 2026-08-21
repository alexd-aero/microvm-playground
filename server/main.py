"""HTTP + websocket surface for the microVM playground."""
import argparse
import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager

import httpx
import websockets as wsclient
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from . import ttyd
from .manager import Manager
from . import catalog
from .models import CreateVM, RenameVM

manager = Manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only the backends that keep per-VM files on disk need a state directory.
    # Containers store nothing, and failing to create a directory it will never
    # use should not stop the server from starting.
    if manager.backend in ("firecracker", "qemu"):
        try:
            C.VMS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                "cannot create the state directory %s (%s). Set MVMP_STATE_DIR "
                "to a writable path." % (C.VMS_DIR, exc)) from exc
    await manager.start_reaper()
    _print_banner()
    try:
        yield
    finally:
        await manager.shutdown()


def _print_banner() -> None:
    """Say what was chosen and what to expect from it.

    "Why is this slow?" should be answerable without opening the UI: the
    difference between a 150 ms launch and a 30 s one is entirely which backend
    and accelerator ended up in play, so print both, plus the way out.
    """
    h = manager.host_info()
    accel = h.accel or ""

    if h.mode == "container":
        expect, faster = "launches in ~150 ms (native, no emulation)", None
    elif h.mode == "firecracker":
        expect, faster = "~125 ms of VMM, then a second or two of guest init", None
    elif h.mode == "qemu" and accel and accel != "tcg":
        expect, faster = "guest boots in a few seconds (%s acceleration)" % accel, None
    elif h.mode == "qemu":
        expect = "guest boots in ~20-40 s -- TCG emulates every instruction"
        faster = [
            "for instant launches, build the container image:",
            "    docker build -t mvmp-playground:latest docker/",
            "for a hardware-accelerated VM, get /dev/kvm on this host",
        ]
    else:
        expect, faster = "simulated -- there is no machine behind this", None

    # ASCII only: a Windows console is cp1252 and box-drawing characters raise
    # UnicodeEncodeError. And nothing here is worth failing a startup over, so
    # the whole thing is best-effort.
    rule = "-" * 66
    rows = [
        ("backend", "%s%s" % (h.mode, (" (%s)" % accel) if accel else "")),
        ("terminal", h.ttyd if h.terminal == "ttyd" and h.ttyd else
                     ("ttyd" if h.terminal == "ttyd" else "built-in xterm.js")),
        ("expect", expect),
    ]
    rows += [("problem", p) for p in h.problems]
    try:
        print("\n" + rule)
        for label, value in rows:
            print(" %-9s %s" % (label, value))
        if faster:
            print("")
            for i, f in enumerate(faster):
                print(" %-9s %s" % ("faster" if i == 0 else "", f))
        print(rule + "\n", flush=True)
    except Exception:
        pass


app = FastAPI(title="microvm playground", lifespan=lifespan)


@app.get("/api/host")
async def host():
    return manager.host_info()


@app.get("/api/vms")
async def list_vms():
    return manager.list()


@app.post("/api/vms")
async def create_vm(spec: CreateVM):
    try:
        rec = await manager.create(spec)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if rec.state == "error":
        return JSONResponse(status_code=500, content=json.loads(rec.view().model_dump_json()))
    return rec.view()


@app.get("/api/images")
async def images():
    """Operating systems this host can launch, with whether they are ready."""
    return catalog.list_images(manager.backend)


@app.patch("/api/vms/{vm_id}")
async def rename_vm(vm_id: str, body: RenameVM):
    rec = await manager.rename(vm_id, body.name)
    if rec is None:
        raise HTTPException(status_code=404, detail="no such playground")
    return rec.view()


@app.post("/api/vms/{vm_id}/stop")
async def stop_vm(vm_id: str):
    try:
        rec = await manager.suspend(vm_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if rec is None:
        raise HTTPException(status_code=404, detail="no such playground")
    return rec.view()


@app.post("/api/vms/{vm_id}/start")
async def start_vm(vm_id: str):
    rec = await manager.resume(vm_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="no such playground")
    if rec.state == "error":
        return JSONResponse(status_code=500,
                            content=json.loads(rec.view().model_dump_json()))
    return rec.view()


@app.delete("/api/vms/{vm_id}")
async def destroy_vm(vm_id: str):
    if not await manager.destroy(vm_id):
        raise HTTPException(status_code=404, detail="no such playground")
    return {"ok": True}


@app.websocket("/api/vms/{vm_id}/console")
async def console(ws: WebSocket, vm_id: str):
    await ws.accept()
    rec = manager.get(vm_id)
    if rec is None or rec.vm is None:
        await ws.close(code=4404, reason="no such playground")
        return

    hub = rec.vm.console
    queue = hub.subscribe()

    back = hub.scrollback()
    if back:
        await ws.send_bytes(back)

    async def pump():
        """Console -> browser."""
        while True:
            chunk = await queue.get()
            if chunk is None:
                with contextlib.suppress(Exception):
                    await ws.send_bytes(b"\r\n\x1b[90m-- console closed --\x1b[0m\r\n")
                return
            await ws.send_bytes(chunk)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                rec.vm.write(msg["bytes"])
            elif msg.get("text"):
                # Control channel: resize events, keepalives.
                try:
                    payload = json.loads(msg["text"])
                except ValueError:
                    rec.vm.write(msg["text"].encode("utf-8"))
                    continue
                if payload.get("type") == "resize":
                    rec.vm.resize(int(payload.get("rows", 24)), int(payload.get("cols", 80)))
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        hub.unsubscribe(queue)


# --- ttyd reverse proxy -------------------------------------------------------
# Each playground runs its own ttyd on a private localhost port. Proxying them
# under /terminal/<id> keeps everything on the single server port, which is the
# only way this works behind Codespaces port forwarding -- per-VM ports could
# not be forwarded individually.

# Headers that describe *this* connection and must not be relayed onto another.
_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer",
    "proxy-authenticate", "proxy-authorization",
    # httpx has already decoded the body, so the upstream framing headers lie.
    "content-length", "content-encoding",
}


def _session_or_404(vm_id: str):
    sess = ttyd.get(vm_id)
    if sess is None or not sess.alive:
        raise HTTPException(status_code=404, detail="no terminal for this playground")
    return sess


async def _proxy_http(vm_id: str, tail: str, request: Request) -> Response:
    sess = _session_or_404(vm_id)
    url = "http://%s%s%s" % (sess.upstream, sess.base_path, tail)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method, url,
            params=dict(request.query_params),
            content=await request.body(),
            headers={k: v for k, v in request.headers.items()
                     if k.lower() not in _HOP_BY_HOP and k.lower() != "host"},
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items()
                 if k.lower() not in _HOP_BY_HOP},
        media_type=upstream.headers.get("content-type"),
    )


@app.websocket("/terminal/{vm_id}/ws")
async def terminal_ws(ws: WebSocket, vm_id: str):
    sess = ttyd.get(vm_id)
    if sess is None or not sess.alive:
        await ws.close(code=4404)
        return

    # ttyd speaks its own "tty" subprotocol; the handshake fails without it.
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="tty" if "tty" in offered else None)

    url = "ws://%s%s/ws" % (sess.upstream, sess.base_path)
    try:
        async with wsclient.connect(url, subprotocols=["tty"], max_size=None,
                                    open_timeout=20, ping_interval=None) as up:
            async def downstream_to_upstream():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])

            async def upstream_to_downstream():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            tasks = [asyncio.create_task(downstream_to_upstream()),
                     asyncio.create_task(upstream_to_downstream())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, ConnectionError, OSError):
        pass
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


@app.api_route("/terminal/{vm_id}", methods=["GET", "HEAD"])
async def terminal_root(vm_id: str, request: Request):
    return await _proxy_http(vm_id, "/", request)


@app.api_route("/terminal/{vm_id}/{tail:path}", methods=["GET", "HEAD", "POST"])
async def terminal_path(vm_id: str, tail: str, request: Request):
    return await _proxy_http(vm_id, "/" + tail, request)


# --- static UI (mounted last so /api wins) -----------------------------------
@app.get("/")
async def index():
    return FileResponse(C.WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(C.WEB_DIR)), name="web")


def main() -> None:
    ap = argparse.ArgumentParser(description="microVM playground server")
    ap.add_argument("--mock", action="store_true",
                    help="simulate VMs (no KVM needed) -- for UI work and demos")
    ap.add_argument("--host", default=C.BIND_HOST)
    ap.add_argument("--port", type=int, default=C.BIND_PORT)
    args = ap.parse_args()

    # ttyd's bridge dials back into this server, so it needs the port we
    # actually bound -- not the default.
    C.RUNTIME_PORT = args.port

    if args.mock:
        os.environ["MVMP_MOCK"] = "1"
        C.MOCK = True
        manager.backend = "mock"
        manager.mock = True

    import uvicorn
    uvicorn.run(
        app, host=args.host, port=args.port, log_level="info",
        # Terminal traffic is a stream of tiny frames; compressing each one
        # costs more latency than the bytes it saves.
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()
