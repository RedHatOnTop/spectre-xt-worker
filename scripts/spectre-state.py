#!/usr/bin/env python3
"""CLI entry for spectre-worker-state. Installed as /usr/local/bin/spectre-state."""
from __future__ import annotations

import sys
from pathlib import Path


def _prepare() -> None:
    here = Path(__file__).resolve().parent
    candidates = [
        here,
        Path("/usr/local/lib/spectre-worker-state"),
    ]
    for parent in candidates:
        if (parent / "worker_state").is_dir():
            sys.path.insert(0, str(parent))
            return
    raise SystemExit("worker_state package not found")


_prepare()
from worker_state.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
