#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$InstallRoot = "$env:ProgramData\WinToolkit",
  [string]$TaskName = "WinToolkitAgent",
  [switch]$RemoveFiles
)

$ErrorActionPreference = "Stop"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$p = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run elevated as Administrator."
}

schtasks.exe /End /TN $TaskName 2>$null | Out-Null
schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
Write-Host "Removed scheduled task: $TaskName"

# Kill stray agent processes
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and $_.CommandLine -like "*WinToolkitAgent.ps1*" } |
  ForEach-Object {
    try {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
      Write-Host "Killed PID $($_.ProcessId)"
    } catch {}
  }

if ($RemoveFiles -and (Test-Path -LiteralPath $InstallRoot)) {
  Remove-Item -LiteralPath $InstallRoot -Recurse -Force
  Write-Host "Removed $InstallRoot"
} else {
  Write-Host "Left files at $InstallRoot (pass -RemoveFiles to delete)"
}
