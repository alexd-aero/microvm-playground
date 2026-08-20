# microvm playground

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/alexd-aero/microvm-playground)

Disposable Linux playgrounds with a glass UI and a real terminal in the browser.
Pick vCPU / memory / disk, hit launch, get a root shell with full outbound
internet. Destroy it and every trace is gone.

```
browser ──ws(binary)──> FastAPI ──pty/tcp──> container | QEMU | Firecracker
   xterm.js                                  one per playground, Debian userland
```

## Quick start in Codespaces

Click the badge. The devcontainer installs the dependencies and builds the
playground image; then:

```bash
./run.sh
```

Port 8080 forwards automatically.

Codespaces does **not** expose `/dev/kvm`, so no VM backend can be hardware
accelerated there — QEMU would fall back to instruction-by-instruction
emulation, which is the opposite of fast. The container backend is selected
instead: it runs directly on the host CPU, so it is both native speed and
millisecond startup. The cost is a shared kernel, which is a weaker boundary
than a VM. That trade is stated in the UI, not hidden.

## Backends

Selected automatically by *readiness*, not just capability; override with
`MVMP_BACKEND`.

| | when it is used | isolation | speed |
|---|---|---|---|
| **firecracker** | Linux with `/dev/kvm` and images built | full VM | ~125 ms boot |
| **container** | Docker available — this is the Codespaces path | shared kernel | native speed, startup in **milliseconds** |
| **qemu** | anywhere, including Windows | full VM | hardware-accelerated via KVM/WHPX/HVF when possible, TCG emulation otherwise |
| **mock** | only if you pass `--mock` | **none — it is not a machine** | instant |

Mock is never chosen automatically. It has no kernel, no filesystem and no
network; it exists to exercise the UI. `apt` and `git` are not "missing" there,
there are no binaries at all.

### Why the container backend is fast

Two reasons, and only one of them is "containers are quick":

1. No emulation and no hypervisor — the guest's instructions are the host's.
2. A **warm pool**. Containers are created and started ahead of demand, then
   adopted and resized in place with `docker update`, so a launch costs an
   `exec` rather than a create-plus-start. Pool size is `MVMP_POOL` (default 2;
   set `0` to disable).

It also gets *better* terminal behaviour than the VM backends: a container
attach is a real pty, so `SIGWINCH` propagates and window resizing needs none of
the `stty` injection a serial console requires.

## Windows — no WSL, no admin, no Hyper-V

```powershell
.\setup.ps1
.\run.ps1
```

Then open <http://127.0.0.1:8080>.

`setup.ps1` needs no elevation. It installs the Python dependencies, unpacks a
**portable QEMU**, and bakes the golden guest image.

Getting QEMU without admin rights is the interesting part. The official build is
an NSIS installer that demands elevation — so we never run it, we *unpack* it.
7-Zip is obtained the same way, through an MSI "administrative install"
(`msiexec /a`), which is pure file extraction. Everything lands in
`%LOCALAPPDATA%\mvmp\qemu` and nothing touches the registry or `Program Files`.

### About speed

QEMU runs guests one of two ways:

- **WHPX** — hardware acceleration through the Windows Hypervisor Platform. Fast.
- **TCG** — software emulation of every instruction. Works absolutely everywhere,
  needs nothing at all, and is roughly 5–10x slower.

The server probes for WHPX by *actually launching it*, because a QEMU built with
WHPX support still fails at runtime when Windows refuses. The common failure is:

```
WHPX: Failed to enable nested virtualization, hr=80370302
```

