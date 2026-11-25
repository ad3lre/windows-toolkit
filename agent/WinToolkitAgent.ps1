#Requires -Version 5.1
<#
.SYNOPSIS
  WinToolkit agent — persistent SYSTEM shell bridge for Linux clients.

.DESCRIPTION
  Listens for (or reverse-connects to) authenticated Linux clients and exposes
  PowerShell/cmd exec, interactive sessions, and file ops as NT AUTHORITY\SYSTEM
  when installed via Install-WinToolkit.ps1.

.PARAMETER ListenPort
  TCP port to bind (default 47382). Ignored in -ConnectHost mode.

.PARAMETER BindAddress
  Address to bind (default 0.0.0.0). Use 127.0.0.1 behind a tunnel.

.PARAMETER ConnectHost
  If set, reverse-connect to this host instead of listening.

.PARAMETER ConnectPort
  Port for reverse connect (default 47382).

.PARAMETER Token
  Shared secret. Falls back to $env:WINTOOLKIT_TOKEN or config file next to script.

.PARAMETER ConfigPath
  Path to agent config JSON (token, listen, connect).

.PARAMETER ReconnectSeconds
  Delay between reverse-connect retries (default 5).
#>
[CmdletBinding()]
param(
  [int]$ListenPort = 47382,
  [string]$BindAddress = "0.0.0.0",
  [string]$ConnectHost = "",
  [int]$ConnectPort = 47382,
  [string]$Token = "",
  [string]$ConfigPath = "",
  [int]$ReconnectSeconds = 5
)

$ErrorActionPreference = "Stop"
$script:AgentVersion = "1.0.0"
$script:AgentName = "wintoolkit"
$script:Sessions = @{}
$script:SessionLock = New-Object object

function Write-AgentLog {
  param([string]$Message, [string]$Level = "INFO")
  $ts = (Get-Date).ToString("s")
  $line = "[$ts] [$Level] $Message"
  try {
    $logDir = Join-Path $PSScriptRoot "logs"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    Add-Content -Path (Join-Path $logDir "agent.log") -Value $line -Encoding UTF8
  } catch {}
  [Console]::Error.WriteLine($line)
}

function ConvertTo-JsonCompact {
  param($Object)
  ($Object | ConvertTo-Json -Compress -Depth 12)
}

function Read-AgentConfig {
  param([string]$Path)
  if (-not $Path) {
    $Path = Join-Path $PSScriptRoot "agent.config.json"
  }
  if (Test-Path $Path) {
    return Get-Content -Raw -Path $Path | ConvertFrom-Json
  }
  return $null
}

function Get-EffectiveToken {
  param([string]$Explicit, $Config)
  if ($Explicit) { return $Explicit }
  if ($env:WINTOOLKIT_TOKEN) { return $env:WINTOOLKIT_TOKEN }
  if ($Config -and $Config.token) { return [string]$Config.token }
  return ""
}

function New-MessageId {
  [guid]::NewGuid().ToString("N").Substring(0, 12)
}

function Send-JsonLine {
  param(
    [System.IO.Stream]$Stream,
    [object]$WriteLock,
    [hashtable]$Object
  )
  $json = ConvertTo-JsonCompact $Object
  $bytes = [Text.Encoding]::UTF8.GetBytes($json + "`n")
  [System.Threading.Monitor]::Enter($WriteLock)
  try {
    $Stream.Write($bytes, 0, $bytes.Length)
    $Stream.Flush()
  } finally {
    [System.Threading.Monitor]::Exit($WriteLock)
  }
}

function Get-HostInfo {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  $isSystem = $identity.User.Value -eq "S-1-5-18"
  return @{
    hostname   = $env:COMPUTERNAME
    user       = $identity.Name
    sid        = $identity.User.Value
    is_admin   = $isAdmin
    is_system  = $isSystem
    os         = [Environment]::OSVersion.VersionString
    ps_version = $PSVersionTable.PSVersion.ToString()
    cwd        = (Get-Location).Path
    agent      = $script:AgentName
    version    = $script:AgentVersion
    pid        = $PID
  }
}

