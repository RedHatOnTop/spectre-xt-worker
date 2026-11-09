#!/usr/bin/env python3
"""Bash-side logic of codex-handoff: shard verdicts, hints, option parsing.

The script is sourced (its entry point is guarded), so the helpers can be
exercised without a peer. The file-copy path itself needs two machines.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-handoff.sh"

S1 = "2026/09/01/rollout-2026-09-01T15-13-50-01a05b9a-3d5a-7871-9024-548202502b70.jsonl"
S2 = ("2026/09/01/rollout-2026-09-01T19-46-03-01a05b9a-3d5a-7871-9024-548202502b70"
      "_01a05c93-7824-75c1-a3d1-b0f49f541dc1.jsonl")


def session_json(*shards: tuple[str, str]) -> str:
    return json.dumps(
        {
            "id": "01a05b9a-3d5a-7871-9024-548202502b70",
            "cwd": "/home/person/Projects/demo",
            "label": "feat/x@abcdef1",
            "shard_count": len(shards),
            "lines": 2328,
            "shards": [
                {"rel": rel, "sha256": sha, "size": 10, "lines": 1000}
                for rel, sha in shards
            ],
        }
    )


def sh(snippet: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'\n{snippet}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class ClassifyShardsTest(unittest.TestCase):
    def test_new_when_nothing_is_on_the_other_side(self) -> None:
        proc = sh(
            "classify_shards \"$JSON\" \"$STATE\"",
            {
                "JSON": session_json((S1, "a" * 64)),
                "STATE": f"{S1} missing",
            },
        )
        self.assertEqual(proc.stdout.strip(), "new")

    def test_already_when_every_shard_matches(self) -> None:
        proc = sh(
            "classify_shards \"$JSON\" \"$STATE\"",
            {
                "JSON": session_json((S1, "a" * 64), (S2, "b" * 64)),
                "STATE": f"{S1} {'a' * 64}\n{S2} {'b' * 64}",
            },
        )
        self.assertEqual(proc.stdout.strip(), "already")

    def test_partial_when_some_shards_are_missing(self) -> None:
        proc = sh(
            "classify_shards \"$JSON\" \"$STATE\"",
            {
                "JSON": session_json((S1, "a" * 64), (S2, "b" * 64)),
                "STATE": f"{S1} {'a' * 64}\n{S2} missing",
            },
        )
        self.assertEqual(proc.stdout.strip(), "partial")

    def test_differs_when_a_shard_hash_changed(self) -> None:
        proc = sh(
            "classify_shards \"$JSON\" \"$STATE\"",
            {
                "JSON": session_json((S1, "a" * 64)),
                "STATE": f"{S1} {'c' * 64}",
            },
        )
        self.assertEqual(proc.stdout.strip(), "differs")

    def test_differs_wins_over_a_missing_shard(self) -> None:
        proc = sh(
            "classify_shards \"$JSON\" \"$STATE\"",
            {
                "JSON": session_json((S1, "a" * 64), (S2, "b" * 64)),
                "STATE": f"{S1} {'c' * 64}\n{S2} missing",
            },
        )
        self.assertEqual(proc.stdout.strip(), "differs")


class ShardListingTest(unittest.TestCase):
    def test_shard_rels_and_tab(self) -> None:
        data = session_json((S1, "a" * 64), (S2, "b" * 64))
        rels = sh('shard_rels "$JSON"', {"JSON": data})
        self.assertEqual(rels.stdout.split(), [S1, S2])
        tab = sh('shard_tab "$JSON"', {"JSON": data})
        self.assertEqual(
            tab.stdout.strip().split("\n"),
            [f"{S1}\t{'a' * 64}", f"{S2}\t{'b' * 64}"],
        )

    def test_local_shard_state_reports_hashes_and_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "sessions" / S1
            target.parent.mkdir(parents=True)
            target.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            proc = sh(
                'local_shard_state "$RELS"',
                {
                    "CODEX_HOME": str(home),
                    "RELS": f"{S1}\n{S2}",
                },
            )
            lines = proc.stdout.strip().split("\n")
            self.assertEqual(lines[0].split()[0], S1)
            self.assertEqual(len(lines[0].split()[1]), 64)
            self.assertEqual(lines[1], f"{S2} missing")


class HintTest(unittest.TestCase):
    def test_session_name_is_tmux_safe(self) -> None:
        proc = sh('session_name "/home/person/Projects/my repo"')
        self.assertEqual(proc.stdout.strip(), "codex-my-repo")

    def test_session_name_falls_back_to_home(self) -> None:
        proc = sh("session_name ''")
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "person"
        self.assertEqual(proc.stdout.strip(), f"codex-{user}")

    def test_resume_cmd_sources_env_and_resumes(self) -> None:
        proc = sh("resume_cmd 01a05b9a-3d5a-7871-9024-548202502b70")
        out = proc.stdout
        self.assertIn(". ~/.codex/modes/env.sh 2>/dev/null", out)
        self.assertIn("codex resume 01a05b9a-3d5a-7871-9024-548202502b70", out)
        # the directory is tmux's -c, so no cd in the command itself
        self.assertNotIn("cd ", out)

    def test_push_hint_points_at_the_peer(self) -> None:
        proc = sh('print_hint push spectre "$JSON"', {"JSON": session_json((S1, "a" * 64))})
        out = proc.stdout
        self.assertIn("ssh spectre -t 'tmux new-session -A -s codex-demo", out)
        self.assertIn('-c "/home/person/Projects/demo"', out)
        self.assertIn("without tmux there: ssh spectre -t \"cd '/home/person/Projects/demo'", out)
        self.assertIn("warp push", out)

    def test_pull_hint_points_here(self) -> None:
        proc = sh('print_hint pull spectre "$JSON"', {"JSON": session_json((S1, "a" * 64))})
        out = proc.stdout
        self.assertIn('tmux new-session -A -s codex-demo -c "/home/person/Projects/demo"', out)
        self.assertIn("codex resume 01a05b9a", out)

    def test_printed_push_line_survives_a_shell_parse(self) -> None:
        """The hint is meant to be pasted: it must survive two shell levels.

        Level 1 (local paste) has to hand ssh exactly one remote command
        string; level 2 (the peer's shell) has to hand tmux a clean `-c`
        directory and one command string. Both are simulated here by
        eval'ing the printed text with `ssh` / `tmux` stubbed.
        """
        proc = sh('print_hint push spectre "$JSON"', {"JSON": session_json((S1, "a" * 64))})
        lines = [ln.strip() for ln in proc.stdout.splitlines()]
        line = next(ln for ln in lines if ln.startswith("ssh spectre -t"))
        parsed = subprocess.run(
            ["bash", "-n", "-c", line], capture_output=True, text=True, check=False
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)

        level1 = subprocess.run(
            [
                "bash",
                "-c",
                'ssh() { for arg in "$@"; do printf "SSHARG:<%s>\\n" "$arg"; done; }\n'
                'eval "$1"',
                "bash",
                line,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(level1.returncode, 0, level1.stderr)
        self.assertIn("SSHARG:<-t>", level1.stdout)
        remote_cmd = next(
            ln[len("SSHARG:<"):-1]
            for ln in level1.stdout.splitlines()
            if ln.startswith("SSHARG:<tmux")
        )

        level2 = subprocess.run(
            [
                "bash",
                "-c",
                'tmux() { for arg in "$@"; do printf "ARG:<%s>\\n" "$arg"; done; }\n'
                'eval "$1"',
                "bash",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(level2.returncode, 0, level2.stderr)
        self.assertIn("ARG:<-c>", level2.stdout)
        self.assertIn("ARG:</home/person/Projects/demo>", level2.stdout)
        self.assertIn(
            "ARG:<. ~/.codex/modes/env.sh 2>/dev/null; codex resume "
            "01a05b9a-3d5a-7871-9024-548202502b70>",
            level2.stdout,
        )

        # the no-tmux fallback must parse to `cd <dir>` + the resume command
        fallback = next(ln for ln in lines if ln.startswith("without tmux there"))
        fb_args = subprocess.run(
            [
                "bash",
                "-c",
                'ssh() { for arg in "$@"; do printf "SSHARG:<%s>\\n" "$arg"; done; }\n'
                'eval "$1"',
                "bash",
                fallback.split(": ", 1)[1],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        fb_cmd = next(
            ln[len("SSHARG:<"):-1]
            for ln in fb_args.stdout.splitlines()
            if ln.startswith("SSHARG:<cd")
        )
        fb_parsed = subprocess.run(
            [
                "bash",
                "-c",
                'cd() { printf "CD:<%s>\\n" "$*"; }\n'
                'codex() { printf "CODEX:<%s>\\n" "$*"; }\n'
                'eval "$1"',
                "bash",
                fb_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
            # hermetic: no real ~/.codex/modes/env.sh, no real codex state
            env={**os.environ, "HOME": "/nonexistent/codex-handoff-test"},
        )
        self.assertEqual(fb_parsed.returncode, 0, fb_parsed.stderr)
        self.assertIn("CD:</home/person/Projects/demo>", fb_parsed.stdout)
        self.assertIn(
            "CODEX:<resume 01a05b9a-3d5a-7871-9024-548202502b70>", fb_parsed.stdout
        )


class JsonFieldTest(unittest.TestCase):
    def test_reads_strings_and_numbers(self) -> None:
        proc = sh(
            'json_field "$JSON" label; json_field "$JSON" shard_count',
            {"JSON": session_json((S1, "a" * 64))},
        )
        self.assertEqual(proc.stdout.split(), ["feat/x@abcdef1", "1"])

    def test_missing_key_is_empty(self) -> None:
        proc = sh('json_field "$JSON" nope', {"JSON": session_json((S1, "a" * 64))})
        self.assertEqual(proc.stdout.strip(), "")


class OptionParsingTest(unittest.TestCase):
    def test_flags_are_captured(self) -> None:
        proc = sh(
            "parse_options --all --include-non-interactive --replace --open"
            " --peer spectre id8\n"
            'echo "$ALL_DIRS $INCLUDE_NON_INTERACTIVE $REPLACE $OPEN_AFTER'
            ' $PEER_ARG $SESSION_REF"'
        )
        self.assertEqual(proc.stdout.split(), ["1", "1", "1", "1", "spectre", "id8"])

    def test_peer_without_a_host_dies(self) -> None:
        proc = sh("parse_options --peer")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--peer needs a host", proc.stderr)

    def test_unknown_option_dies(self) -> None:
        proc = sh("parse_options --nope")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown option", proc.stderr)

    def test_list_args_carries_json_and_limit(self) -> None:
        proc = sh(
            'ALL_DIRS=0; JSON_OUT=1; LIMIT=3; list_args;'
            ' printf "%s\\n" "${LIST_SELECTION[@]}"'
        )
        tokens = proc.stdout.split()
        self.assertIn("--cwd", tokens)
        self.assertIn("--json", tokens)
        self.assertEqual(tokens[-2:], ["--limit", "3"])


class EntryPointTest(unittest.TestCase):
    def test_sourcing_does_not_run_a_command(self) -> None:
        proc = sh("echo sourced")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "sourced")

    def test_no_command_prints_usage_and_exits_2(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("codex-handoff push", proc.stdout)

    def test_help_exits_zero(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--replace", proc.stdout)

    def test_unknown_command_exits_2(self) -> None:
        proc = subprocess.run(
            ["bash", str(SCRIPT), "frobnicate"], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 2)


class SimulatedPullTest(unittest.TestCase):
    """Drives cmd_pull with a stubbed peer: verdict -> copy -> ledger -> hint.

    The transfer itself cannot run here (it needs a second machine), but
    everything around it can: selection, classification, the copy
    invocation, the ledger and the printed hint.
    """

    def test_pull_copies_missing_shards_and_records_the_handoff(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codexhome"
            (home / "sessions").mkdir(parents=True)
            state = root / "state"
            fixture = session_json((S1, "a" * 64))
            snippet = (
                "resolve_peer() { printf '%s\\n' fedora; }\n"
                "ssh_peer() {\n"
                "  local peer=\"$1\"; shift\n"
                "  local cmd=\"$*\"\n"
                "  case \"${cmd}\" in\n"
                "    'printf %s \"$HOME\"') printf '%s' \"$HOME\" ;;\n"
                "    *meta*) printf '%s' \"$FIXTURE\" ;;\n"
                "    *codex_rollout.py*) printf '%s' /usr/local/lib/spectre-codex/codex_rollout.py ;;\n"
                "    *) return 0 ;;\n"
                "  esac\n"
                "}\n"
                "scp() { local dest=\"${@: -1}\"; mkdir -p \"$(dirname \"${dest}\")\"; : >\"${dest}\"; }\n"
                "cmd_pull --peer fedora --all\n"
            )
            proc = sh(
                snippet,
                {
                    "CODEX_HOME": str(home),
                    "XDG_STATE_HOME": str(state),
                    "FIXTURE": fixture,
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            landed = home / "sessions" / S1
            self.assertTrue(landed.is_file(), proc.stdout)
            self.assertIn("codex-handoff pull: fedora ->", proc.stdout)
            self.assertIn("resume here", proc.stdout)
            ledger = state / "spectre-codex-handoff" / "handoff.jsonl"
            self.assertTrue(ledger.is_file())
            record = json.loads(ledger.read_text(encoding="utf-8").strip())
            self.assertEqual(record["direction"], "pull")
            self.assertEqual(record["peer"], "fedora")
            self.assertEqual(record["id"], "01a05b9a-3d5a-7871-9024-548202502b70")

    def test_pull_refuses_a_fork_point_without_replace(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codexhome"
            target = home / "sessions" / S1
            target.parent.mkdir(parents=True)
            target.write_text("local version\n", encoding="utf-8")
            snippet = (
                "resolve_peer() { printf '%s\\n' fedora; }\n"
                "ssh_peer() {\n"
                "  local peer=\"$1\"; shift\n"
                "  case \"$*\" in\n"
                "    'printf %s \"$HOME\"') printf '%s' \"$HOME\" ;;\n"
                "    *meta*) printf '%s' \"$FIXTURE\" ;;\n"
                "    *) return 0 ;;\n"
                "  esac\n"
                "}\n"
                "scp() { echo 'scp must not run' >&2; return 1; }\n"
                "cmd_pull --peer fedora --all\n"
            )
            proc = sh(
                snippet,
                {
                    "CODEX_HOME": str(home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FIXTURE": session_json((S1, "b" * 64)),
                },
            )
            self.assertEqual(proc.returncode, 3)
            self.assertIn("fork point", proc.stderr)
            self.assertIn("--replace", proc.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "local version\n")


class SimulatedPushTest(unittest.TestCase):
    """cmd_push against a stubbed peer: copy calls, ledger, remote hint."""

    def test_push_copies_shards_and_prints_the_remote_resume_line(self) -> None:
        import tempfile

        sid = "01a05b9a-3d5a-7871-9024-548202502b70"
        rel = f"2026/09/15/rollout-2026-09-15T10-00-00-{sid}.jsonl"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codexhome"
            rollout = home / "sessions" / rel
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-15T01:00:00.000Z",
                        "ordinal": 0,
                        "type": "session_meta",
                        "payload": {
                            "id": sid,
                            "cwd": "/home/person/Projects/demo",
                            "originator": "codex-tui",
                            "source": "cli",
                            "git": {"branch": "feat/x", "commit_hash": "a" * 40},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            trace = root / "trace.log"
            remote_home = str(Path.home())
            snippet = (
                "resolve_peer() { printf '%s\\n' spectre; }\n"
                "ssh_peer() {\n"
                "  local peer=\"$1\"; shift\n"
                "  local cmd=\"$*\"\n"
                "  printf 'ssh_peer:%s\\n' \"${cmd}\" >>\"$TRACE\"\n"
                "  case \"${cmd}\" in\n"
                "    'printf %s \"$HOME\"') printf '%s' \"$HOME\" ;;\n"
                "    *'while read -r rel'*)\n"
                "      # faithful: the peer's loop reads the shard list from stdin\n"
                "      while IFS= read -r rel; do [ -n \"$rel\" ] && printf '%s missing\\n' \"$rel\"; done\n"
                "      ;;\n"
                "    *) return 0 ;;\n"
                "  esac\n"
                "}\n"
                "scp() { printf 'scp:%s\\n' \"$*\" >>\"$TRACE\"; }\n"
                "cmd_push --peer spectre --all\n"
            )
            proc = sh(
                snippet,
                {
                    "CODEX_HOME": str(home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "TRACE": str(trace),
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"  -> spectre: {rel}", proc.stdout)
            self.assertIn(
                "ssh spectre -t 'tmux new-session -A -s codex-demo -c \"/home/person/Projects/demo\"",
                proc.stdout,
            )
            self.assertIn("warp push", proc.stdout)
            calls = trace.read_text(encoding="utf-8")
            self.assertIn(f"install -d -m 0700 '{remote_home}/.codex/sessions/2026/09/15'", calls)
            self.assertIn(f"scp:-q -o StrictHostKeyChecking=accept-new {rollout}", calls)
            user = os.environ.get("USER") or os.environ.get("LOGNAME") or "person"
            self.assertIn(f"{user}@spectre:{remote_home}/.codex/sessions/{rel}.incoming", calls)
            self.assertIn(f"mv -f '{remote_home}/.codex/sessions/{rel}.incoming'", calls)
            record = json.loads(
                (root / "state" / "spectre-codex-handoff" / "handoff.jsonl")
                .read_text(encoding="utf-8")
                .strip()
            )
            self.assertEqual(record["direction"], "push")
            self.assertEqual(record["peer"], "spectre")


if __name__ == "__main__":
    unittest.main()
