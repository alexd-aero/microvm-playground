<#
  One-time setup for the microVM playground on Windows.

      .\setup.ps1

  Needs no administrator rights and no WSL. It installs the Python
  dependencies, unpacks a portable QEMU, and bakes the golden Debian image
  every playground is cloned from.
#>
[CmdletBinding()]
param(
  [int]$DiskGB = 8,
  [string]$Packages = "",
  [switch]$ForceQemu,
  [switch]$ForceImage,
  [switch]$SkipBake
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    ok  $m" -ForegroundColor Green }
function Die($m)  { Write-Host "    x   $m" -ForegroundColor Red; exit 1 }

# ── python ───────────────────────────────────────────────────────────────────
Step "Checking Python"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) { Die "Python 3 not found. Install it from python.org or 'winget install Python.Python.3.12'" }
$pyver = & $py --version
Ok $pyver

Step "Installing Python dependencies"
& $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
Ok "fastapi, uvicorn, httpx, pydantic, pycdlib"

# ── qemu ─────────────────────────────────────────────────────────────────────
Step "Provisioning QEMU (portable, no admin)"
$qemuArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $here "tools\get-qemu.ps1"))
if ($ForceQemu) { $qemuArgs += "-Force" }
& powershell.exe @qemuArgs
if ($LASTEXITCODE -ne 0) { Die "QEMU provisioning failed" }

# ── golden image ─────────────────────────────────────────────────────────────
if ($SkipBake) {
  Step "Skipping the image bake as requested"
} else {
  Step "Baking the golden Debian image"
  Write-Host "    This boots a real guest and installs packages inside it." -ForegroundColor DarkGray
  Write-Host "    Under TCG emulation expect 15-45 minutes. It happens once." -ForegroundColor DarkGray
  $bakeArgs = @((Join-Path $here "tools\bake.py"), "--disk-gb", $DiskGB)
  if ($Packages)   { $bakeArgs += @("--packages", $Packages) }
  if ($ForceImage) { $bakeArgs += "--force" }
  & $py @bakeArgs
  if ($LASTEXITCODE -ne 0) { Die "image bake failed" }
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  Start it with:  .\run.ps1"
Write-Host "  Then open:      http://127.0.0.1:8080"
