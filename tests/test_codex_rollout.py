#!/usr/bin/env python3
"""Rollout parsing and session selection for the Codex session handoff."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import codex_rollout  # noqa: E402

TUI = "01a0a49c-7dc8-7aa2-a4e7-9db6c6c5184b"
EXEC = "01a071d9-1752-7b33-beb4-6a56c1656f24"


def write_rollout(
    codex_home: Path,
    session_id: str,
    *,
    cwd: str = "/ws",
    originator: str = "codex-tui",
    source: str = "cli",
    branch: str | None = None,
    commit: str | None = None,
    ordinal: int = 0,
    ts: str = "2026-09-15T10-00-00",
    day: tuple[str, str, str] = ("2026", "09", "15"),
    suffix: str | None = None,
    records: tuple[dict, ...] = (),
    mtime: float | None = None,
) -> Path:
    """Write one rollout file the way codex writes it."""
    payload: dict[str, object] = {
        "id": session_id,
        "session_id": session_id,
        "cwd": cwd,
        "originator": originator,
        "source": source,
        "cli_version": "0.154.0",
        "timestamp": "2026-09-15T01:00:00.000Z",
    }
    if branch:
        payload["git"] = {"branch": branch, "commit_hash": commit or ("0" * 40)}
    name = f"rollout-{ts}-{session_id}" + (f"_{suffix}" if suffix else "")
    path = codex_home / "sessions" / Path(*day) / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": "2026-09-15T01:00:00.000Z",
                "ordinal": ordinal,
                "type": "session_meta",
                "payload": payload,
            }
        )
    ]
    lines.extend(json.dumps(record) for record in records)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def user_message(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


class MetaTest(unittest.TestCase):
    def test_parses_session_meta_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = write_rollout(
                home,
                TUI,
                cwd="/home/person/Projects/x",
                branch="feat/thing",
                commit="abcdef1234567890",
                ordinal=7,
            )
            meta = codex_rollout.session_meta(path)
            assert meta is not None
            self.assertEqual(meta.id, TUI)
            self.assertEqual(meta.cwd, "/home/person/Projects/x")
            self.assertEqual(meta.originator, "codex-tui")
            self.assertEqual(meta.source, "cli")
            self.assertEqual(meta.git_branch, "feat/thing")
            self.assertEqual(meta.git_commit, "abcdef1234567890")
            self.assertEqual(meta.ordinal, 7)

    def test_rejects_unusable_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            empty = home / "sessions" / "2026" / "09" / "15" / "empty.jsonl"
            empty.parent.mkdir(parents=True)
            empty.write_text("", encoding="utf-8")
            self.assertIsNone(codex_rollout.session_meta(empty))

            partial = empty.with_name("partial.jsonl")
            partial.write_text('{"timestamp":"x","type":"session_met', encoding="utf-8")
            self.assertIsNone(codex_rollout.session_meta(partial))

            wrong = empty.with_name("wrong.jsonl")
            wrong.write_text('{"type":"event_msg","payload":{"id":"x"}}\n', encoding="utf-8")
            self.assertIsNone(codex_rollout.session_meta(wrong))

            noid = empty.with_name("noid.jsonl")
            noid.write_text('{"type":"session_meta","payload":{"cwd":"/ws"}}\n', encoding="utf-8")
            self.assertIsNone(codex_rollout.session_meta(noid))

    def test_missing_file_is_none(self) -> None:
        self.assertIsNone(codex_rollout.session_meta(Path("/nonexistent/rollout.jsonl")))


class FirstUserTextTest(unittest.TestCase):
    def test_skips_injected_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp),
                TUI,
                records=(
                    user_message("# AGENTS.md instructions for /ws\n\nrules"),
                    user_message("<environment_context><cwd>/ws</cwd></environment_context>"),
                    user_message("fix the flaky test\nsecond line"),
                ),
            )
            self.assertEqual(
                codex_rollout.first_user_text(path), "fix the flaky test second line"
            )

    def test_no_user_text_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(
                Path(tmp), TUI, records=(user_message("# AGENTS.md instructions for /ws"),)
            )
            self.assertEqual(codex_rollout.first_user_text(path), "")
S1 = "01a05c93-7824-75c1-a3d1-b0f49f541dc1"
S2 = "01a05caf-265c-7713-8805-959c46f617cd"


class IterSessionsTest(unittest.TestCase):
    def test_groups_shards_of_one_session_by_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_rollout(home, TUI, ordinal=1028, ts="2026-09-01T20-16-17",
                          suffix=S2, mtime=3000)
            write_rollout(home, TUI, ordinal=1026, ts="2026-09-01T19-46-03",
                          suffix=S1, mtime=2000)
            write_rollout(home, TUI, ordinal=0, ts="2026-09-01T15-13-50", mtime=1000)
            sessions = codex_rollout.iter_sessions(home)
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.shard_count, 3)
            self.assertEqual(
                [p.name for p in session.files],
                [
                    f"rollout-2026-09-01T15-13-50-{TUI}.jsonl",
                    f"rollout-2026-09-01T19-46-03-{TUI}_{S1}.jsonl",
                    f"rollout-2026-09-01T20-16-17-{TUI}_{S2}.jsonl",
                ],
            )
            self.assertEqual(session.mtime, 3000)
            self.assertEqual(session.size, sum(p.stat().st_size for p in session.files))
            self.assertEqual(session.path, session.files[-1])

    def test_skips_unusable_and_orders_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_rollout(home, TUI, mtime=1000)
            write_rollout(home, EXEC, originator="codex_exec", source="exec", mtime=5000)
            broken = home / "sessions" / "2026" / "09" / "15" / "partial.jsonl"
            broken.write_text('{"type":"session_met', encoding="utf-8")
            (home / "sessions" / "notes.txt").write_text("ignore", encoding="utf-8")
            sessions = codex_rollout.iter_sessions(home)
            self.assertEqual([s.id for s in sessions], [EXEC, TUI])

    def test_missing_sessions_dir_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(codex_rollout.iter_sessions(Path(tmp)), [])


class SelectTest(unittest.TestCase):
    def _sessions(self, home: Path) -> list[codex_rollout.Session]:
        write_rollout(home, TUI, cwd="/ws/a", mtime=1000, branch="main")
        write_rollout(home, EXEC, cwd="/ws/a", mtime=5000, originator="codex_exec",
                      source="exec")
        write_rollout(home, "01a10000-0000-7000-8000-000000000001", cwd="/ws/b", mtime=9000)
        return codex_rollout.iter_sessions(home)

    def test_default_picks_newest_interactive_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            chosen = codex_rollout.select(sessions, cwd="/ws/a")
            self.assertEqual(chosen.id, TUI)  # the newer exec session is filtered out

    def test_cwd_match_ignores_trailing_slash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            self.assertEqual(codex_rollout.select(sessions, cwd="/ws/a/").id, TUI)

    def test_unknown_cwd_raises_unless_all_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            with self.assertRaises(codex_rollout.SelectionError):
                codex_rollout.select(sessions, cwd="/elsewhere")
    def test_exact_uuid_beats_a_longer_prefix_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_rollout(home, TUI, cwd="/ws/a", mtime=1000)
            write_rollout(home, TUI + "5", cwd="/ws/a", mtime=2000)
            sessions = codex_rollout.iter_sessions(home)
            self.assertEqual(codex_rollout.select(sessions, session_ref=TUI).id, TUI)

    def test_ambiguous_prefix_reports_the_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            with self.assertRaises(codex_rollout.SelectionError) as ctx:
                codex_rollout.select(sessions, session_ref="01a", all_dirs=True)
            self.assertIn("ambiguous", str(ctx.exception))

    def test_non_interactive_session_needs_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            with self.assertRaises(codex_rollout.SelectionError):
                codex_rollout.select(sessions, session_ref=EXEC)
            chosen = codex_rollout.select(
                sessions, session_ref=EXEC, include_non_interactive=True
            )
            self.assertEqual(chosen.id, EXEC)

    def test_id_prefix_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(Path(tmp))
            self.assertEqual(codex_rollout.select(sessions, session_ref="01a0a4").id, TUI)

    def test_no_sessions_at_all(self) -> None:
        with self.assertRaises(codex_rollout.SelectionError):
            codex_rollout.select([], cwd="/ws")


class DescribeTest(unittest.TestCase):
    def test_reports_every_shard_with_hashes_and_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_rollout(home, TUI, cwd="/ws/a", branch="feat/x",
                          commit="abcdef1234567890", ordinal=0,
                          ts="2026-09-01T15-13-50", day=("2026", "09", "01"),
                          records=(user_message("do the thing"),))
            write_rollout(home, TUI, cwd="/ws/a", ordinal=1026, ts="2026-09-01T19-46-03",
                          day=("2026", "09", "01"), suffix=S1,
                          records=(user_message("and more"),))
            session = codex_rollout.iter_sessions(home)[0]
            described = codex_rollout.describe(session, home)
            self.assertEqual(described["id"], TUI)
            self.assertEqual(described["label"], "feat/x@abcdef1")
            self.assertEqual(described["shard_count"], 2)
            self.assertEqual(described["first_user_text"], "do the thing")
            rels = [shard["rel"] for shard in described["shards"]]
            self.assertEqual(
                rels,
                [
                    f"2026/09/01/rollout-2026-09-01T15-13-50-{TUI}.jsonl",
                    f"2026/09/01/rollout-2026-09-01T19-46-03-{TUI}_{S1}.jsonl",
                ],
            )
            for shard in described["shards"]:
                self.assertEqual(len(str(shard["sha256"])), 64)
                self.assertGreater(int(shard["lines"]), 0)
            self.assertEqual(
                described["lines"],
                sum(int(shard["lines"]) for shard in described["shards"]),
            )

    def test_label_is_empty_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_rollout(home, TUI, cwd="/ws/a")
            session = codex_rollout.iter_sessions(home)[0]
            self.assertEqual(codex_rollout.describe(session, home)["label"], "")


class HumanAgeTest(unittest.TestCase):
    def test_buckets(self) -> None:
        self.assertEqual(codex_rollout.human_age(5), "5s")
        self.assertEqual(codex_rollout.human_age(120), "2m")
        self.assertEqual(codex_rollout.human_age(7200), "2h")
        self.assertEqual(codex_rollout.human_age(172800), "2d")
        self.assertEqual(codex_rollout.human_age(-5), "0s")


if __name__ == "__main__":
    unittest.main()
