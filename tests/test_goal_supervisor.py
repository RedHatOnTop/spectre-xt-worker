#!/usr/bin/env python3
"""Unit tests for the Grokbot goal supervisor.

Two layers:

* pure helpers — gates, caps, decision validation, the grok prompt/argv/env
  construction, and the envelope parser
* an offline end-to-end run — a fake watcher, fake grok, fake bridge and fake
  notify in a temp dir, so the whole decision path (gate to guarded dispatch to
  Slack report to persisted state) is exercised without a model call, without a
  box, and without tmux
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "goal-supervisor.py"

spec = importlib.util.spec_from_file_location("goal_supervisor", SCRIPT)
assert spec and spec.loader
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def cfg(**over):
    base = {
        "worker": None,
        "workers_file": ROOT / "config" / "qoder-workers.json",
        "prompt_file": ROOT / "config" / "goal-supervisor-prompt.md",
        "state_file": Path("/nonexistent/goal-supervisor.json"),
        "grok_bin": "/nonexistent/grok",
        "grok_home": Path("/nonexistent/grok-home"),
        "bridge_bin": "spectre-slack-bridge",
        "watcher_bin": "spectre-qoder-goal-watch",
        "notify_bin": "/usr/local/bin/spectre-slack-notify",
        "agent": "grok",
        "channel": "lobby",
        "alerts_channel": "fleet",
        "model": "grok-4.6",
        "effort": "low",
        "max_turns": 6,
        "timeout_sec": 300,
        "goal_max": supervisor.GOAL_MAX,
        "max_per_day": 20,
        "max_per_worker_day": 8,
        "cooldown_sec": 900,
        "max_cost_usd_day": 1.0,
    }
    base.update(over)
    return base


def empty_state():
    return {
        "reviewed": {},
        "escalated": {},
        "workers": {},
        "alerts": {},
        "day": {"date": "", "dispatches": 0, "cost_usd": 0.0},
    }


def parked(turn_id="t-1", reason="goal_budget", **extra):
    pos = {
        "state": "parked",
        "reason": reason,
        "turn_id": turn_id,
        "ts": "2026-09-15T10:00:00+09:00",
    }
    pos.update(extra)
    return pos


class KeyTest(unittest.TestCase):
    def test_normalize_and_fingerprint_ignore_layout(self) -> None:
        self.assertEqual(supervisor.normalize_goal("Fix   the\nsnake_case bug"), "Fix the snake_case bug")
        self.assertEqual(
            supervisor.goal_fingerprint("Fix the snake_case bug"),
            supervisor.goal_fingerprint("  Fix the   snake_case\nbug  "),
        )
        self.assertEqual(len(supervisor.goal_fingerprint("x")), 16)

    def test_review_key_only_for_reviewable_stops(self) -> None:
        self.assertEqual(supervisor.review_key(parked("t-9")), "park:goal_budget:t-9")
        self.assertEqual(
            supervisor.review_key({"state": "idle", "reason": "goal_complete", "turn_id": "t-2"}),
            "done:goal_complete:t-2",
        )
        self.assertIsNone(supervisor.review_key(parked("t-9", reason="plan_gate")))
        self.assertIsNone(
            supervisor.review_key({"state": "active", "reason": "end_turn", "turn_id": "t"})
        )
        self.assertIsNone(supervisor.review_key({"state": "parked", "reason": "goal_budget"}))
        self.assertIsNone(
            supervisor.review_key({"state": "idle", "reason": "end_turn", "turn_id": "t"})
        )


class GateTest(unittest.TestCase):
    def test_park_is_reviewable(self) -> None:
        verdict = supervisor.gate(parked(), {}, cfg(), empty_state(), NOW)
        self.assertEqual((verdict["action"], verdict["reason"]), ("review", "reviewable"))

    def test_policy_false_skips_even_if_parked_shape(self) -> None:
        pos = parked()
        pos["policy"] = {
            "grokbot_may_advance": False,
            "can_resume": False,
            "can_dispatch_goal": False,
        }
        verdict = supervisor.gate(pos, {}, cfg(), empty_state(), NOW)
        self.assertEqual(verdict["action"], "skip")

    def test_policy_true_reviews_budget_park(self) -> None:
        pos = parked()
        pos["policy"] = {"grokbot_may_advance": True, "can_resume": True}
        verdict = supervisor.gate(pos, {}, cfg(), empty_state(), NOW)
        self.assertEqual(verdict["action"], "review")

    def test_plan_gate_escalates_and_is_never_reviewed(self) -> None:
        verdict = supervisor.gate(parked("t-7", reason="plan_gate"), {}, cfg(), empty_state(), NOW)
        self.assertEqual(verdict["action"], "escalate")
        self.assertEqual(verdict["key"], "gate:t-7")

    def test_active_and_unknown_are_skipped(self) -> None:
        state = empty_state()
        self.assertEqual(supervisor.gate({"state": "active"}, {}, cfg(), state, NOW)["reason"], "active")
        self.assertEqual(
            supervisor.gate({"state": "unknown"}, {}, cfg(), state, NOW)["reason"], "unknown_state"
        )

    def test_handled_stop_is_not_reviewed_twice(self) -> None:
        state = empty_state()
        state["reviewed"][supervisor.review_key(parked())] = NOW - 10
        self.assertEqual(supervisor.gate(parked(), {}, cfg(), state, NOW)["reason"], "already_handled")

    def test_caps_and_cooldown(self) -> None:
        state = empty_state()
        state["day"] = {"date": supervisor.day_key(NOW), "dispatches": 0, "cost_usd": 5.0}
        self.assertEqual(supervisor.gate(parked(), {}, cfg(), state, NOW)["reason"], "cost_cap")

        state["day"] = {"date": supervisor.day_key(NOW), "dispatches": 20, "cost_usd": 0.0}
        self.assertEqual(supervisor.gate(parked(), {}, cfg(), state, NOW)["reason"], "day_cap")

        state["day"] = {"date": supervisor.day_key(NOW), "dispatches": 0, "cost_usd": 0.0}
        worker = {"day": supervisor.day_key(NOW), "count": 8}
        self.assertEqual(supervisor.gate(parked(), worker, cfg(), state, NOW)["reason"], "worker_day_cap")

        worker = {"day": supervisor.day_key(NOW), "count": 1, "last_at": NOW - 60}
        self.assertEqual(supervisor.gate(parked(), worker, cfg(), state, NOW)["reason"], "cooldown")
        worker["last_at"] = NOW - 901
        self.assertEqual(supervisor.gate(parked(), worker, cfg(), state, NOW)["action"], "review")

    def test_yesterday_does_not_count_today(self) -> None:
        state = empty_state()
        state["day"] = {"date": "2000-01-01", "dispatches": 99, "cost_usd": 99.0}
        worker = {"day": "2000-01-01", "count": 99, "last_at": NOW - 4000}
        self.assertEqual(supervisor.gate(parked(), worker, cfg(), state, NOW)["action"], "review")
        self.assertEqual(supervisor.cost_today(state, NOW), 0.0)
        self.assertEqual(supervisor.dispatches_today(state, NOW), 0)


class DecisionTest(unittest.TestCase):
    def test_valid_decisions(self) -> None:
        for decision in ("resume", "goal", "stop", "escalate"):
            payload = {
                "decision": decision,
                "goal": "Shrink the parser" if decision == "goal" else "",
                "rationale": "because the evidence says so",
                "evidence": ["git log: abc123 fix parser"],
            }
            ok, problem = supervisor.validate_decision(payload)
            self.assertTrue(ok, f"{decision}: {problem}")

    def test_rejections(self) -> None:
        base = {"decision": "resume", "rationale": "r", "evidence": ["e"]}
        cases = {
            "unknown decision": {**base, "decision": "party"},
            "no evidence": {**base, "evidence": []},
            "no evidence key": {"decision": "resume", "rationale": "r"},
            "blank evidence": {**base, "evidence": ["   "]},
            "empty rationale": {**base, "rationale": "  "},
            "too many evidence": {**base, "evidence": ["a", "b", "c", "d", "e"]},
            "goal without text": {**base, "decision": "goal", "goal": ""},
            "not an object": [],
        }
        for label, payload in cases.items():
            ok, _ = supervisor.validate_decision(payload)
            self.assertFalse(ok, label)

    def test_goal_length_cap(self) -> None:
        payload = {"decision": "goal", "goal": "x" * 10, "rationale": "r", "evidence": ["e"]}
        self.assertTrue(supervisor.validate_decision(payload, goal_max=10)[0])
        self.assertFalse(supervisor.validate_decision(payload, goal_max=9)[0])

    def test_decision_of_normalizes(self) -> None:
        got = supervisor.decision_of(
            {
                "decision": "goal",
                "goal": "  Ship   the  fix ",
                "rationale": "  because ",
                "evidence": [" a ", "b"],
            }
        )
        self.assertEqual(got["goal"], "Ship the fix")
        self.assertEqual(got["rationale"], "because")
        self.assertEqual(got["evidence"], ["a", "b"])

    def test_repeat_guard_reads_recent_fingerprints(self) -> None:
        worker = {"goals": ["aaaa", "bbbb", "cccc", "dddd"]}
        self.assertTrue(supervisor.repeats_last_goal("aaaa", worker))
        self.assertFalse(supervisor.repeats_last_goal("dddd", worker))
        self.assertFalse(supervisor.repeats_last_goal("aaaa", {}))
class PromptArgvEnvTest(unittest.TestCase):
    def test_render_prompt_substitutes_and_fails_closed(self) -> None:
        template = "w={{WORKER}} p={{PROBE_JSON}} r={{REPO_EVIDENCE}} s={{SESSION_TAIL}} a={{PRIOR_ACTIONS}}"
        rendered = supervisor.render_prompt(
            template,
            {
                "WORKER": "qoder",
                "PROBE_JSON": "{}",
                "REPO_EVIDENCE": "log",
                "SESSION_TAIL": "tail",
                "PRIOR_ACTIONS": "(none)",
            },
        )
        self.assertIn("w=qoder", rendered)
        self.assertNotIn("{{", rendered)
        with self.assertRaises(ValueError):
            supervisor.render_prompt(template, {"WORKER": "qoder"})
        with self.assertRaises(ValueError):
            supervisor.render_prompt("literal {{NOT_A_PLACEHOLDER}}", {})

    def test_shipped_template_has_every_placeholder(self) -> None:
        template = (ROOT / "config" / "goal-supervisor-prompt.md").read_text(encoding="utf-8")
        for name in supervisor.PLACEHOLDERS:
            self.assertIn("{{" + name + "}}", template, name)

    def test_build_grok_args_is_read_only_and_schema_bound(self) -> None:
        args = supervisor.build_grok_args("prompt text", cfg())
        self.assertEqual(args[args.index("-p") + 1], "prompt text")
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertEqual(args[args.index("--tools") + 1], "Read")
        self.assertEqual(args[args.index("-m") + 1], "grok-4.6")
        self.assertEqual(args[args.index("--effort") + 1], "low")
        self.assertEqual(args[args.index("--max-turns") + 1], "6")
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["decision"]["enum"], list(supervisor.DECISIONS))
        denied = [args[i + 1] for i, arg in enumerate(args) if arg == "--deny"]
        for tool in ("Write", "Edit", "MultiEdit", "Notebook", "Bash", "BashOutput", "Agent", "Task", "WebFetch", "WebSearch"):
            self.assertIn(tool, denied, tool)
        # grok's own vocabulary only: a Claude name like NotebookEdit aborts the
        # run during argument validation ("unsupported tool prefix"), before any
        # model call — proven the hard way on 2026-09-15.
        self.assertNotIn("NotebookEdit", denied)
        self.assertIn("--no-plan", args)
        self.assertIn("--no-auto-update", args)
        self.assertEqual(args.count("-p"), 1)

    def test_one_line_log_redacts_and_bounds(self) -> None:
        self.assertEqual(
            supervisor.one_line_log('Error: --deny "X": unsupported tool prefix\nsecond line'),
            'Error: --deny "X": unsupported tool prefix second line',
        )
        self.assertNotIn("xoxb-secret", supervisor.one_line_log("token xoxb-secretvalue123 here"))
        self.assertEqual(len(supervisor.one_line_log("y" * 500)), 200)

    def test_cost_and_dispatch_accounting(self) -> None:
        state = empty_state()
        supervisor.record_cost(state, NOW, 0.02)
        supervisor.record_dispatch(state, "qoder", NOW, "abc123")
        # Spend is booked once: record_dispatch must not add it again, or the
        # effective USD cap would silently halve.
        self.assertAlmostEqual(state["day"]["cost_usd"], 0.02, places=6)
        self.assertEqual(state["day"]["dispatches"], 1)
        self.assertEqual(state["day"]["date"], supervisor.day_key(NOW))
        self.assertEqual(supervisor.cost_today(state, NOW), 0.02)
        self.assertEqual(state["workers"]["qoder"]["count"], 1)
        self.assertEqual(state["workers"]["qoder"]["goals"], ["abc123"])
        self.assertEqual(supervisor.worker_action_count(state["workers"]["qoder"], NOW), 1)
        # A new day restarts the counters (the state keeps only today's, which
        # is all the caps need) but keeps the repeat-guard history.
        supervisor.record_cost(state, NOW + 86_400, 0.5)
        self.assertAlmostEqual(supervisor.cost_today(state, NOW + 86_400), 0.5, places=6)
        self.assertEqual(supervisor.cost_today(state, NOW), 0.0)
        self.assertEqual(supervisor.dispatches_today(state, NOW + 86_400), 0)
        self.assertEqual(state["workers"]["qoder"]["goals"], ["abc123"])

    def test_reviewer_env_carries_only_the_configured_credential(self) -> None:
        baseline = supervisor.reviewer_env("/tmp/grok-home")
        self.assertNotIn("XAI_API_KEY", baseline)
        self.assertNotIn("GROK_CODE_XAI_API_KEY", baseline)
        keyed = supervisor.reviewer_env("/tmp/grok-home", {"XAI_API_KEY": "xai-test-value"})
        self.assertEqual(keyed["XAI_API_KEY"], "xai-test-value")
        # An empty or whitespace value is not a credential.
        self.assertNotIn("XAI_API_KEY", supervisor.reviewer_env("/tmp/gh", {"XAI_API_KEY": "  "}))

    def test_grok_env_keys_takes_nothing_but_the_credential_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grok.env"
            path.write_text(
                "# comment\n"
                "XAI_API_KEY=xai-test-value\n"
                "SLACK_BOT_TOKEN=xoxb-not-for-the-reviewer\n"
                "OTHER_THING=1\n",
                encoding="utf-8",
            )
            got = supervisor.grok_env_keys(path)
            self.assertEqual(got, {"XAI_API_KEY": "xai-test-value"})
            self.assertNotIn("SLACK_BOT_TOKEN", got)
            self.assertNotIn("OTHER_THING", got)
            self.assertEqual(supervisor.grok_env_keys(Path(tmp) / "missing.env"), {})

    def test_classify_grok_failure_not_signed_in(self) -> None:
        # The exact headless wording from grok 1.0.30 with no credential.
        text = (
            "Error: Not signed in. To authenticate without a browser, run:\n"
            "  grok login --device-code\n\n"
            "Alternatively, set the XAI_API_KEY environment variable or run `grok login` "
            "on a machine with a browser.\n"
        )
        self.assertEqual(supervisor.classify_grok_failure(text), "auth_failed")

    def test_classify_grok_failure_bad_api_key(self) -> None:
        # Probed 2026-09-15 with the api_key pin and a shape-valid dummy key: the
        # CLI reached the API and the server answered 400. No "auth" substring.
        text = (
            'Internal error: {"message": "API error (status 400 Bad Request): '
            'invalid-argument: Incorrect API key provided. You can obtain an API key '
            'from https://console.x.ai.", "http_status": 400}'
        )
        self.assertEqual(supervisor.classify_grok_failure(text), "auth_failed")

    def test_shipped_profile_defaults_to_the_browser_login(self) -> None:
        import tomllib

        text = (ROOT / "config" / "grok-supervisor" / "config.toml").read_text(encoding="utf-8")
        profile = tomllib.loads(text)
        # Default is the device-code session (one browser approval, self-refreshing
        # token): no auth pin, so a session token is used as-is.
        self.assertNotIn("auth", profile)
        # The no-login alternative stays documented, not active: an api_key pin
        # with no XAI_API_KEY present would fail closed.
        self.assertIn("preferred_method", text)
        self.assertEqual(profile["disabled_mcp_servers"], [])
        self.assertIs(profile["compat"]["claude"]["mcps"], False)

    def test_reviewer_env_is_built_from_nothing(self) -> None:
        env = supervisor.reviewer_env("/tmp/grok-home")
        self.assertEqual(env["GROK_HOME"], "/tmp/grok-home")
        self.assertEqual(env["GROK_DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(env["GROK_MEMORY"], "0")
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        self.assertNotIn("SLACK_APP_TOKEN", env)
        self.assertNotIn("SPECTRE_GOAL_SUPERVISOR", env)
        # PATH is pinned: the reviewer cannot inherit a user bin dir.
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_worker_block_reports_the_injection_path(self) -> None:
        block = supervisor.worker_block(
            "qoder", {"cwd": "/work/x", "tmux": "qoder"}, parked(num_turns=1000)
        )
        self.assertIn("worker: qoder", block)
        self.assertIn("cwd: /work/x", block)
        self.assertIn("tmux target: qoder", block)
        self.assertIn("num_turns: 1000", block)
        native = supervisor.worker_block("zzbrush", {"cwd": "/work/z", "tmux": None}, parked())
        self.assertIn("orca native terminal", native)

    def test_dispatch_argv_shape(self) -> None:
        goal = supervisor.dispatch_argv(cfg(), "goal", "qoder", "Fix the parser", False)
        self.assertEqual(goal[:5], ["spectre-slack-bridge", "--dispatch", "goal", "qoder", "Fix the parser"])
        self.assertIn("--operator", goal)
        self.assertNotIn("--dry-run", goal)
        resume = supervisor.dispatch_argv(cfg(), "resume", "pugc", "", True)
        self.assertEqual(resume[2:4], ["resume", "pugc"])
        self.assertIn("--dry-run", resume)

    def test_missing_binary_recognizes_python_and_node_phrasings(self) -> None:
        self.assertTrue(supervisor.missing_binary({"errno": 2, "error": "FileNotFoundError: ..."}))
        self.assertTrue(supervisor.missing_binary({"errno": None, "error": "spawn ENOENT"}))
        self.assertFalse(supervisor.missing_binary({"errno": None, "error": "exit=1"}))
class PostFormatTest(unittest.TestCase):
    def test_review_post_carries_decision_goal_and_evidence(self) -> None:
        text = supervisor.format_review_post(
            "qoder",
            parked(),
            {
                "decision": "goal",
                "goal": "Shrink the parser",
                "rationale": "budget exhausted",
                "evidence": ["git log: abc123"],
            },
            "dispatch: dispatch_sent (tmux qoder, chars 812)",
        )
        self.assertIn("new unit for qoder", text)
        self.assertIn("Shrink the parser", text)
        self.assertIn("budget exhausted", text)
        self.assertIn("abc123", text)
        self.assertIn("dispatch_sent", text)

    def test_escalation_and_alert_posts(self) -> None:
        text = supervisor.format_escalation_post("qoder", parked(reason="plan_gate"), "note")
        self.assertIn("plan approval", text)
        self.assertIn("approve or deny it in the orca UI", text)
        alert = supervisor.format_alert_post("quota_exhausted", "worker=qoder")
        self.assertIn("goal supervisor skipped: quota_exhausted", alert)

    def test_alert_throttle_holds_for_the_cooldown(self) -> None:
        state = empty_state()
        self.assertTrue(supervisor.alert_throttled(state, "grok:quota_exhausted", NOW))
        self.assertFalse(supervisor.alert_throttled(state, "grok:quota_exhausted", NOW + 60))
        self.assertTrue(supervisor.alert_throttled(state, "grok:quota_exhausted", NOW + 3601))
        self.assertTrue(supervisor.alert_throttled(state, "grok:auth_failed", NOW + 1))


def write_script(path: Path, body: str) -> Path:
    path.write_text(body.lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


WATCHER_BODY = """
#!/bin/bash
printf '%s\\n' 'PAYLOAD'
"""

GROK_BODY = """
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if "-p" in args else ""
with open("__LOG__", "w", encoding="utf-8") as fh:
    json.dump({"args": args, "env": dict(os.environ), "prompt": prompt, "cwd": os.getcwd()}, fh)
