#!/usr/bin/env python3
"""Serve spectre-status over loopback + Tailscale for a live glance.

Binds 127.0.0.1 and the current Tailscale IPv4 only. Port 9091.
  GET /       auto-refresh HTML
  GET /json   spectre-status --json
"""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9091


def _status_json() -> bytes:
    try:
        proc = subprocess.run(
            ["spectre-status", "--json"],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return json.dumps({"error": str(exc)}).encode()
    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or proc.stdout or b"spectre-status failed").strip()
        return json.dumps({"error": err.decode("utf-8", "replace")}).encode()
    return proc.stdout


def _tailscale_ip() -> str | None:
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ip = (proc.stdout or "").strip().splitlines()
    return ip[0] if ip else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/json", "/status.json"):
            self._send(200, _status_json(), "application/json; charset=utf-8")
            return
        if self.path in ("/", "/index.html"):
            html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="3"/>
<title>spectre status</title>
<style>
 body{font:14px/1.4 ui-monospace,monospace;background:#111;color:#ddd;margin:24px}
 h1{font-size:16px;color:#9cf} pre{white-space:pre-wrap}
 .ok{color:#8d8} .bad{color:#f88}
</style></head><body>
<h1>spectre</h1>
<pre id="out">loading…</pre>
<script>
async function tick(){
  try{
    const r=await fetch('/json',{cache:'no-store'});
    const j=await r.json();
    document.getElementById('out').textContent=JSON.stringify(j,null,2);
  }catch(e){
    document.getElementById('out').textContent=String(e);
  }
}
tick();
</script>
</body></html>
"""
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")


def _serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.serve_forever()


def main() -> int:
    hosts = ["127.0.0.1"]
    ts = _tailscale_ip()
    if ts:
        hosts.append(ts)
    threads = []
    for host in hosts:
        t = threading.Thread(target=_serve, args=(host, PORT), daemon=True)
        t.start()
        threads.append(t)
    print("spectre-status-http on " + ", ".join(f"http://{h}:{PORT}" for h in hosts), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
