<#
  Portable QEMU for Windows, with no administrator rights.

  The official QEMU build is an NSIS installer that demands elevation. We never
  run it: we unpack it. 7-Zip is obtained the same way, via an MSI
  "administrative install" (msiexec /a), which is pure file extraction and
  needs no privileges either.

  Result: a self-contained QEMU under %LOCALAPPDATA%\mvmp\qemu.
#>
[CmdletBinding()]
param(
  [string]$Root = "$env:LOCALAPPDATA\mvmp",
  [string]$QemuUrl = "https://qemu.weilnetz.de/w64/2026/qemu-w64-setup-20260811.exe",
  [string]$SevenZipUrl = "https://www.7-zip.org/a/7z2501-x64.msi",
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is ~10x faster without it

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    ok  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !   $m" -ForegroundColor Yellow }

$qemuDir = Join-Path $Root "qemu"
$work    = Join-Path $Root "work"
$exe     = Join-Path $qemuDir "qemu-system-x86_64.exe"

if ((Test-Path $exe) -and -not $Force) {
  Ok "QEMU already present at $qemuDir"
  & $exe --version | Select-Object -First 1
  return
}

New-Item -ItemType Directory -Force -Path $Root, $work, $qemuDir | Out-Null

# ── 1. a 7-Zip we can use without installing it ──────────────────────────────
Step "Fetching 7-Zip (extraction only, no install)"
$msi = Join-Path $work "7z.msi"
$szDir = Join-Path $work "7z"
$sevenZip = Get-ChildItem -Path $szDir -Recurse -Filter "7z.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName

if (-not $sevenZip) {
  # Prefer a copy the user already has.
  foreach ($cand in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe")) {
    if (Test-Path $cand) { $sevenZip = $cand; break }
  }
}

if (-not $sevenZip) {
  Invoke-WebRequest -Uri $SevenZipUrl -OutFile $msi -UseBasicParsing
  Ok ("downloaded {0:N1} MB" -f ((Get-Item $msi).Length / 1MB))

  New-Item -ItemType Directory -Force -Path $szDir | Out-Null
  # /a is an administrative install: unpack only, no registry, no elevation.
  $p = Start-Process msiexec.exe -Wait -PassThru -NoNewWindow `
        -ArgumentList @("/a", "`"$msi`"", "/qn", "TARGETDIR=`"$szDir`"")
  if ($p.ExitCode -ne 0) { throw "msiexec /a failed with exit code $($p.ExitCode)" }

  $sevenZip = Get-ChildItem -Path $szDir -Recurse -Filter "7z.exe" |
              Select-Object -First 1 -ExpandProperty FullName
}
if (-not $sevenZip) { throw "could not obtain 7z.exe" }
Ok "7z at $sevenZip"

# ── 2. the QEMU installer, downloaded but never executed ─────────────────────
Step "Downloading QEMU (this is ~130 MB)"
$setup = Join-Path $work "qemu-setup.exe"
if (-not (Test-Path $setup) -or $Force) {
  Invoke-WebRequest -Uri $QemuUrl -OutFile $setup -UseBasicParsing
}
Ok ("installer {0:N1} MB" -f ((Get-Item $setup).Length / 1MB))

# ── 3. unpack it ─────────────────────────────────────────────────────────────
Step "Unpacking QEMU to $qemuDir"
& $sevenZip x $setup "-o$qemuDir" -y | Out-Null
if ($LASTEXITCODE -ne 0) { throw "7z extraction failed ($LASTEXITCODE)" }

# NSIS scratch space, useless to us
Remove-Item -Recurse -Force (Join-Path $qemuDir '$PLUGINSDIR') -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $qemuDir "Uninstall.exe") -ErrorAction SilentlyContinue

if (-not (Test-Path $exe)) {
  # Some builds nest one level deeper; go find it.
  $found = Get-ChildItem -Path $qemuDir -Recurse -Filter "qemu-system-x86_64.exe" |
           Select-Object -First 1
  if (-not $found) { throw "qemu-system-x86_64.exe not found after extraction" }
  Warn "binaries were nested under $($found.Directory.Name); flattening"
  Get-ChildItem -Path $found.Directory.FullName | Move-Item -Destination $qemuDir -Force
}

Ok "extracted"

# ── 4. prove it runs ─────────────────────────────────────────────────────────
Step "Verifying"
$ver = & $exe --version 2>&1 | Select-Object -First 1
Ok $ver
$accels = (& $exe -accel help 2>&1) -join " "
$best = if ($accels -match "whpx") { "whpx" } else { "tcg" }
Ok "accelerators available:$($accels -replace '\s+',' ')"
if ($best -eq "tcg") {
  Warn "No WHPX -- guests will run under TCG software emulation (slower but works everywhere)."
  Warn "To enable hardware acceleration, in an elevated PowerShell:"
  Warn "  Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All"
} else {
  Ok "WHPX available: guests will be hardware-accelerated"
}

Write-Host ""
Write-Host "QEMU ready at $qemuDir" -ForegroundColor Green
