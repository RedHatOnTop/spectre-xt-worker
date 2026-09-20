#!/usr/bin/env python3
"""Pin sync by worktree + argv/model, never Orca agentIdentity=gemini."""
from __future__ import annotations

import json
import sys


def pick_pin(terminals: list[dict], cwd: str, model: str | None) -> dict:
    live = [
        t
        for t in terminals
        if t.get("worktreePath") == cwd and t.get("connected") and t.get("writable")
    ]
    if model:
        named = [t for t in live if model.lower() in str(t.get("title") or "").lower()]
        if len(named) == 1:
            return {"ok": True, "handle": named[0].get("handle")}
    if len(live) == 1:
        return {"ok": True, "handle": live[0].get("handle")}
    return {"ok": False, "detail": f"{len(live)} live terminals for {cwd}"}


def main() -> int:
    print(json.dumps({"ok": True, "dry_run": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
