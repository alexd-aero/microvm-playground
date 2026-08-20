"""HTTP + websocket surface for the microVM playground."""
import argparse
import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from .manager import Manager
from .models import CreateVM

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
    try:
        yield
    finally:
        await manager.shutdown()


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