mode = "__MODE__"
if mode == "quota":
    print("API error (status 402 Payment Required): Grok Build usage balance exhausted", file=sys.stderr)
    sys.exit(1)
if mode == "invalid":
    payload = {"decision": "goal", "goal": "x", "rationale": "no evidence attached"}
elif mode == "stop":
    payload = {"decision": "stop", "goal": "", "rationale": "clean finish",
               "evidence": ["git log: abc123 done"]}
else:
    payload = {"decision": "goal", "goal": "Shrink the parser",
               "rationale": "budget exhausted mid-unit",
               "evidence": ["git log: abc123 parser wip"]}
print(json.dumps({"stopReason": "end_turn", "num_turns": 3, "total_cost_usd": 0.02,
                  "structuredOutput": payload}))
"""

BRIDGE_BODY = """
#!/usr/bin/env python3
import json, sys
with open("__LOG__", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"ok": True, "evt": "dispatch_sent", "tmux": "qoder", "chars": 812}))
"""

NOTIFY_BODY = """
#!/usr/bin/env python3
import json, sys
with open("__LOG__", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print("slack-notify: posted lobby ts=1.5")
"""
class IntegrationTest(unittest.TestCase):
    """Offline end-to-end: fake watcher/grok/bridge/notify, no model, no tmux."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "state.json"
        self.grok_log = self.dir / "grok.json"
        self.bridge_log = self.dir / "bridge.log"
        self.notify_log = self.dir / "notify.log"
        self.review_cwd = self.dir / "review-cwd"
        self.workers_file = self.dir / "workers.json"
        self.workers_file.write_text(
            json.dumps({"workers": {"qoder": {"cwd": str(self.dir), "tmux": "qoder"}}}),
            encoding="utf-8",
        )
        self.bridge = write_script(
            self.dir / "fake-bridge", BRIDGE_BODY.replace("__LOG__", str(self.bridge_log))
        )
        self.notify = write_script(
            self.dir / "fake-notify", NOTIFY_BODY.replace("__LOG__", str(self.notify_log))
        )
        self.grok = write_script(
            self.dir / "fake-grok",
            GROK_BODY.replace("__LOG__", str(self.grok_log)).replace("__MODE__", "ok"),
        )
        self.watcher = write_script(
            self.dir / "fake-watcher", WATCHER_BODY.replace("PAYLOAD", self.park_payload())
        )
        sys.path.insert(0, str(ROOT / "scripts"))
        from worker_state.server import Handler, UnixHTTPServer
        from worker_state.store import Store

        self.store = Store(self.dir / "worker-state.sqlite")
        self.seed_park()
        self.sock = str(self.dir / "state.sock")
        Handler.store = self.store
        self.server = UnixHTTPServer(self.sock, Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self._old_sock = os.environ.get("SPECTRE_WORKER_STATE_SOCK")
        os.environ["SPECTRE_WORKER_STATE_SOCK"] = self.sock

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        if self._old_sock is None:
            os.environ.pop("SPECTRE_WORKER_STATE_SOCK", None)
        else:
            os.environ["SPECTRE_WORKER_STATE_SOCK"] = self._old_sock
        self.tmp.cleanup()

    def seed_park(self, reason: str = "goal_budget", turn_id: str = "t-42") -> None:
        from worker_state.types import iso_from

        self.store.ingest(
            {
                "event_id": f"park-{reason}-{turn_id}",
                "worker": "qoder",
                "kind": "goal.parked",
                "source": "session_jsonl",
                "turn_id": turn_id,
                "source_timestamp": iso_from(NOW),
                "payload": {"park_reason": reason, "num_turns": 1000},
            },
            NOW,
        )

    def park_payload(self, reason: str = "goal_budget", turn_id: str = "t-42") -> str:
        return json.dumps(
            {
                "worker": "qoder",
                "state": "parked",
                "reason": reason,
                "turn_id": turn_id,
                "ts": "2026-09-15T10:00:00+09:00",
                "num_turns": 1000 if reason == "goal_budget" else None,
                "segment": "",
                "tmux": True,
            }
        )

    def run_supervisor(self, *extra: str, enabled: bool = True) -> int:
        args = [
            "--scan",
            "--workers-file",
            str(self.workers_file),
            "--prompt-file",
            str(ROOT / "config" / "goal-supervisor-prompt.md"),
            "--state-file",
            str(self.state_file),
            "--watcher-bin",
            str(self.watcher),
            "--grok-bin",
            str(self.grok),
            "--grok-home",
            str(self.dir / "grok-home"),
            "--grok-env-file",
            str(self.dir / "grok.env"),
            "--review-cwd",
            str(self.review_cwd),
            "--bridge-bin",
            str(self.bridge),
            "--notify-bin",
            str(self.notify),
            *extra,
        ]
        if enabled:
            args.append("--enable")
        saved = os.environ.pop("SPECTRE_GOAL_SUPERVISOR", None)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = supervisor.main(args)
        finally:
            if saved is not None:
                os.environ["SPECTRE_GOAL_SUPERVISOR"] = saved
        self.last_stdout = out.getvalue()
        return code

    def read_log(self, path: Path) -> list:
        if not path.is_file():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def post_texts(self, channel: str = "lobby") -> list:
        out = []
        for call in self.read_log(self.notify_log):
            if channel in call and "--text" in call:
                out.append(call[call.index("--text") + 1])
        return out
    def test_api_key_path_needs_no_login_and_leaks_nowhere(self) -> None:
        key = "xai-test-value-not-a-real-key"
        (self.dir / "grok.env").write_text(
            f"# reviewer credential\nXAI_API_KEY={key}\nSLACK_BOT_TOKEN=xoxb-decoy\n",
            encoding="utf-8",
        )
        self.assertEqual(self.run_supervisor(), 0)
        grok = json.loads(self.grok_log.read_text(encoding="utf-8"))
        # The key is the credential the isolated home has no auth.json for...
        self.assertEqual(grok["env"]["XAI_API_KEY"], key)
        # ...and nothing else from that file goes with it.
        self.assertNotIn("SLACK_BOT_TOKEN", grok["env"])
        # No credential value in the posts (or anything else printed).
        for call in self.read_log(self.notify_log):
            self.assertNotIn(key, json.dumps(call))
        self.assertNotIn(key, self.last_stdout)

    def test_probe_shows_credential_names_only(self) -> None:
        key = "xai-test-value-not-a-real-key"
        (self.dir / "grok.env").write_text(f"XAI_API_KEY={key}\n", encoding="utf-8")
        args = [
            "--probe",
            "qoder",
            "--workers-file",
            str(self.workers_file),
            "--state-file",
            str(self.state_file),
            "--watcher-bin",
            str(self.watcher),
            "--grok-bin",
            str(self.grok),
            "--grok-env-file",
            str(self.dir / "grok.env"),
            "--bridge-bin",
            str(self.bridge),
            "--notify-bin",
            str(self.notify),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(supervisor.main(args), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["grok_env_keys"], ["XAI_API_KEY"])
        self.assertNotIn(key, out.getvalue())

    def test_goal_decision_dispatches_once_then_dedups(self) -> None:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-decoy-must-not-reach-the-model"
        try:
            self.assertEqual(self.run_supervisor(), 0)
        finally:
            os.environ.pop("SLACK_BOT_TOKEN", None)

        calls = self.read_log(self.bridge_log)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][:5], ["--dispatch", "goal", "qoder", "Shrink the parser", "--operator"]
        )
        self.assertEqual(calls[0][5], "grok-supervisor")

        grok = json.loads(self.grok_log.read_text(encoding="utf-8"))
        self.assertIn("worker: qoder", grok["prompt"])
        self.assertIn("tmux target: qoder", grok["prompt"])
        self.assertEqual(grok["cwd"], str(self.review_cwd))
        # The reviewer never sees the box's Slack credentials.
        self.assertNotIn("SLACK_BOT_TOKEN", grok["env"])
        self.assertNotIn("SPECTRE_GOAL_SUPERVISOR", grok["env"])
        self.assertEqual(grok["env"]["GROK_HOME"], str(self.dir / "grok-home"))

        posts = self.read_log(self.notify_log)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][posts[0].index("--agent") + 1], "grok")
        self.assertEqual(posts[0][posts[0].index("--channel") + 1], "lobby")

        state = self.read_state()
        self.assertIn("park:goal_budget:t-42", state["reviewed"])
        self.assertEqual(state["day"]["dispatches"], 1)
        self.assertAlmostEqual(state["day"]["cost_usd"], 0.02, places=6)
        self.assertEqual(
            state["workers"]["qoder"]["goals"], [supervisor.goal_fingerprint("Shrink the parser")]
        )

        # Second tick: the stop is settled, so nothing is bought again.
        self.assertEqual(self.run_supervisor(), 0)
        self.assertEqual(len(self.read_log(self.bridge_log)), 1)
        self.assertEqual(self.read_state()["day"]["dispatches"], 1)

    def test_quota_failure_alerts_once_and_keeps_the_event_for_retry(self) -> None:
        write_script(
            self.grok,
            GROK_BODY.replace("__LOG__", str(self.grok_log)).replace("__MODE__", "quota"),
        )
        self.assertEqual(self.run_supervisor(), 0)
        self.assertEqual(self.read_log(self.bridge_log), [])
        self.assertNotIn("park:goal_budget:t-42", self.read_state()["reviewed"])
        self.assertEqual(len(self.post_texts("fleet")), 1)
        self.assertIn("quota_exhausted", self.post_texts("fleet")[0])

        # A second tick alerts nothing (cooldown) but still retries the review.
        self.assertEqual(self.run_supervisor(), 0)
        self.assertEqual(len(self.post_texts("fleet")), 1)
        self.assertNotIn("park:goal_budget:t-42", self.read_state()["reviewed"])

    def test_invalid_decision_is_discarded_and_settled(self) -> None:
        write_script(
            self.grok,
            GROK_BODY.replace("__LOG__", str(self.grok_log)).replace("__MODE__", "invalid"),
        )
        self.assertEqual(self.run_supervisor(), 0)
        self.assertEqual(self.read_log(self.bridge_log), [])
        state = self.read_state()
        self.assertIn("park:goal_budget:t-42", state["reviewed"])
        self.assertEqual(state["day"]["dispatches"], 0)
        self.assertTrue(any("discarded its own decision" in text for text in self.post_texts()))
    def test_stop_decision_reports_without_dispatching(self) -> None:
        write_script(
            self.grok,
            GROK_BODY.replace("__LOG__", str(self.grok_log)).replace("__MODE__", "stop"),
        )
        self.assertEqual(self.run_supervisor(), 0)
        self.assertEqual(self.read_log(self.bridge_log), [])
        self.assertTrue(any("looks finished" in text for text in self.post_texts()))
        self.assertIn("park:goal_budget:t-42", self.read_state()["reviewed"])

    def test_plan_gate_escalates_without_a_model_call(self) -> None:
        self.seed_park("plan_gate", "t-77")
        self.assertEqual(self.run_supervisor(), 0)
        self.assertFalse(self.grok_log.is_file())
        self.assertEqual(self.read_log(self.bridge_log), [])
        self.assertTrue(any("plan approval" in text for text in self.post_texts()))
        state = self.read_state()
        self.assertIn("gate:t-77", state["escalated"])
        self.assertNotIn("gate:t-77", state["reviewed"])

    def test_kill_switch_does_nothing(self) -> None:
        self.assertEqual(self.run_supervisor(enabled=False), 0)
        self.assertIn("disabled", self.last_stdout)
        self.assertFalse(self.state_file.is_file())
        self.assertFalse(self.grok_log.is_file())
        self.assertFalse(self.bridge_log.is_file())

    def test_probe_never_calls_the_model_or_dispatches(self) -> None:
        args = [
            "--probe",
            "qoder",
            "--workers-file",
            str(self.workers_file),
            "--state-file",
            str(self.state_file),
            "--watcher-bin",
            str(self.watcher),
            "--grok-bin",
            str(self.grok),
            "--bridge-bin",
            str(self.bridge),
            "--notify-bin",
            str(self.notify),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(supervisor.main(args), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["worker"], "qoder")
        self.assertEqual(payload["gate"]["action"], "review")
        self.assertFalse(self.grok_log.is_file())
        self.assertFalse(self.bridge_log.is_file())
        self.assertFalse(self.state_file.is_file())

    def test_dry_run_posts_nothing_and_writes_no_state(self) -> None:
        self.assertEqual(self.run_supervisor("--dry-run"), 0)
        plan = json.loads(self.last_stdout)
        self.assertEqual([action["worker"] for action in plan["actions"]], ["qoder"])
        self.assertFalse(self.grok_log.is_file())
        self.assertFalse(self.bridge_log.is_file())
        self.assertFalse(self.notify_log.is_file())
        self.assertFalse(self.state_file.is_file())


if __name__ == "__main__":
    unittest.main()