which means another hypervisor already owns the CPU — usually Virtualization
Based Security / Memory Integrity, or Hyper-V. If you want the fast path, enable
the platform feature in an elevated PowerShell:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
```

and, if it still fails, turn off Core Isolation → Memory Integrity in Windows
Security. Both need a reboot. **This is optional** — under TCG everything works,
a Debian guest just takes tens of seconds to boot instead of a few.

## Linux

```bash
sudo ./setup.sh          # firecracker + kernel + debootstrapped rootfs
sudo ./run.sh
```

`DISTRO=alpine` builds a smaller, ~4x faster-booting guest that uses `apk`.
Firecracker needs root for tap devices and NAT; the QEMU backend does not.

## How the QEMU backend works

Three choices make it work unprivileged:

- **User-mode (SLIRP) networking.** The guest reaches the internet through the
  host's own sockets — no tap device, no NAT rules, no admin. The guest sits at
  `10.0.2.15` behind a virtual gateway at `10.0.2.2`. `apt` just works.
- **qcow2 backing files.** Each playground is a copy-on-write clone of the golden
  image, created in milliseconds and costing a few hundred KB until written to.
  Ten playgrounds do not cost ten times the disk.
- **Serial and QMP over TCP, dialled outward.** Windows has no ptys, so the
  server listens on two ephemeral ports and QEMU connects back to them. No
  pty emulation, and no port-allocation race.

Shutdown is ACPI (`system_powerdown` over QMP) with a `quit` and then a kill as
fallbacks, after which the overlay is deleted.

### The golden image

`tools/bake.py` boots the stock Debian cloud image once with a generated
cloud-init seed ISO, installs a real userland, configures autologin on the
serial console, then disables cloud-init and powers off. Later boots go straight
to a shell.

```bash
python tools/bake.py                                    # defaults
python tools/bake.py --force --packages "git curl build-essential golang"
python tools/bake.py --disk-gb 16
```

The bake is the slow step — every apt operation runs inside an emulated CPU.
The default package list is deliberately lean for that reason.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MVMP_BACKEND` | `auto` | `container`, `qemu`, `firecracker` or `mock` |
| `MVMP_STATE_DIR` | `%LOCALAPPDATA%\mvmp` / `/var/lib/mvmp` | images and per-VM data |
| `MVMP_HOST` / `MVMP_PORT` | `127.0.0.1` / `8080` | bind address |
| `MVMP_ACCEL` | autoprobed | force `tcg`, `whpx`, `kvm`, `hvf` |
| `MVMP_IMAGE` | `images/golden.qcow2` | golden image for QEMU |
| `MVMP_MAX_VMS` | `32` | concurrent playground cap |
| `MVMP_POOL` | `2` | warm containers kept ready (container backend) |
| `MVMP_IMAGE_TAG` | `mvmp-playground:latest` | container image to run |

A playground's disk can never be smaller than the golden image it clones —
qcow2 overlays inherit their backing file's virtual size. The server computes
the real minimum and the UI clamps the slider to it. Going *larger* than the
golden image works, but the guest filesystem will not fill the extra space
until you run `growpart /dev/vda 1 && resize2fs /dev/vda1` inside it.

## Security

- **There is no authentication.** Anyone who can reach the port can create VMs
  and get a root shell in them. It binds to `127.0.0.1` for that reason. Put it
  behind an authenticating proxy before using `-BindHost 0.0.0.0`.
- The QEMU backend runs entirely as your own user. A guest escape would land in
  your account, not root — but it is still a real boundary you are trusting.
- **The container backend shares the host kernel.** It runs as root inside the
  container with `no-new-privileges` and a pid limit, but a kernel exploit is a
  full escape. That is an acceptable trade for a scratch environment in a
  throwaway Codespace; it is not one for running code you actively distrust.
  Use a VM backend if you need a real boundary.
- SLIRP means guests reach whatever your host can reach, including your LAN.
- Firecracker mode needs root on the host for `CAP_NET_ADMIN`, and does not use
  `jailer` — that would be the next hardening step.

## Known limitations

- **TCG is slow.** A Debian guest takes tens of seconds to reach a prompt. This
  is inherent to emulating every instruction, not a bug.
- **Live resize is slightly noisy.** A serial console carries no `SIGWINCH`, so
  on browser resize the server writes `stty rows R cols C` into the guest shell,
  which echoes one line. Initial sizing is clean — the guest queries the terminal
  with a cursor-position report at login.
- **Typing latency is dominated by rendering, not the network.** The console
  websocket round-trips in ~2 ms on localhost. The terminal uses xterm's WebGL
  renderer for that reason; an embedded webview that throttles
  `requestAnimationFrame` will still feel laggy, so prefer a real browser tab.
- **Hostnames are generic** in the QEMU guest (`playground`); the friendly name
  lives in the UI. Per-VM hostnames would need a per-VM seed ISO.

## Layout

```
server/
  main.py         FastAPI routes + console websocket
  container.py    Docker backend with a warm pool (Codespaces path)
  manager.py      backend selection, registry, TTL reaper, host probe
  qemu.py         QEMU process, serial/QMP over TCP, qcow2 overlays
  firecracker.py  VMM process, API-over-unix-socket, pty console
  mock.py         simulated backend (UI demo only)
  net.py          tap devices and NAT rules (firecracker only)
  images.py       per-VM disk provisioning (firecracker only)
  console.py      console fan-out with scrollback
web/              index.html style.css app.js + vendored xterm.js
tools/
  get-qemu.ps1    portable QEMU, no admin
  bake.py         builds the golden Debian image
setup.ps1 run.ps1   Windows entry points
setup.sh  run.sh    Linux entry points
```
