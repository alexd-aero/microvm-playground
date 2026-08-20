<#
  Start the playground server.

      .\run.ps1                 QEMU guests on 127.0.0.1:8080
      .\run.ps1 -Port 9000
      .\run.ps1 -Mock           simulated VMs, for UI work only

  No administrator rights required: QEMU runs unprivileged and its user-mode
  networking needs no tap device or firewall rule.
#>
[CmdletBinding()]
param(
  [int]$Port = 8080,
  [string]$BindHost = "127.0.0.1",
  [switch]$Mock
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Host "Python 3 not found." -ForegroundColor Red; exit 1 }

$serverArgs = @((Join-Path $here "run_server.py"), "--port", $Port, "--host", $BindHost)
if ($Mock) { $serverArgs += "--mock" }

Write-Host "microvm playground -> http://${BindHost}:${Port}" -ForegroundColor Cyan
& $py @serverArgs