function Invoke-OneShot {
  param(
    [string]$Cmd,
    [string]$Shell = "powershell",
    [string]$Cwd = "",
    [int]$TimeoutSec = 120
  )
  if ([string]::IsNullOrWhiteSpace($Cmd)) {
    throw "cmd is required"
  }
  if ($TimeoutSec -le 0) { $TimeoutSec = 120 }
  if ($TimeoutSec -gt 3600) { $TimeoutSec = 3600 }

  $workDir = if ($Cwd -and (Test-Path -LiteralPath $Cwd)) { $Cwd } else { $env:SystemRoot }

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.RedirectStandardInput = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.WorkingDirectory = $workDir
  $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
  $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

  if ($Shell -eq "cmd") {
    $psi.FileName = "$env:SystemRoot\System32\cmd.exe"
    $psi.Arguments = "/d /c $Cmd"
  } else {
    $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $Cmd"
  }

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  [void]$proc.Start()

  $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
  $stderrTask = $proc.StandardError.ReadToEndAsync()
  $exited = $proc.WaitForExit($TimeoutSec * 1000)
  if (-not $exited) {
    try { $proc.Kill() } catch {}
    throw "command timed out after ${TimeoutSec}s"
  }
  [void]$stdoutTask.AsyncWaitHandle.WaitOne(5000)
  [void]$stderrTask.AsyncWaitHandle.WaitOne(5000)

  return @{
    exit   = $proc.ExitCode
    stdout = [string]$stdoutTask.Result
    stderr = [string]$stderrTask.Result
  }
}

function Start-InteractiveSession {
  param(
    [string]$Shell = "powershell",
    [string]$Cwd = "",
    [System.IO.Stream]$Stream,
    [object]$WriteLock
  )
  $sessionId = "s" + (New-MessageId)
  $workDir = if ($Cwd -and (Test-Path -LiteralPath $Cwd)) { $Cwd } else { $env:SystemRoot }

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.RedirectStandardInput = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.WorkingDirectory = $workDir
  $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
  $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

  if ($Shell -eq "cmd") {
    $psi.FileName = "$env:SystemRoot\System32\cmd.exe"
    $psi.Arguments = "/d /q /k"
  } else {
    $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $psi.Arguments = "-NoLogo -NoExit -NoProfile -ExecutionPolicy Bypass"
  }

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  $proc.EnableRaisingEvents = $true

  # Event actions run in a separate runspace — write JSON directly to the stream.
  $outHandler = {
    if ($null -ne $EventArgs.Data) {
      $state = $Event.MessageData
      $payload = '{"op":"shell.out","session":"' + $state.SessionId + '","stream":"stdout","data":' +
        (ConvertTo-Json -InputObject ($EventArgs.Data + "`n") -Compress) + '}'
      try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload + "`n")
        [System.Threading.Monitor]::Enter($state.WriteLock)
        try { $state.Stream.Write($bytes, 0, $bytes.Length); $state.Stream.Flush() }
        finally { [System.Threading.Monitor]::Exit($state.WriteLock) }
      } catch {}
    }
  }
  $errHandler = {
    if ($null -ne $EventArgs.Data) {
      $state = $Event.MessageData
      $payload = '{"op":"shell.out","session":"' + $state.SessionId + '","stream":"stderr","data":' +
        (ConvertTo-Json -InputObject ($EventArgs.Data + "`n") -Compress) + '}'
      try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload + "`n")
        [System.Threading.Monitor]::Enter($state.WriteLock)
        try { $state.Stream.Write($bytes, 0, $bytes.Length); $state.Stream.Flush() }
        finally { [System.Threading.Monitor]::Exit($state.WriteLock) }
      } catch {}
    }
  }

  $state = [pscustomobject]@{
    SessionId = $sessionId
    Stream    = $Stream
    WriteLock = $WriteLock
    Process   = $proc
  }

  $outEvent = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $outHandler -MessageData $state
  $errEvent = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errHandler -MessageData $state
  $exitEvent = Register-ObjectEvent -InputObject $proc -EventName Exited -Action {
    $state = $Event.MessageData
    $code = 0
    try { $code = $state.Process.ExitCode } catch {}
    $payload = '{"op":"shell.exit","session":"' + $state.SessionId + '","code":' + $code + '}'
    try {
      $bytes = [Text.Encoding]::UTF8.GetBytes($payload + "`n")
      [System.Threading.Monitor]::Enter($state.WriteLock)
      try { $state.Stream.Write($bytes, 0, $bytes.Length); $state.Stream.Flush() }
      finally { [System.Threading.Monitor]::Exit($state.WriteLock) }
    } catch {}
  } -MessageData $state

  [void]$proc.Start()
  $proc.BeginOutputReadLine()
  $proc.BeginErrorReadLine()

  [System.Threading.Monitor]::Enter($script:SessionLock)
  try {
    $script:Sessions[$sessionId] = @{
      Process   = $proc
      OutEvent  = $outEvent
      ErrEvent  = $errEvent
      ExitEvent = $exitEvent
      Shell     = $Shell
    }
  } finally {
    [System.Threading.Monitor]::Exit($script:SessionLock)
  }

  return $sessionId
}

