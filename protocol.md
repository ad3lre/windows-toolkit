# WinToolkit wire protocol

Newline-delimited JSON over TCP (optional TLS). First message from client must be `auth`.

## Framing

- One JSON object per line (`\n`-terminated), UTF-8
- Request/response correlated by `id` (string)
- Server may push events without `id` (e.g. `shell.out`)

## Ops (client → agent)

| op | fields | meaning |
|----|--------|---------|
| `auth` | `token` | Authenticate; required first |
| `info` | | Host identity, privileges, cwd |
| `exec` | `cmd`, `shell?` (`powershell`\|`cmd`), `cwd?`, `timeout?` | One-shot command |
| `shell.open` | `shell?`, `cwd?` | Interactive session |
| `shell.write` | `session`, `data` | stdin to session |
| `shell.resize` | `session`, `cols`, `rows` | best-effort (no PTY on Windows) |
| `shell.close` | `session` | Kill session |
| `fs.list` | `path` | Directory listing |
| `fs.stat` | `path` | File metadata |
| `fs.get` | `path`, `offset?`, `length?` | Read file (base64 chunk) |
| `fs.put` | `path`, `data_b64`, `append?`, `mode?` | Write file chunk |
| `fs.mkdir` | `path` | Create directory tree |
| `fs.rm` | `path`, `recurse?` | Delete |
| `ping` | | Liveness |

## Responses

```json
{"id":"1","ok":true,...}
{"id":"1","ok":false,"error":"..."}
```

## Push events

```json
{"op":"shell.out","session":"s1","stream":"stdout","data":"..."}
{"op":"shell.out","session":"s1","stream":"stderr","data":"..."}
{"op":"shell.exit","session":"s1","code":0}
{"op":"hello","agent":"wintoolkit","version":"1.0.0"}
```
