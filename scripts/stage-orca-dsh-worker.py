#!/usr/bin/env python3
"""Build a pinned native DSH registration overlay without changing running Orca."""
import argparse
import json
from pathlib import Path
import sys

from orca_runtime.worker_registration import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.source, args.output)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f'stage-orca-dsh-worker: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
