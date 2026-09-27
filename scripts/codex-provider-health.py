#!/usr/bin/env python3
"""Select a healthy relay without switching a running Codex process."""
from pathlib import Path
import sys

for parent in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent / 'lib/spectre-worker-state',
               Path('/usr/local/lib/spectre-worker-state')):
    if (parent / 'control_plane').is_dir():
        sys.path.insert(0, str(parent))
        break

from control_plane.cli import provider_main

if __name__ == '__main__':
    raise SystemExit(provider_main())