function Stop-InteractiveSession {
  param([string]$SessionId)
  [System.Threading.Monitor]::Enter($script:SessionLock)
  try {
    if (-not $script:Sessions.ContainsKey($SessionId)) { return }
    $s = $script:Sessions[$SessionId]
    $script:Sessions.Remove($SessionId)
  } finally {
    [System.Threading.Monitor]::Exit($script:SessionLock)
  }
  foreach ($ev in @($s.OutEvent, $s.ErrEvent, $s.ExitEvent)) {
    try { Unregister-Event -SourceIdentifier $ev.Name -ErrorAction SilentlyContinue } catch {}
    try { Remove-Job -Id $ev.Id -Force -ErrorAction SilentlyContinue } catch {}
  }
  try {
    if (-not $s.Process.HasExited) { $s.Process.Kill() }
  } catch {}
  try { $s.Process.Dispose() } catch {}
}

function Stop-AllSessions {
  $ids = @()
  [System.Threading.Monitor]::Enter($script:SessionLock)
  try { $ids = @($script:Sessions.Keys) } finally { [System.Threading.Monitor]::Exit($script:SessionLock) }
  foreach ($id in $ids) { Stop-InteractiveSession -SessionId $id }
}

function Get-FileStat {
  param([string]$Path)
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  return @{
    path     = $item.FullName
    name     = $item.Name
    is_dir   = [bool]$item.PSIsContainer
    size     = if ($item.PSIsContainer) { 0 } else { [int64]$item.Length }
    mtime    = $item.LastWriteTimeUtc.ToString("o")
    ctime    = $item.CreationTimeUtc.ToString("o")
    attrs    = [string]$item.Attributes
  }
}

