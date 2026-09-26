#!/usr/bin/env python3
"""Recover an unresponsive worker-state socket without dispatching work."""
from pathlib import Path
import sys

for parent in (Path(__file__).resolve().parent,
               Path(__file__).resolve().parent.parent / "lib/spectre-worker-state",
               Path("/usr/local/lib/spectre-worker-state")):
    if (parent / "worker_state").is_dir():
        sys.path.insert(0, str(parent))
        break

from worker_state.watchdog import main

if __name__ == "__main__":
    raise SystemExit(main())
