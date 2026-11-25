#Requires -Version 5.1
<#
.SYNOPSIS
  Install WinToolkit agent as a persistent SYSTEM scheduled task.

.DESCRIPTION
  Copies agent files to ProgramData, writes config with token + mode,
  and registers an ONSTART task running as NT AUTHORITY\SYSTEM (Highest).

  Run elevated (Administrator) once on the Windows box.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$Token,

  [ValidateSet("listen", "reverse")]
  [string]$Mode = "listen",

  [int]$ListenPort = 47382,
  [string]$BindAddress = "0.0.0.0",
  [string]$ConnectHost = "",
  [int]$ConnectPort = 47382,

  [string]$InstallRoot = "$env:ProgramData\WinToolkit",
  [string]$TaskName = "WinToolkitAgent",
  [switch]$StartNow
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run Install-WinToolkit.ps1 from an elevated Administrator PowerShell."
  }
}

Assert-Admin

if ($Mode -eq "reverse" -and -not $ConnectHost) {
  throw "-ConnectHost is required when -Mode reverse"
}

$agentSrc = Join-Path $PSScriptRoot "WinToolkitAgent.ps1"
if (-not (Test-Path -LiteralPath $agentSrc)) {
  throw "WinToolkitAgent.ps1 not found next to installer"
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallRoot "logs") -Force | Out-Null

$agentDst = Join-Path $InstallRoot "WinToolkitAgent.ps1"
Copy-Item -LiteralPath $agentSrc -Destination $agentDst -Force

$config = @{
  token         = $Token
  mode          = $Mode
  listen_port   = $ListenPort
  bind_address  = $BindAddress
  connect_host  = $ConnectHost
  connect_port  = $ConnectPort
}
$configPath = Join-Path $InstallRoot "agent.config.json"
($config | ConvertTo-Json -Depth 5) | Set-Content -Path $configPath -Encoding UTF8

# Restrict ACL: SYSTEM + Administrators only
$acl = Get-Acl $InstallRoot
$acl.SetAccessRuleProtection($true, $false)
$rules = @(
  New-Object System.Security.AccessControl.FileSystemAccessRule("NT AUTHORITY\SYSTEM", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"),
  New-Object System.Security.AccessControl.FileSystemAccessRule("BUILTIN\Administrators", "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
)
foreach ($r in $rules) { $acl.AddAccessRule($r) }
Set-Acl -Path $InstallRoot -AclObject $acl

$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agentDst`" -ConfigPath `"$configPath`""
if ($Mode -eq "reverse") {
  $args += " -ConnectHost `"$ConnectHost`" -ConnectPort $ConnectPort"
} else {
  $args += " -ListenPort $ListenPort -BindAddress `"$BindAddress`""
}
$args += " -Token `"$Token`""

# Remove existing task if present
schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null

# ONSTART as SYSTEM — survives reboot, runs before logon
$create = schtasks.exe /Create /TN $TaskName /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "$ps $args" /F
if ($LASTEXITCODE -ne 0) {
  throw "schtasks create failed: $create"
}

Write-Host "Installed WinToolkit agent:"
Write-Host "  Root:  $InstallRoot"
Write-Host "  Task:  $TaskName (ONSTART / SYSTEM / HIGHEST)"
Write-Host "  Mode:  $Mode"
if ($Mode -eq "listen") {
  Write-Host "  Bind:  ${BindAddress}:${ListenPort}"
  Write-Host "  Open firewall if needed: New-NetFirewallRule -DisplayName WinToolkit -Direction Inbound -Protocol TCP -LocalPort $ListenPort -Action Allow"
} else {
  Write-Host "  Dial:  ${ConnectHost}:${ConnectPort}"
}

if ($StartNow) {
  schtasks.exe /Run /TN $TaskName | Out-Null
  Start-Sleep -Seconds 1
  Write-Host "  Started task now."
}

# Verify identity hint
Write-Host ""
Write-Host "After start, from Linux:  wt info <host>"
Write-Host "Expect whoami: NT AUTHORITY\SYSTEM"
