#!/usr/bin/env python3
"""Minimal protocol-compatible agent for Linux-side client testing (not Windows)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import threading
from pathlib import Path


def send(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())


def handle(conn: socket.socket, token: str) -> None:
    send(
        conn,
        {
            "op": "hello",
            "agent": "wintoolkit-mock",
            "version": "1.0.0-mock",
            "info": {
                "hostname": socket.gethostname(),
                "user": "MOCK\\SYSTEM",
                "is_system": True,
                "is_admin": True,
                "os": "mock",
                "cwd": str(Path.cwd()),
            },
        },
    )
    buf = b""
    authed = False
    fobj = conn.makefile("rb")
    while True:
        line = fobj.readline()
        if not line:
            break
        msg = json.loads(line.decode())
        mid = msg.get("id")
        op = msg.get("op")

        def ok(**kw):
            send(conn, {"id": mid, "ok": True, **kw})

        def err(text: str):
            send(conn, {"id": mid, "ok": False, "error": text})

        if op == "auth":
            if msg.get("token") != token:
                err("invalid token")
                continue
            authed = True
            ok(whoami="MOCK\\SYSTEM", is_system=True, hostname=socket.gethostname())
            continue
        if not authed:
            err("authenticate first")
            continue
        if op == "ping":
            ok(pong=True)
        elif op == "info":
            ok(
                hostname=socket.gethostname(),
                user="MOCK\\SYSTEM",
                is_system=True,
                is_admin=True,
                os="mock-linux",
                ps_version="n/a",
                cwd=str(Path.cwd()),
                agent="wintoolkit-mock",
                version="1.0.0-mock",
                pid=os.getpid(),
            )
        elif op == "exec":
            cmd = msg.get("cmd") or ""
            try:
                p = subprocess.run(
                    ["bash", "-lc", cmd],
                    capture_output=True,
                    text=True,
                    timeout=int(msg.get("timeout") or 120),
                )
                ok(exit=p.returncode, stdout=p.stdout, stderr=p.stderr)
            except Exception as exc:
                err(str(exc))
        elif op == "fs.list":
            path = Path(msg["path"])
            entries = [
                {
                    "name": c.name,
                    "is_dir": c.is_dir(),
                    "size": c.stat().st_size if c.is_file() else 0,
                    "mtime": "",
                }
                for c in sorted(path.iterdir(), key=lambda x: x.name)
            ]
            ok(path=str(path.resolve()), entries=entries)
        elif op == "fs.get":
            path = Path(msg["path"])
            data = path.read_bytes()
            offset = int(msg.get("offset") or 0)
            length = int(msg.get("length") or len(data))
            chunk = data[offset : offset + length]
            ok(
                path=str(path),
                offset=offset,
                length=len(chunk),
                eof=offset + len(chunk) >= len(data),
                size=len(data),
                data_b64=base64.b64encode(chunk).decode(),
            )
        elif op == "fs.put":
            path = Path(msg["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = base64.b64decode(msg.get("data_b64") or "")
            mode = "ab" if msg.get("append") else "wb"
            with path.open(mode) as f:
                f.write(raw)
            ok(path=str(path), wrote=len(raw))
        elif op == "fs.mkdir":
            Path(msg["path"]).mkdir(parents=True, exist_ok=True)
            ok(path=msg["path"])
        elif op == "fs.rm":
            path = Path(msg["path"])
            if path.is_dir():
                import shutil

                shutil.rmtree(path) if msg.get("recurse") else path.rmdir()
            else:
                path.unlink()
            ok()
        elif op == "shell.open":
            err("mock agent: use exec (no interactive shell)")
        else:
            err(f"unknown op: {op}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--port", type=int, default=47382)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    print(f"mock agent on {args.bind}:{args.port}", flush=True)
    while True:
        conn, addr = srv.accept()
        print(f"client {addr}", flush=True)
        try:
            handle(conn, args.token)
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
