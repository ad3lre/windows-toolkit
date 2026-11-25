#!/usr/bin/env python3
"""WinToolkit Linux client — SYSTEM shell / files on a Windows agent."""

from __future__ import annotations

import argparse
import base64
import json
import os
import select
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_PORT = 47382
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "windows-toolkit"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "windows-toolkit"
SERVE_SOCK = STATE_DIR / "serve.sock"


class ProtocolError(RuntimeError):
    pass


class WtClient:
    def __init__(self, sock: socket.socket, token: str):
        self.sock = sock
        self.token = token
        self._buf = b""
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_cv = threading.Condition(self._lock)
        self._closed = False
        self.hello: dict[str, Any] = {}
        self._reader = threading.Thread(target=self._read_loop, name="wt-reader", daemon=True)
        self._reader.start()

    @classmethod
    def connect(cls, host: str, port: int, token: str, timeout: float = 15.0) -> "WtClient":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = cls(sock, token)
        client._await_hello(timeout=timeout)
        client.auth(wait=timeout)
        return client

    def close(self) -> None:
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _await_hello(self, timeout: float) -> None:
        deadline = time.time() + timeout
        with self._event_cv:
            while not self.hello and not self._closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("no hello from agent")
                self._event_cv.wait(remaining)

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    line, self._buf = self._buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    self._dispatch(msg)
        except OSError:
            pass
        finally:
            with self._event_cv:
                self._closed = True
                self._event_cv.notify_all()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        with self._event_cv:
            if msg.get("op") == "hello":
                self.hello = msg
                self._event_cv.notify_all()
                return
            mid = msg.get("id")
            if mid and mid in self._pending:
                self._pending[mid] = msg
                self._event_cv.notify_all()
                return
            self._events.append(msg)
            self._event_cv.notify_all()

    def _send(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.sock.sendall(data)

    def request(self, op: str, wait: float = 120.0, **fields: Any) -> dict[str, Any]:
        mid = uuid.uuid4().hex[:12]
        payload = {"id": mid, "op": op, **fields}
        with self._event_cv:
            self._pending[mid] = {}
        self._send(payload)
        deadline = time.time() + wait
        with self._event_cv:
            while True:
                msg = self._pending.get(mid)
                if msg:
                    del self._pending[mid]
                    if not msg.get("ok", False):
                        raise ProtocolError(msg.get("error") or "request failed")
                    return msg
                if self._closed:
                    raise ConnectionError("connection closed")
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._pending.pop(mid, None)
                    raise TimeoutError(f"timeout waiting for {op}")
                self._event_cv.wait(remaining)

    def auth(self, wait: float = 15.0) -> dict[str, Any]:
        return self.request("auth", wait=wait, token=self.token)

    def info(self) -> dict[str, Any]:
        return self.request("info", wait=30.0)

    def exec(self, cmd: str, shell: str = "powershell", cwd: str = "", timeout: int = 120) -> dict[str, Any]:
        fields: dict[str, Any] = {"cmd": cmd, "shell": shell, "timeout": int(timeout)}
        if cwd:
            fields["cwd"] = cwd
        return self.request("exec", wait=float(timeout) + 5.0, **fields)

    def pop_events(self) -> list[dict[str, Any]]:
        with self._event_cv:
            ev = self._events
            self._events = []
            return ev

    def wait_event(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        with self._event_cv:
            while True:
                for i, ev in enumerate(self._events):
                    if predicate(ev):
                        return self._events.pop(i)
                if self._closed:
                    raise ConnectionError("connection closed")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("event wait timed out")
                self._event_cv.wait(remaining)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def resolve_token(cli_token: Optional[str], cfg: dict[str, Any]) -> str:
    token = cli_token or os.environ.get("WINTOOLKIT_TOKEN") or cfg.get("token") or ""
    if not token:
        raise SystemExit(
            "No token. Set WINTOOLKIT_TOKEN, pass --token, or run: wt config set-token <token>"
        )
    return token


def resolve_target(host: Optional[str], port: Optional[int], cfg: dict[str, Any]) -> tuple[str, int]:
    h = host or cfg.get("default_host")
    if not h:
        raise SystemExit("No host. Pass <host> or set default_host via: wt config set-host <host>")
    p = port or int(cfg.get("default_port") or DEFAULT_PORT)
    return h, p


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.config_action == "show":
        redacted = dict(cfg)
        if "token" in redacted and redacted["token"]:
            t = redacted["token"]
            redacted["token"] = t[:4] + "…" + t[-4:] if len(t) > 10 else "***"
        print(json.dumps(redacted, indent=2))
        print(f"(file: {CONFIG_PATH})")
        return 0
    if args.config_action == "set-token":
        cfg["token"] = args.value
        save_config(cfg)
        print(f"token saved to {CONFIG_PATH}")
        return 0
    if args.config_action == "set-host":
        cfg["default_host"] = args.value
        save_config(cfg)
        print(f"default_host={args.value}")
        return 0
    if args.config_action == "set-port":
        cfg["default_port"] = int(args.value)
        save_config(cfg)
        print(f"default_port={args.value}")
        return 0
    raise SystemExit(f"unknown config action: {args.config_action}")


def cmd_gen_token(_: argparse.Namespace) -> int:
    token = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    print(token)
    return 0


class ServeProxy(WtClient):
    """Talk to a local `wt serve` daemon instead of dialing Windows directly."""

    def __init__(self, sock: socket.socket):
        # Skip token auth — serve already authenticated the agent.
        self.sock = sock
        self.token = ""
        self._buf = b""
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_cv = threading.Condition(self._lock)
        self._closed = False
        self.hello = {"op": "hello", "via": "serve"}
        self._reader = threading.Thread(target=self._read_loop, name="wt-serve-proxy", daemon=True)
        self._reader.start()

    @classmethod
    def connect_serve(cls, path: Path = SERVE_SOCK, timeout: float = 5.0) -> "ServeProxy":
        if not path.exists():
            raise ConnectionError(f"wt serve not running (missing {path})")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.settimeout(None)
        proxy = cls(sock)
        # Probe
        st = proxy.request("serve.status", wait=5.0)
        if not st.get("agent_connected"):
            proxy.close()
            raise ConnectionError("wt serve is up but no Windows agent is connected yet")
        return proxy

    def auth(self, wait: float = 15.0) -> dict[str, Any]:
        return self.request("serve.status", wait=wait)


def with_client(args: argparse.Namespace, fn: Callable[[WtClient], int]) -> int:
    cfg = load_config()
    host = getattr(args, "host", None)
    want_serve = host in ("@", "-", "serve", "local")
    no_host = host in (None, "")
    # Explicit serve marker, or no host and daemon sock present (and no default_host).
    if want_serve or (no_host and SERVE_SOCK.exists() and not cfg.get("default_host")):
        client: WtClient = ServeProxy.connect_serve()
    else:
        token = resolve_token(args.token, cfg)
        h, port = resolve_target(host, args.port, cfg)
        client = WtClient.connect(h, port, token)
    try:
        return fn(client)
    finally:
        client.close()


def cmd_info(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        info = c.info()
        for k in (
            "hostname",
            "user",
            "is_system",
            "is_admin",
            "os",
            "ps_version",
            "cwd",
            "pid",
            "version",
        ):
            if k in info:
                print(f"{k}: {info[k]}")
        return 0

    return with_client(args, run)


def cmd_exec(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        result = c.exec(args.command, shell=args.shell, cwd=args.cwd or "", timeout=args.timeout)
        sys.stdout.write(result.get("stdout") or "")
        err = result.get("stderr") or ""
        if err:
            sys.stderr.write(err)
        code = int(result.get("exit") or 0)
        return code

    return with_client(args, run)


def cmd_shell(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        open_fields: dict[str, Any] = {"shell": args.shell}
        if args.cwd:
            open_fields["cwd"] = args.cwd
        resp = c.request("shell.open", wait=30.0, **open_fields)
        session = resp["session"]
        print(f"[wt] interactive {args.shell} session={session}  (Ctrl-D / exit to quit)", file=sys.stderr)

        stop = threading.Event()

        def pump_events() -> None:
            while not stop.is_set():
                for ev in c.pop_events():
                    if ev.get("op") == "shell.out" and ev.get("session") == session:
                        stream = ev.get("stream") or "stdout"
                        data = ev.get("data") or ""
                        if stream == "stderr":
                            sys.stderr.write(data)
                            sys.stderr.flush()
                        else:
                            sys.stdout.write(data)
                            sys.stdout.flush()
                    elif ev.get("op") == "shell.exit" and ev.get("session") == session:
                        print(f"\n[wt] remote exit code={ev.get('code')}", file=sys.stderr)
                        stop.set()
                        return
                time.sleep(0.02)

        t = threading.Thread(target=pump_events, daemon=True)
        t.start()
        try:
            while not stop.is_set():
                if sys.stdin.isatty():
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if not r:
                        continue
                line = sys.stdin.readline()
                if line == "":
                    break
                c.request("shell.write", wait=30.0, session=session, data=line)
        except KeyboardInterrupt:
            print("", file=sys.stderr)
        finally:
            stop.set()
            try:
                c.request("shell.close", wait=10.0, session=session)
            except Exception:
                pass
        return 0

    return with_client(args, run)


def cmd_ls(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        resp = c.request("fs.list", wait=60.0, path=args.path)
        for e in resp.get("entries") or []:
            kind = "d" if e.get("is_dir") else "f"
            size = e.get("size") or 0
            print(f"{kind}  {size:>12}  {e.get('name')}")
        return 0

    return with_client(args, run)


def cmd_get(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        dest = Path(args.local)
        dest.parent.mkdir(parents=True, exist_ok=True)
        offset = 0
        with dest.open("wb") as out:
            while True:
                resp = c.request(
                    "fs.get",
                    wait=120.0,
                    path=args.remote,
                    offset=offset,
                    length=1024 * 1024,
                )
                raw = resp.get("data_b64") or ""
                if raw:
                    chunk = base64.b64decode(raw)
                    out.write(chunk)
                    offset += len(chunk)
                if resp.get("eof"):
                    break
                if not raw:
                    break
        print(f"saved {offset} bytes → {dest}")
        return 0

    return with_client(args, run)


def cmd_put(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        src = Path(args.local)
        data = src.read_bytes()
        chunk_size = 768 * 1024  # base64 expands; keep under 1MiB wire payload
        append = False
        sent = 0
        while sent < len(data) or (sent == 0 and len(data) == 0):
            piece = data[sent : sent + chunk_size]
            b64 = base64.b64encode(piece).decode("ascii")
            c.request(
                "fs.put",
                wait=120.0,
                path=args.remote,
                data_b64=b64,
                append=append,
            )
            append = True
            sent += len(piece)
            if len(data) == 0:
                break
        print(f"uploaded {sent} bytes → {args.remote}")
        return 0

    return with_client(args, run)


def cmd_mkdir(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        c.request("fs.mkdir", wait=30.0, path=args.path)
        print(f"mkdir {args.path}")
        return 0

    return with_client(args, run)


def cmd_rm(args: argparse.Namespace) -> int:
    def run(c: WtClient) -> int:
        c.request("fs.rm", wait=60.0, path=args.path, recurse=bool(args.recurse))
        print(f"removed {args.path}")
        return 0

    return with_client(args, run)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config: {CONFIG_PATH} ({'yes' if CONFIG_PATH.is_file() else 'missing'})")
    print(f"token:  {'set' if (args.token or os.environ.get('WINTOOLKIT_TOKEN') or cfg.get('token')) else 'MISSING'}")
    print(f"serve:  {SERVE_SOCK} ({'up' if SERVE_SOCK.exists() else 'down'})")
    if not SERVE_SOCK.exists():
        return 1
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(str(SERVE_SOCK))
        sock.sendall(b'{"id":"st","op":"serve.status"}\n')
        line = sock.makefile("rb").readline()
        sock.close()
        msg = json.loads(line.decode())
        print(f"agent:  {'connected' if msg.get('agent_connected') else 'waiting'}")
        if msg.get("agent"):
            a = msg["agent"]
            print(f"  host={a.get('hostname')} user={a.get('whoami')} system={a.get('is_system')} peer={a.get('peer')}")
        return 0 if msg.get("ok") else 1
    except OSError as exc:
        print(f"serve:  error ({exc})")
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Persistent reverse-agent listener + local unix control socket for `wt` commands."""
    cfg = load_config()
    token = resolve_token(args.token, cfg)
    port = getattr(args, "serve_port", None) or args.port or int(cfg.get("listen_port") or cfg.get("default_port") or DEFAULT_PORT)
    bind = args.bind or "0.0.0.0"

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if SERVE_SOCK.exists():
        try:
            SERVE_SOCK.unlink()
        except OSError:
            pass

    agent_lock = threading.Lock()
    agent_holder: dict[str, Any] = {"client": None, "meta": None, "peer": None}
    local_clients: list[socket.socket] = []
    local_lock = threading.Lock()

    def broadcast_event(ev: dict[str, Any]) -> None:
        data = (json.dumps(ev, separators=(",", ":")) + "\n").encode()
        with local_lock:
            dead: list[socket.socket] = []
            for lc in local_clients:
                try:
                    lc.sendall(data)
                except OSError:
                    dead.append(lc)
            for d in dead:
                try:
                    local_clients.remove(d)
                except ValueError:
                    pass

    def event_pump() -> None:
        while True:
            with agent_lock:
                c: Optional[WtClient] = agent_holder["client"]
            if c is None:
                time.sleep(0.2)
                continue
            try:
                for ev in c.pop_events():
                    broadcast_event(ev)
            except Exception:
                pass
            time.sleep(0.02)

    threading.Thread(target=event_pump, name="wt-event-pump", daemon=True).start()

    def handle_local(conn: socket.socket) -> None:
        with local_lock:
            local_clients.append(conn)
        try:
            f = conn.makefile("rb")
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    conn.sendall(b'{"ok":false,"error":"invalid json"}\n')
                    continue
                mid = msg.get("id")
                op = msg.get("op")
                if op == "serve.status":
                    with agent_lock:
                        meta = agent_holder["meta"]
                        peer = agent_holder["peer"]
                        alive = agent_holder["client"] is not None and not agent_holder["client"]._closed
                    payload = {
                        "id": mid,
                        "ok": True,
                        "agent_connected": bool(alive),
                        "agent": ({**(meta or {}), "peer": peer} if alive else None),
                        "listen": f"{bind}:{port}",
                    }
                    conn.sendall((json.dumps(payload) + "\n").encode())
                    continue
                with agent_lock:
                    agent: Optional[WtClient] = agent_holder["client"]
                    if agent is None or agent._closed:
                        conn.sendall(
                            (json.dumps({"id": mid, "ok": False, "error": "no agent connected"}) + "\n").encode()
                        )
                        continue
                # Forward to agent
                fields = {k: v for k, v in msg.items() if k not in ("id",)}
                try:
                    op_name = fields.pop("op")
                    wait = float(fields.get("timeout", 120)) + 5.0 if op_name == "exec" else 120.0
                    resp = agent.request(op_name, wait=wait, **fields)
                    resp = dict(resp)
                    resp["id"] = mid
                    conn.sendall((json.dumps(resp, separators=(",", ":")) + "\n").encode())
                except Exception as exc:
                    conn.sendall(
                        (json.dumps({"id": mid, "ok": False, "error": str(exc)}) + "\n").encode()
                    )
        finally:
            with local_lock:
                if conn in local_clients:
                    local_clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def local_accept_loop(srv: socket.socket) -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=handle_local, args=(conn,), daemon=True).start()

    local_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    local_srv.bind(str(SERVE_SOCK))
    os.chmod(SERVE_SOCK, 0o600)
    local_srv.listen(16)
    threading.Thread(target=local_accept_loop, args=(local_srv,), daemon=True).start()

    agent_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    agent_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    agent_srv.bind((bind, port))
    agent_srv.listen(5)
    print(f"[wt] serve listening for agents on {bind}:{port}", flush=True)
    print(f"[wt] local control socket {SERVE_SOCK}", flush=True)

    try:
        while True:
            conn, addr = agent_srv.accept()
            print(f"[wt] agent dial-in from {addr[0]}:{addr[1]}", flush=True)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                client = WtClient(conn, token)
                client._await_hello(timeout=30.0)
                auth = client.auth(wait=30.0)
                meta = {
                    "whoami": auth.get("whoami"),
                    "is_system": auth.get("is_system"),
                    "hostname": auth.get("hostname"),
                }
                print(
                    f"[wt] authenticated {meta.get('hostname')} as {meta.get('whoami')} "
                    f"system={meta.get('is_system')}",
                    flush=True,
                )
                with agent_lock:
                    old = agent_holder["client"]
                    agent_holder["client"] = client
                    agent_holder["meta"] = meta
                    agent_holder["peer"] = f"{addr[0]}:{addr[1]}"
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                # Hold until agent disconnects
                while not client._closed:
                    time.sleep(0.5)
                print(f"[wt] agent disconnected ({addr[0]}:{addr[1]})", flush=True)
            except Exception as exc:
                print(f"[wt] agent session error: {exc}", flush=True)
                try:
                    conn.close()
                except OSError:
                    pass
            finally:
                with agent_lock:
                    if agent_holder.get("peer") == f"{addr[0]}:{addr[1]}":
                        agent_holder["client"] = None
                        agent_holder["meta"] = None
                        agent_holder["peer"] = None
    finally:
        try:
            local_srv.close()
        except OSError:
            pass
        try:
            SERVE_SOCK.unlink()
        except OSError:
            pass
        try:
            agent_srv.close()
        except OSError:
            pass
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """Accept reverse connections from Windows agents; drop into shell/info."""
    cfg = load_config()
    token = resolve_token(args.token, cfg)
    port = args.port or int(cfg.get("listen_port") or DEFAULT_PORT)
    bind = args.bind or "0.0.0.0"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(5)
    print(f"[wt] listening for reverse agents on {bind}:{port}", file=sys.stderr)

    while True:
        conn, addr = srv.accept()
        print(f"[wt] agent connected from {addr[0]}:{addr[1]}", file=sys.stderr)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = WtClient(conn, token)
        try:
            client._await_hello(timeout=30.0)
            auth = client.auth(wait=30.0)
            print(
                f"[wt] authenticated as {auth.get('whoami')} "
                f"system={auth.get('is_system')} host={auth.get('hostname')}",
                file=sys.stderr,
            )
            if args.once_info:
                info = client.info()
                print(json.dumps({k: info.get(k) for k in info if k not in ("id", "ok")}, indent=2))
            else:
                open_fields: dict[str, Any] = {"shell": args.shell}
                if args.cwd:
                    open_fields["cwd"] = args.cwd
                resp = client.request("shell.open", wait=30.0, **open_fields)
                session = resp["session"]
                print(f"[wt] shell session={session}", file=sys.stderr)
                stop = threading.Event()

                def pump() -> None:
                    while not stop.is_set():
                        for ev in client.pop_events():
                            if ev.get("op") == "shell.out" and ev.get("session") == session:
                                data = ev.get("data") or ""
                                if ev.get("stream") == "stderr":
                                    sys.stderr.write(data)
                                    sys.stderr.flush()
                                else:
                                    sys.stdout.write(data)
                                    sys.stdout.flush()
                            elif ev.get("op") == "shell.exit" and ev.get("session") == session:
                                stop.set()
                                return
                        time.sleep(0.02)

                threading.Thread(target=pump, daemon=True).start()
                try:
                    while not stop.is_set():
                        if sys.stdin.isatty():
                            r, _, _ = select.select([sys.stdin], [], [], 0.2)
                            if not r:
                                continue
                        line = sys.stdin.readline()
                        if line == "":
                            break
                        client.request("shell.write", wait=30.0, session=session, data=line)
                except KeyboardInterrupt:
                    print("", file=sys.stderr)
                finally:
                    stop.set()
                    try:
                        client.request("shell.close", wait=10.0, session=session)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[wt] session error: {exc}", file=sys.stderr)
        finally:
            client.close()
        if args.once:
            break
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wt",
        description="WinToolkit — remote Windows SYSTEM shell from Linux",
    )
    p.add_argument("--token", default=None, help="shared secret (or WINTOOLKIT_TOKEN / config)")
    p.add_argument("--port", type=int, default=None, help=f"agent port (default {DEFAULT_PORT})")

    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="manage local client config")
    c.add_argument("config_action", choices=["show", "set-token", "set-host", "set-port"])
    c.add_argument("value", nargs="?", default=None)
    c.set_defaults(func=cmd_config)

    g = sub.add_parser("gen-token", help="generate a strong shared token")
    g.set_defaults(func=cmd_gen_token)

    def add_host(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("host", nargs="?", default=None, help="windows host / IP")

    info = sub.add_parser("info", help="show agent identity / privilege")
    add_host(info)
    info.set_defaults(func=cmd_info)

    ex = sub.add_parser("exec", help="run one command")
    add_host(ex)
    ex.add_argument("command", help="command string")
    ex.add_argument("--shell", choices=["powershell", "cmd"], default="powershell")
    ex.add_argument("--cwd", default="")
    ex.add_argument("--timeout", type=int, default=120)
    ex.set_defaults(func=cmd_exec)

    sh = sub.add_parser("shell", help="interactive PowerShell/cmd")
    add_host(sh)
    sh.add_argument("--shell", choices=["powershell", "cmd"], default="powershell")
    sh.add_argument("--cwd", default="")
    sh.set_defaults(func=cmd_shell)

    ls = sub.add_parser("ls", help="list remote directory")
    add_host(ls)
    ls.add_argument("path", help=r"remote path e.g. C:\Windows")
    ls.set_defaults(func=cmd_ls)

    getp = sub.add_parser("get", help="download remote file")
    add_host(getp)
    getp.add_argument("remote")
    getp.add_argument("local")
    getp.set_defaults(func=cmd_get)

    putp = sub.add_parser("put", help="upload local file")
    add_host(putp)
    putp.add_argument("local")
    putp.add_argument("remote")
    putp.set_defaults(func=cmd_put)

    mk = sub.add_parser("mkdir", help="create remote directory")
    add_host(mk)
    mk.add_argument("path")
    mk.set_defaults(func=cmd_mkdir)

    rm = sub.add_parser("rm", help="delete remote path")
    add_host(rm)
    rm.add_argument("path")
    rm.add_argument("-r", "--recurse", action="store_true")
    rm.set_defaults(func=cmd_rm)

    listen = sub.add_parser("listen", help="accept reverse connections from agents (interactive)")
    listen.add_argument("--bind", default="0.0.0.0")
    listen.add_argument("--shell", choices=["powershell", "cmd"], default="powershell")
    listen.add_argument("--cwd", default="")
    listen.add_argument("--once", action="store_true", help="handle one agent then exit")
    listen.add_argument("--once-info", action="store_true", help="print info and exit (implies --once)")
    listen.set_defaults(func=cmd_listen)

    serve = sub.add_parser("serve", help="persistent daemon: accept reverse agents + local wt proxy")
    serve.add_argument("--bind", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=None, dest="serve_port")
    serve.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="show local serve daemon / agent connection state")
    st.set_defaults(func=cmd_status)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "config" and args.config_action.startswith("set-") and not args.value:
        parser.error(f"config {args.config_action} requires a value")
    if args.cmd == "listen" and args.once_info:
        args.once = True
    try:
        return int(args.func(args) or 0)
    except (ProtocolError, ConnectionError, TimeoutError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
