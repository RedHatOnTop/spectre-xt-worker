#!/usr/bin/env python3
"""Pure resolver + policy tests for spectre-worker-state."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worker_state.policy import fail_closed_snapshot, policy_for
from worker_state.resolver import resolve
from worker_state.types import AUTH_TUI, Event, iso_from

NOW = 1_800_000_000.0
WORKER = "pugc"


def ev(kind: str, seq: int, **kw) -> Event:
    ts = kw.pop("ts", NOW)
    source = kw.pop("source", "session_jsonl")
    authority = kw.pop("authority", None)
    if authority is None:
        from worker_state.types import authority_for

        authority = authority_for(source, kind)
    return Event(
        journal_seq=seq,
        event_id=kw.pop("event_id", f"e{seq}"),
        worker_id=kw.pop("worker_id", WORKER),
        kind=kind,
        source=source,
        authority=authority,
        source_timestamp=iso_from(ts) if isinstance(ts, (int, float)) else ts,
        goal_id=kw.pop("goal_id", None),
        turn_id=kw.pop("turn_id", None),
        dispatch_id=kw.pop("dispatch_id", None),
        attempt_id=kw.pop("attempt_id", None),
        payload=kw.pop("payload", {}),
    )


def snap(events, now=NOW):
    return resolve(events, now, WORKER)


class EmptyAndPolicy(unittest.TestCase):
    def test_unseen_worker_is_idle_and_dispatchable(self):
        out = snap([])
        self.assertEqual(out["goal"]["state"], "IDLE")
        self.assertTrue(out["policy"]["can_dispatch_goal"])
        self.assertFalse(out["policy"]["can_resume"])
        self.assertEqual(out["reason"], "no evidence; worker unseen")

    def test_fail_closed_unknown_blocks_all_mutation(self):
        closed = fail_closed_snapshot("pugc", "connection refused", iso_from(NOW))
        self.assertEqual(closed["goal"]["state"], "UNKNOWN")
        self.assertFalse(closed["policy"]["can_dispatch_goal"])
        self.assertFalse(closed["policy"]["can_resume"])
        self.assertFalse(closed["policy"]["continuity_recovery_allowed"])
        self.assertFalse(closed["policy"]["grokbot_may_advance"])

    def test_plan_gate_refuses_resume(self):
        policy = policy_for(
            goal_state="PARKED",
            park_reason="plan_gate",
            stalled=False,
            waiting=False,
            completion_open=False,
        )
        self.assertFalse(policy["can_resume"])
        self.assertFalse(policy["can_dispatch_goal"])
        self.assertFalse(policy["grokbot_may_advance"])

    def test_goal_budget_allows_resume(self):
        policy = policy_for(
            goal_state="PARKED",
            park_reason="goal_budget",
            stalled=False,
            waiting=False,
            completion_open=False,
        )
        self.assertTrue(policy["can_resume"])
        self.assertFalse(policy["can_dispatch_goal"])
        self.assertTrue(policy["grokbot_may_advance"])


class Lifecycle(unittest.TestCase):
    def test_model_started_is_running_not_age_idle(self):
        events = [ev("model.request.started", 1, ts=NOW - 601, turn_id="t1")]
        out = snap(events, NOW)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertFalse(out["policy"]["can_dispatch_goal"])
        self.assertGreaterEqual(out["execution"]["progress_seq"], 1)

    def test_same_input_same_snapshot(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("tool.started", 2, turn_id="t1", payload={"tool": "shell"}),
        ]
        a = snap(events)
        b = snap(events)
        a.pop("debug")
        b.pop("debug")
        self.assertEqual(a, b)

    def test_stale_turn_ended_does_not_idle_later_work(self):
        events = [
            ev("model.request.started", 1, turn_id="t1", ts=NOW - 10),
            ev("turn.ended", 2, turn_id="t0", ts=NOW - 4000, payload={"reason": "session.phase.finished"}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")

    def test_turn_ended_is_idle(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("turn.ended", 2, turn_id="t1", payload={"reason": "end_turn"}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "IDLE")
        self.assertTrue(out["policy"]["can_dispatch_goal"])

    def test_max_turns_is_parked_goal_budget(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev(
                "goal.parked",
                2,
                turn_id="t1",
                payload={"park_reason": "goal_budget", "num_turns": 1000},
            ),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "PARKED")
        self.assertEqual(out["goal"]["park_reason"], "goal_budget")
        self.assertTrue(out["policy"]["can_resume"])
        self.assertFalse(out["policy"]["can_dispatch_goal"])

    def test_exit_plan_mode_is_plan_gate(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("goal.parked", 2, turn_id="t1", payload={"park_reason": "plan_gate"}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["park_reason"], "plan_gate")
        self.assertFalse(out["policy"]["can_resume"])

    def test_permission_resolved_clears_plan_gate(self):
        events = [
            ev("goal.parked", 1, turn_id="t1", payload={"park_reason": "plan_gate"}),
            ev("permission.resolved", 2, turn_id="t1", payload={"allowed": True}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertIsNone(out["goal"]["park_reason"])

    def test_completed_then_late_tool_started_stays_completed(self):
        events = [
            ev("model.request.started", 1, turn_id="t1", attempt_id=1),
            ev("goal.completed", 2, turn_id="t1", attempt_id=1, goal_id="g-1"),
            ev("tool.started", 3, turn_id="t1", attempt_id=1, ts=NOW - 5),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "COMPLETED")
        self.assertTrue(out["policy"]["can_dispatch_goal"])

    def test_stale_attempt_cannot_rollback(self):
        events = [
            ev("terminal_write.succeeded", 1, goal_id="g-1", dispatch_id="d-1", attempt_id=1, payload={"action": "dispatch_goal"}),
            ev("model.request.started", 2, goal_id="g-1", turn_id="t1", attempt_id=1),
            ev("goal.completed", 3, goal_id="g-1", turn_id="t1", attempt_id=1),
            ev("terminal_write.succeeded", 4, goal_id="g-1", dispatch_id="d-2", attempt_id=3, payload={"action": "resume"}),
            ev("model.request.started", 5, goal_id="g-1", turn_id="t3", attempt_id=3),
            ev("tool.started", 6, goal_id="g-1", turn_id="t1", attempt_id=1, payload={"tool": "shell"}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertEqual(out["goal"]["attempt_id"], 3)

    def test_tui_cannot_flip_lifecycle(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("goal.completed", 2, source="tui", authority=AUTH_TUI, turn_id="t1"),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")

    def test_heartbeat_does_not_increment_progress(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("heartbeat", 2, source="harness"),
            ev("heartbeat", 3, source="harness"),
        ]
        out = snap(events)
        self.assertEqual(out["execution"]["progress_seq"], 1)
        self.assertEqual(out["goal"]["state"], "RUNNING")

    def test_waiting_blocks_recovery(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("delegated_wait.begin", 2, turn_id="t1"),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "WAITING")
        self.assertFalse(out["policy"]["continuity_recovery_allowed"])
        self.assertTrue(out["policy"]["continuity_eligible"])

    def test_in_flight_tool_is_not_stalled(self):
        events = [ev("tool.started", 1, turn_id="t1", ts=NOW - 4000, payload={"tool": "shell"})]
        out = snap(events, NOW)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertFalse(out["execution"]["stalled"])

    def test_running_without_progress_eventually_stalls(self):
        events = [ev("model.request.started", 1, turn_id="t1", ts=NOW - 2000)]
        out = snap(events, NOW)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertTrue(out["execution"]["stalled"])
        self.assertFalse(out["policy"]["can_dispatch_goal"])
        self.assertTrue(out["policy"]["continuity_recovery_allowed"])

    def test_injected_becomes_unconfirmed_after_timeout(self):
        events = [
            ev(
                "terminal_write.succeeded",
                1,
                ts=NOW - 121,
                goal_id="g-1",
                dispatch_id="d-1",
                attempt_id=1,
                payload={"action": "dispatch_goal", "goal_id": "g-1", "dispatch_id": "d-1", "attempt_id": 1},
            )
        ]
        out = snap(events, NOW)
        self.assertEqual(out["goal"]["state"], "UNCONFIRMED")
        self.assertFalse(out["policy"]["can_dispatch_goal"])

    def test_injected_then_turn_id_accepts_and_runs(self):
        events = [
            ev(
                "terminal_write.succeeded",
                1,
                goal_id="g-1",
                dispatch_id="d-1",
                attempt_id=1,
                payload={"action": "dispatch_goal"},
            ),
            ev("model.request.started", 2, turn_id="t-new", goal_id="g-1", attempt_id=1),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertEqual(out["goal"]["turn_id"], "t-new")

    def test_transport_down_is_not_goal_failure(self):
        events = [
            ev("model.request.started", 1, turn_id="t1"),
            ev("transport.down", 2, source="tmux", payload={"kind": "tmux"}),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertEqual(out["transport"]["state"], "DOWN")

    def test_process_cpu_prevents_stall(self):
        events = [
            ev("model.request.started", 1, turn_id="t1", ts=NOW - 2000),
            ev(
                "process.sample",
                2,
                source="process",
                ts=NOW - 5,
                payload={"alive": True, "nprocs": 4, "cpu_delta": 50},
            ),
        ]
        out = snap(events, NOW)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertFalse(out["execution"]["stalled"])

    def test_new_turn_after_park_starts_epoch(self):
        events = [
            ev("goal.parked", 1, turn_id="t1", payload={"park_reason": "goal_budget"}),
            ev("model.request.started", 2, turn_id="t2"),
        ]
        out = snap(events)
        self.assertEqual(out["goal"]["state"], "RUNNING")
        self.assertEqual(out["goal"]["turn_id"], "t2")


class Property(unittest.TestCase):
    def test_random_sequences_are_deterministic(self):
        import random

        kinds = [
            ("model.request.started", "session_jsonl", {}),
            ("tool.started", "session_jsonl", {"tool": "shell"}),
            ("tool.completed", "session_jsonl", {"tool": "shell"}),
            ("heartbeat", "harness", {}),
            ("turn.ended", "session_jsonl", {"reason": "end_turn"}),
            ("goal.parked", "session_jsonl", {"park_reason": "goal_budget"}),
            ("goal.completed", "session_jsonl", {}),
            ("transport.down", "tmux", {"kind": "tmux"}),
            ("hook.finished", "tui", {}),
        ]
        rng = random.Random(7)
        for trial in range(25):
            events = []
            for seq in range(rng.randint(0, 10)):
                kind, source, payload = kinds[rng.randrange(len(kinds))]
                events.append(
                    ev(
                        kind,
                        seq + 1,
                        source=source if kind != "hook.finished" else "tui",
                        turn_id=f"t{rng.randint(1, 3)}",
                        attempt_id=rng.choice([None, 1, 2]),
                        payload=dict(payload),
                        ts=NOW - rng.randint(0, 4000),
                    )
                )
            first = snap(events)
            second = snap(events)
            first.pop("debug")
            second.pop("debug")
            self.assertEqual(first, second, msg=f"trial {trial}")


if __name__ == "__main__":
    unittest.main()
