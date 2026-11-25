# windows-toolkit

Persistent **Windows SYSTEM shell** reachable from Linux for owned-machine admin and dev work.

```
Linux (wt CLI)  ←TCP/token→  Windows agent (NT AUTHORITY\SYSTEM)
```

## What you get

| From Linux | On Windows (as SYSTEM) |
|------------|-------------------------|
| `wt shell` | Interactive PowerShell or cmd |
| `wt exec` | One-shot commands |
| `wt put` / `wt get` / `wt ls` | File transfer & listing |
| `wt info` | Confirm hostname / `is_system` |
| `wt listen` | Accept reverse dial-ins (NAT-friendly) |

Agent installs as an **ONSTART scheduled task** running as **SYSTEM / Highest**, survives reboot, no logon required.

## Quick start

### 1. Linux — generate token & install CLI

```bash
cd /home/founder/prod/windows-toolkit
chmod +x scripts/wt scripts/pack-agent.sh
mkdir -p ~/.local/bin
ln -sf "$(pwd)/scripts/wt" ~/.local/bin/wt

TOKEN=$(wt gen-token)
wt config set-token "$TOKEN"
# optional:
# wt config set-host 192.168.1.50
```

### 2. Windows — install agent (elevated PowerShell)

Copy `agent/` to the Windows box (or `scripts/pack-agent.sh` → untar), then:

**Listen mode** (Linux dials Windows — open firewall port):

```powershell
cd path\to\agent
.\Install-WinToolkit.ps1 -Token 'PASTE_TOKEN' -Mode listen -ListenPort 47382 -StartNow
New-NetFirewallRule -DisplayName WinToolkit -Direction Inbound -Protocol TCP -LocalPort 47382 -Action Allow
```

**Reverse mode** (Windows dials Linux — better for NAT/home PCs):

```powershell
.\Install-WinToolkit.ps1 -Token 'PASTE_TOKEN' -Mode reverse -ConnectHost 'YOUR.LINUX.IP' -ConnectPort 47382 -StartNow
```

On Linux for reverse:

```bash
wt listen --port 47382
```

### 3. Use it

```bash
wt info 192.168.1.50
# user: NT AUTHORITY\SYSTEM
# is_system: True

wt exec 192.168.1.50 'whoami; $PSVersionTable.PSVersion'
wt shell 192.168.1.50
wt ls 192.168.1.50 'C:\Windows\System32'
wt put ./build.zip 'C:\Temp\build.zip'
wt get 'C:\Windows\System32\drivers\etc\hosts' ./hosts
```

## Layout

```
agent/     WinToolkitAgent.ps1 + Install/Uninstall (SYSTEM task)
client/    wt.py — Linux CLI (stdlib only)
protocol.md
relay/     optional systemd unit sketch for listen mode
scripts/   wt launcher, pack-agent, mock-agent (local test)
```

## Security

- Shared token required on every connection (`~/.config/windows-toolkit/config.json`, mode `0600`).
- Installer locks `%ProgramData%\WinToolkit` to **SYSTEM + Administrators** only.
- Prefer reverse mode or bind to a Tailscale/WireGuard IP instead of exposing `0.0.0.0` on the public internet.
- This is for **machines you own/administer**. Treat the token like a root password.

## Uninstall (Windows, elevated)

```powershell
.\Uninstall-WinToolkit.ps1 -RemoveFiles
```

## Local smoke test (no Windows)

```bash
TOKEN=$(wt gen-token)
python3 scripts/mock-agent.py --token "$TOKEN" --port 47391 &
wt --token "$TOKEN" --port 47391 info 127.0.0.1
wt --token "$TOKEN" --port 47391 exec 127.0.0.1 'echo hello-from-mock'
```
