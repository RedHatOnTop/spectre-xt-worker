#!/usr/bin/env python3
"""Codex CLI session rollouts: enumerate, filter and describe them.

Codex writes one rollout file per session:

    $CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl

The first line is a `session_meta` record carrying the session id, the
recorded working directory, the originator, the source and the CLI
version. Codex resolves a session from that file alone (measured
2026-09-15, codex-cli 0.154.0: a rollout copied into a fresh CODEX_HOME
is resolvable by id with no session index and no database present), which
is what makes a handoff between two machines a single file copy.

Used by `codex-handoff` (peer transfer) and by `codex-handoff list`.
No third-party imports, no codex internals.

Subcommands:
  list   newest first; the current directory unless --all
  meta   one session as JSON (explicit id/prefix, or the newest match)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIRNAME = "sessions"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Sessions created by `codex exec` are hidden from the resume picker unless
# --include-non-interactive is passed, so mirror that default here. Both
# fields agree on every session measured on the daily driver, and either
# one alone is enough to classify a record.
NON_INTERACTIVE_ORIGINATORS = frozenset({"codex_exec"})
NON_INTERACTIVE_SOURCES = frozenset({"exec"})
# The first "user" record in a rollout is usually injected context (AGENTS.md,
# environment, plugin menu), not something the user typed. Measured
# 2026-09-15 on 216 local rollouts: 215 of them open with one of these.
INJECTED_PREFIXES = (
    "<recommended_plugins>",
    "<environment_context>",
    "<user_instructions>",
    "<INSTRUCTIONS>",
    "<permissions",
    "<turn_aborted",
    "# AGENTS.md instructions",
)
FIRST_TEXT_LIMIT = 120


class SelectionError(Exception):
    """No session matched, or a reference matched more than one."""


@dataclass(frozen=True)
class RolloutMeta:
    """The first-line session_meta of one rollout file."""

    id: str
    cwd: str
    originator: str
    source: str
    git_branch: str
    git_commit: str
    cli_version: str
    started_at: str
    ordinal: int


@dataclass(frozen=True)
class Session:
    """One Codex session: every rollout shard that carries its id.

    Long sessions are paginated — a base file plus `_<uuid>` shards, each
    with its own first-line ordinal (measured 2026-09-15 on a 2309-record
    session). A handoff has to move the whole set or the history is
    truncated, so shards are grouped here and never treated as separate
    sessions.
    """

    files: tuple[Path, ...]
    id: str
    cwd: str
    originator: str
    source: str
    git_branch: str
    git_commit: str
    cli_version: str
    started_at: str
    mtime: float
    size: int

    @property
    def path(self) -> Path:
        """The newest shard — the file codex appends to."""
        return self.files[-1]

    @property
    def shard_count(self) -> int:
        return len(self.files)

    @property
    def non_interactive(self) -> bool:
        return (
            self.originator in NON_INTERACTIVE_ORIGINATORS
            or self.source in NON_INTERACTIVE_SOURCES
        )

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def label(self) -> str:
        if not self.git_branch:
            return ""
        return f"{self.git_branch}@{self.git_commit[:7]}"


def sessions_root(codex_home: str | os.PathLike[str]) -> Path:
    return Path(codex_home) / SESSIONS_DIRNAME


def session_meta(path: Path) -> RolloutMeta | None:
    """The first-line session_meta of a rollout, or None when unusable.

    A half-written rollout (crash, copy in flight) has no parseable first
    line; callers must treat that as "not a session", never as an error.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        record = json.loads(first)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    timestamp = record.get("timestamp") or payload.get("timestamp") or ""

    def field(key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    git = payload.get("git")
    git_branch = git_commit = ""
    if isinstance(git, dict):
        git_branch = git.get("branch") if isinstance(git.get("branch"), str) else ""
        git_commit = (
            git.get("commit_hash") if isinstance(git.get("commit_hash"), str) else ""
        )
    ordinal = record.get("ordinal")
    return RolloutMeta(
        id=session_id,
        cwd=field("cwd"),
        originator=field("originator"),
        source=field("source"),
        git_branch=git_branch,
        git_commit=git_commit,
        cli_version=field("cli_version"),
        started_at=timestamp if isinstance(timestamp, str) else "",
        ordinal=ordinal if isinstance(ordinal, int) else 0,
    )


def first_user_text(path: Path, limit: int = FIRST_TEXT_LIMIT) -> str:
    """The first user prompt in the rollout, collapsed and bounded.

    Best-effort only: this is a label for `list`, so a rollout that never
    yields text (or fails mid-read) simply returns "".
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                text = _user_text(record.get("type"), payload)
                if text and not _is_injected(text):
                    return " ".join(text.split())[:limit]
    except OSError:
        return ""
    return ""


def _user_text(record_type: object, payload: dict) -> str:
    if record_type == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "user":
            return ""
        return _content_text(payload.get("content"))
    if record_type == "event_msg":
        if payload.get("type") != "user_message":
            return ""
        message = payload.get("message")
        return message if isinstance(message, str) else ""
    return ""


def _is_injected(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(INJECTED_PREFIXES)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
        if isinstance(item, str):
            return item
    return ""


@dataclass(frozen=True)
class _Shard:
    """One rollout file, before shards are grouped into a session."""

    path: Path
    meta: RolloutMeta
    mtime: float
    size: int


def iter_sessions(codex_home: str | os.PathLike[str]) -> list[Session]:
    """Every usable session under <codex_home>/sessions, newest first.

    Shards that share a session id are one session, ordered by their
    first-line ordinal — the order codex replays them in. The session
    keeps the base shard's metadata (its own session_meta) and the newest
    mtime, so "newest session" means "most recently written", not
    "started most recently".
    """
    root = sessions_root(codex_home)
    if not root.is_dir():
        return []
    grouped: dict[str, list[_Shard]] = {}
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        meta = session_meta(path)
        if meta is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        grouped.setdefault(meta.id, []).append(
            _Shard(path=path, meta=meta, mtime=stat.st_mtime, size=stat.st_size)
        )
    found: list[Session] = []
    for shards in grouped.values():
        shards.sort(key=lambda s: (s.meta.ordinal, s.path.name))
        base = shards[0].meta
        found.append(
            Session(
                files=tuple(shard.path for shard in shards),
                id=base.id,
                cwd=base.cwd,
                originator=base.originator,
                source=base.source,
                git_branch=base.git_branch,
                git_commit=base.git_commit,
                cli_version=base.cli_version,
                started_at=base.started_at,
                mtime=max(shard.mtime for shard in shards),
                size=sum(shard.size for shard in shards),
            )
        )
    found.sort(key=lambda s: (s.mtime, s.started_at), reverse=True)
    return found


def _same_dir(left: str, right: str) -> bool:
    return os.path.normpath(left) == os.path.normpath(right)


def select(
    sessions: list[Session],
    session_ref: str | None = None,
    cwd: str | None = None,
    all_dirs: bool = False,
    include_non_interactive: bool = False,
) -> Session:
    """Pick one session: explicit id/prefix, else the newest match.

    Mirrors the resume picker's two filters: the recorded cwd (disabled by
    all_dirs) and non-interactive sessions (disabled by
    include_non_interactive). An explicit id is never filtered by cwd — a
    session started elsewhere is still a valid handoff target.
    """
    candidates = [
        s for s in sessions if include_non_interactive or not s.non_interactive
    ]
    if session_ref:
        candidates = [s for s in candidates if _match_ref(s, session_ref)]
        if not candidates:
            raise SelectionError(f"no session matches '{session_ref}'")
        exact = [s for s in candidates if s.id.lower() == session_ref.lower()]
        if exact:
            return exact[0]
        if len(candidates) > 1:
            listing = ", ".join(
                f"{s.short_id} ({s.cwd or 'no cwd'})" for s in candidates
            )
            raise SelectionError(
                f"'{session_ref}' is ambiguous: {listing} — use a longer prefix"
            )
        return candidates[0]
    if not all_dirs and cwd:
        filtered = [s for s in candidates if s.cwd and _same_dir(s.cwd, cwd)]
        if filtered:
            candidates = filtered
        else:
            raise SelectionError(
                f"no session recorded for {cwd} — pass --all to ignore the directory"
            )
    if not candidates:
        raise SelectionError("no Codex sessions found")
    return candidates[0]


def _match_ref(session: Session, ref: str) -> bool:
    needle = ref.lower()
    if session.id.lower() == needle:
        return True
    if UUID_RE.match(ref):
        return False
    return session.id.lower().startswith(needle)


def shard_records(
    session: Session, codex_home: str | os.PathLike[str]
) -> list[dict[str, object]]:
    """Per-shard rel path, size, record count and sha256, in replay order."""
    root = sessions_root(codex_home)
    records: list[dict[str, object]] = []
    for path in session.files:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        lines = 0
        digest = ""
        size = 0
        try:
            with path.open("rb") as handle:
                for _ in handle:
                    lines += 1
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            size = path.stat().st_size
        except OSError:
            continue
        records.append(
            {"rel": rel, "size": size, "lines": lines, "sha256": digest}
        )
    return records


def _first_user_text_of(session: Session) -> str:
    for path in session.files:
        text = first_user_text(path)
        if text:
            return text
    return ""


def describe(session: Session, codex_home: str | os.PathLike[str]) -> dict[str, object]:
    """Full JSON description of one session: every shard plus totals."""
    shards = shard_records(session, codex_home)
    return {
        "id": session.id,
        "short_id": session.short_id,
        "cwd": session.cwd,
        "originator": session.originator,
        "source": session.source,
        "label": session.label,
        "git_branch": session.git_branch,
        "git_commit": session.git_commit,
        "cli_version": session.cli_version,
        "started_at": session.started_at,
        "mtime": session.mtime,
        "size": session.size,
        "lines": sum(int(shard["lines"]) for shard in shards),
        "shard_count": len(shards),
        "shards": shards,
        "path": str(session.path),
        "rel": str(shards[-1]["rel"]) if shards else "",
        "sha256": str(shards[-1]["sha256"]) if shards else "",
        "non_interactive": session.non_interactive,
        "first_user_text": _first_user_text_of(session),
    }


def human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _print_list(rows: list[dict[str, object]], now: float) -> None:
    if not rows:
        print("no sessions found")
        return
    for row in rows:
        age = human_age(now - float(row["mtime"]))
        kind = "exec" if row["non_interactive"] else "tui"
        detail = " | ".join(
            part
            for part in (str(row["label"]), str(row["first_user_text"]))
            if part
        )
        print(
            "{short}  {age:>4}  {kind:<4}  {size:>9}  {cwd}\n"
            "          {detail}".format(
                short=row["short_id"],
                age=age,
                kind=kind,
                size=f"{int(row['size'])}B",
                cwd=row["cwd"] or "(no cwd recorded)",
                detail=detail or "(no label recorded)",
            )
        )


def cmd_list(args: argparse.Namespace) -> int:
    sessions = [
        s
        for s in iter_sessions(args.codex_home)
        if args.include_non_interactive or not s.non_interactive
    ]
    if not args.all and args.cwd:
        filtered = [s for s in sessions if s.cwd and _same_dir(s.cwd, args.cwd)]
        sessions = filtered
    if args.limit:
        sessions = sessions[: args.limit]
    rows = [describe(s, args.codex_home) for s in sessions]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_list(rows, now=time.time())
    return 0


def cmd_meta(args: argparse.Namespace) -> int:
    try:
        session = select(
            iter_sessions(args.codex_home),
            session_ref=args.id,
            cwd=args.cwd,
            all_dirs=args.all,
            include_non_interactive=args.include_non_interactive,
        )
    except SelectionError as exc:
        print(f"codex_rollout: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(describe(session, args.codex_home), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex CLI session rollouts.")
    parser.set_defaults(func=None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
        help="Codex home directory (default: $CODEX_HOME or ~/.codex)",
    )
    common.add_argument(
        "--cwd", default=None, help="recorded working directory to match"
    )
    common.add_argument(
        "--all", action="store_true", help="ignore the recorded working directory"
    )
    common.add_argument(
        "--include-non-interactive",
        action="store_true",
        help="also consider `codex exec` sessions (codex hides these by default)",
    )

    sub = parser.add_subparsers(dest="command")
    lst = sub.add_parser("list", parents=[common], help="list sessions, newest first")
    lst.add_argument("--json", action="store_true", help="machine-readable output")
    lst.add_argument("--limit", type=int, default=0, help="stop after N sessions")
    lst.set_defaults(func=cmd_list)

    meta = sub.add_parser("meta", parents=[common], help="one session as JSON")
    meta.add_argument("--id", default=None, help="session id or unique prefix")
    meta.set_defaults(func=cmd_meta)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
