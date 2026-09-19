"""spectre-state CLI. Client of the UDS API except for `serve` / `ingest`."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from time import time

from .client import StateClient, default_db_path, default_socket_path
from .qoder_jsonl import SESSIONS_DIR, load_workers, workers_path
from .server import ingest_workers, serve_forever
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectre-state",
        description="Authoritative Spectre worker-state API client / daemon",
    )
    parser.add_argument("--socket", default=None, help="unix socket path")
    parser.add_argument("--db", default=None, help="sqlite path (serve/ingest)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="GET /v1/health")
    get_p = sub.add_parser("get", help="GET /v1/workers/{worker}/snapshot")
    get_p.add_argument("worker")
    rec_p = sub.add_parser("reconcile", help="POST /v1/reconcile")
    rec_p.add_argument("worker")
    sh_p = sub.add_parser("shadow", help="GET recent shadow divergences")
    sh_p.add_argument("worker")

    serve_p = sub.add_parser("serve", help="run the UDS HTTP daemon")
    serve_p.add_argument("--workers-file", default=None)
    serve_p.add_argument("--sessions-dir", default=str(SESSIONS_DIR))
    serve_p.add_argument("--poll-interval", type=float, default=5.0)
    serve_p.add_argument("--no-shadow", action="store_true")
    serve_p.add_argument(
        "--shadow-log",
        default="/work/logs/worker-state-shadow.jsonl",
    )

    ingest_p = sub.add_parser("ingest", help="one-shot jsonl ingest + optional shadow")
    ingest_p.add_argument("--workers-file", default=None)
    ingest_p.add_argument("--sessions-dir", default=str(SESSIONS_DIR))
    ingest_p.add_argument("--no-shadow", action="store_true")

    args = parser.parse_args(argv)
    sock = args.socket or default_socket_path()
    if args.cmd == "serve":
        return cmd_serve(args, sock)
    if args.cmd == "ingest":
        return cmd_ingest(args)
    client = StateClient(sock)
    if args.cmd == "health":
        status, payload = client.health()
        print(json.dumps(payload, sort_keys=True))
        return 0 if status == 200 and payload.get("ok") else 1
    if args.cmd == "get":
        print(json.dumps(client.snapshot(args.worker), sort_keys=True, indent=2))
        return 0
    if args.cmd == "reconcile":
        status, payload = client.reconcile(args.worker)
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0 if status == 200 else 1
    if args.cmd == "shadow":
        status, payload = client.request("GET", f"/v1/workers/{args.worker}/shadow")
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0 if status == 200 else 1
    return 2


def cmd_serve(args: argparse.Namespace, sock: str) -> int:
    db = args.db or default_db_path()
    store = Store(db)
    workers_file = Path(args.workers_file).expanduser() if args.workers_file else workers_path()
    shadow_log = Path(args.shadow_log).expanduser() if args.shadow_log else None
    if shadow_log and not os_writable(shadow_log):
        shadow_log = Path(db).parent / "worker-state-shadow.jsonl"

    def _stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    serve_forever(
        sock,
        store,
        workers_file=workers_file,
        sessions_dir=Path(args.sessions_dir).expanduser(),
        poll_interval=args.poll_interval,
        shadow=not args.no_shadow,
        shadow_log=shadow_log,
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    db = args.db or default_db_path()
    store = Store(db)
    workers_file = Path(args.workers_file).expanduser() if args.workers_file else workers_path()
    workers = load_workers(workers_file)
    results = ingest_workers(
        store,
        workers,
        Path(args.sessions_dir).expanduser(),
        time(),
        shadow=not args.no_shadow,
    )
    summary = {
        name: {
            "goal": row["snapshot"]["goal"]["state"],
            "park_reason": row["snapshot"]["goal"].get("park_reason"),
            "policy": row["snapshot"]["policy"],
            "shadow": row.get("shadow"),
        }
        for name, row in results.items()
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def os_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            return True
    except OSError:
        return False