function Handle-Request {
  param(
    $Msg,
    [System.IO.Stream]$Stream,
    [object]$WriteLock,
    [ref]$Authenticated,
    [string]$ExpectedToken
  )

  $id = [string]$Msg.id
  $op = [string]$Msg.op

  function Reply-Ok {
    param([hashtable]$Extra)
    $obj = @{ id = $id; ok = $true }
    foreach ($k in $Extra.Keys) { $obj[$k] = $Extra[$k] }
    Send-JsonLine -Stream $Stream -WriteLock $WriteLock -Object $obj
  }
  function Reply-Err {
    param([string]$ErrorText)
    Send-JsonLine -Stream $Stream -WriteLock $WriteLock -Object @{ id = $id; ok = $false; error = $ErrorText }
  }

  try {
    if ($op -eq "auth") {
      $got = [string]$Msg.token
      if (-not $ExpectedToken) {
        Reply-Err "agent has no token configured"
        return
      }
      if ($got -cne $ExpectedToken) {
        Reply-Err "invalid token"
        return
      }
      $Authenticated.Value = $true
      $info = Get-HostInfo
      Reply-Ok @{ whoami = $info.user; is_system = $info.is_system; hostname = $info.hostname }
      return
    }

    if (-not $Authenticated.Value) {
      Reply-Err "authenticate first"
      return
    }

    switch ($op) {
      "ping" {
        Reply-Ok @{ pong = $true; ts = (Get-Date).ToUniversalTime().ToString("o") }
      }
      "info" {
        Reply-Ok (Get-HostInfo)
      }
      "exec" {
        $result = Invoke-OneShot -Cmd ([string]$Msg.cmd) -Shell ($(if ($Msg.shell) { [string]$Msg.shell } else { "powershell" })) `
          -Cwd ([string]$Msg.cwd) -TimeoutSec ($(if ($Msg.timeout) { [int]$Msg.timeout } else { 120 }))
        Reply-Ok $result
      }
      "shell.open" {
        $sid = Start-InteractiveSession -Shell ($(if ($Msg.shell) { [string]$Msg.shell } else { "powershell" })) `
          -Cwd ([string]$Msg.cwd) -Stream $Stream -WriteLock $WriteLock
        Reply-Ok @{ session = $sid }
      }
      "shell.write" {
        $sid = [string]$Msg.session
        [System.Threading.Monitor]::Enter($script:SessionLock)
        try {
          if (-not $script:Sessions.ContainsKey($sid)) { throw "unknown session" }
          $proc = $script:Sessions[$sid].Process
        } finally {
          [System.Threading.Monitor]::Exit($script:SessionLock)
        }
        if ($proc.HasExited) { throw "session exited" }
        $proc.StandardInput.Write([string]$Msg.data)
        $proc.StandardInput.Flush()
        Reply-Ok @{}
      }
      "shell.close" {
        Stop-InteractiveSession -SessionId ([string]$Msg.session)
        Reply-Ok @{}
      }
      "shell.resize" {
        # Windows redirected consoles have no PTY; acknowledge for client compatibility.
        Reply-Ok @{ note = "no pty; resize ignored" }
      }
      "fs.list" {
        $path = [string]$Msg.path
        if (-not (Test-Path -LiteralPath $path)) { throw "path not found" }
        $entries = @()
        Get-ChildItem -LiteralPath $path -Force | ForEach-Object {
          $entries += @{
            name   = $_.Name
            is_dir = [bool]$_.PSIsContainer
            size   = if ($_.PSIsContainer) { 0 } else { [int64]$_.Length }
            mtime  = $_.LastWriteTimeUtc.ToString("o")
          }
        }
        Reply-Ok @{ path = (Resolve-Path -LiteralPath $path).Path; entries = $entries }
      }
      "fs.stat" {
        Reply-Ok (Get-FileStat -Path ([string]$Msg.path))
      }
      "fs.get" {
        $path = [string]$Msg.path
        if (-not (Test-Path -LiteralPath $path) -or (Get-Item -LiteralPath $path).PSIsContainer) {
          throw "not a file"
        }
        $offset = if ($null -ne $Msg.offset) { [int64]$Msg.offset } else { [int64]0 }
        $length = if ($null -ne $Msg.length) { [int]$Msg.length } else { 1024 * 1024 }
        if ($length -le 0 -or $length -gt 4 * 1024 * 1024) { $length = 1024 * 1024 }
        $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
          $fs.Seek($offset, [IO.SeekOrigin]::Begin) | Out-Null
          $buf = New-Object byte[] $length
          $n = $fs.Read($buf, 0, $length)
          $slice = if ($n -lt $length) { $buf[0..([Math]::Max(0, $n - 1))] } else { $buf }
          if ($n -eq 0) { $b64 = "" } else { $b64 = [Convert]::ToBase64String($slice) }
          Reply-Ok @{
            path   = $path
            offset = $offset
            length = $n
            eof    = ($offset + $n) -ge $fs.Length
            size   = $fs.Length
            data_b64 = $b64
          }
        } finally { $fs.Dispose() }
      }
      "fs.put" {
        $path = [string]$Msg.path
        $dir = Split-Path -Parent $path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
          New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $bytes = [Convert]::FromBase64String([string]$Msg.data_b64)
        $append = [bool]$Msg.append
        if ($append -and (Test-Path -LiteralPath $path)) {
          $fs = [IO.File]::Open($path, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        } else {
          $fs = [IO.File]::Open($path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        }
        try {
          $fs.Write($bytes, 0, $bytes.Length)
        } finally { $fs.Dispose() }
        Reply-Ok @{ path = $path; wrote = $bytes.Length }
      }
      "fs.mkdir" {
        $path = [string]$Msg.path
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Reply-Ok @{ path = $path }
      }
      "fs.rm" {
        $path = [string]$Msg.path
        $recurse = [bool]$Msg.recurse
        if ($recurse) {
          Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
        } else {
          Remove-Item -LiteralPath $path -Force -ErrorAction Stop
        }
        Reply-Ok @{}
      }
      default {
        Reply-Err "unknown op: $op"
      }
    }
  } catch {
    Reply-Err ([string]$_.Exception.Message)
  }
}

function Invoke-ClientSession {
  param(
    [System.Net.Sockets.TcpClient]$Client,
    [string]$ExpectedToken
  )
  $stream = $Client.GetStream()
  $reader = New-Object System.IO.StreamReader($stream, [Text.Encoding]::UTF8, $false, 8192, $true)
  $writeLock = New-Object object
  $authenticated = $false

  try {
    Send-JsonLine -Stream $stream -WriteLock $writeLock -Object @{
      op      = "hello"
      agent   = $script:AgentName
      version = $script:AgentVersion
      info    = Get-HostInfo
    }

    while ($Client.Connected) {
      $line = $reader.ReadLine()
      if ($null -eq $line) { break }
      $line = $line.Trim()
      if (-not $line) { continue }
      try {
        $msg = $line | ConvertFrom-Json
      } catch {
        Send-JsonLine -Stream $stream -WriteLock $writeLock -Object @{ ok = $false; error = "invalid json" }
        continue
      }
      Handle-Request -Msg $msg -Stream $stream -WriteLock $writeLock -Authenticated ([ref]$authenticated) -ExpectedToken $ExpectedToken
    }
  } finally {
    Stop-AllSessions
    try { $reader.Dispose() } catch {}
    try { $Client.Close() } catch {}
  }
}

# --- bootstrap ---
$config = Read-AgentConfig -Path $ConfigPath
$token = Get-EffectiveToken -Explicit $Token -Config $config
if (-not $token) {
  Write-AgentLog "No token configured. Set -Token, WINTOOLKIT_TOKEN, or agent.config.json" "ERROR"
  exit 2
}

if ($config) {
  if (-not $PSBoundParameters.ContainsKey("ListenPort") -and $config.listen_port) { $ListenPort = [int]$config.listen_port }
  if (-not $PSBoundParameters.ContainsKey("BindAddress") -and $config.bind_address) { $BindAddress = [string]$config.bind_address }
  if (-not $ConnectHost -and $config.connect_host) { $ConnectHost = [string]$config.connect_host }
  if (-not $PSBoundParameters.ContainsKey("ConnectPort") -and $config.connect_port) { $ConnectPort = [int]$config.connect_port }
}

$info = Get-HostInfo
Write-AgentLog ("Starting {0} v{1} as {2} (system={3})" -f $script:AgentName, $script:AgentVersion, $info.user, $info.is_system)

if ($ConnectHost) {
  Write-AgentLog "Reverse-connect mode → ${ConnectHost}:${ConnectPort}"
  while ($true) {
    $client = $null
    try {
      $client = New-Object System.Net.Sockets.TcpClient
      $client.Connect($ConnectHost, $ConnectPort)
      Write-AgentLog "Connected to ${ConnectHost}:${ConnectPort}"
      Invoke-ClientSession -Client $client -ExpectedToken $token
      Write-AgentLog "Session ended; reconnecting in ${ReconnectSeconds}s"
    } catch {
      Write-AgentLog ("Connect failed: {0}" -f $_.Exception.Message) "WARN"
    } finally {
      if ($client) { try { $client.Close() } catch {} }
    }
    Start-Sleep -Seconds $ReconnectSeconds
  }
} else {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($BindAddress), $ListenPort)
  $listener.Start()
  Write-AgentLog "Listening on ${BindAddress}:${ListenPort}"
  try {
    while ($true) {
      $client = $listener.AcceptTcpClient()
      $remote = $client.Client.RemoteEndPoint.ToString()
      Write-AgentLog "Accepted $remote"
      # Sequential sessions keep the agent simple and safe; one operator at a time.
      try {
        Invoke-ClientSession -Client $client -ExpectedToken $token
      } catch {
        Write-AgentLog ("Session error: {0}" -f $_.Exception.Message) "ERROR"
      }
      Write-AgentLog "Closed $remote"
    }
  } finally {
    $listener.Stop()
  }
}
