#!/usr/bin/env python3
"""Efficient 0x kill-switch: parse catalog windows and decide billed vs free."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qoder-efficient-guard.py"
spec = importlib.util.spec_from_file_location("qoder_efficient_guard", MODULE_PATH)
guard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)

LIVE_FREE = (
    '{"key":"efficient","format":"openai","source":"system","enable":true,'
    '"display_name":"Efficient","is_vl":true,"is_reasoning":false,'
    '"is_default":false,"price_factor":0.0,"original_price_factor":0.3,'
    '"max_input_tokens":200000,"strategies":[{"tag":"C4","priority":999,'
    '"enabled":false,"disabled_message_key":"codeSafeModelReason"}],'
    '"is_free":true}'
)

LIVE_BILLED = LIVE_FREE.replace('"price_factor":0.0', '"price_factor":0.3').replace(
    '"is_free":true', '"is_free":false'
)

NEIGHBORS = (
    '{"key":"performance","display_name":"Performance","price_factor":1.1},'
    + LIVE_FREE
    + ',{"key":"lite","display_name":"Lite","price_factor":0.0,"is_free":true}'
)


class ParseTests(unittest.TestCase):
    def test_free_zero_point_zero(self):
        rates = guard.parse_efficient_rates(LIVE_FREE.encode())
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0].price_factor, 0.0)
        self.assertEqual(rates[0].original_price_factor, 0.3)
        self.assertTrue(rates[0].is_free)
        self.assertFalse(guard.is_billed(rates[0]))

    def test_integer_zero_is_free(self):
        blob = LIVE_FREE.replace('"price_factor":0.0', '"price_factor":0').encode()
        rate = guard.parse_efficient_rates(blob)[0]
        self.assertEqual(rate.price_factor, 0.0)
        self.assertFalse(guard.is_billed(rate))

    def test_positive_factor_is_billed_even_if_is_free(self):
        blob = LIVE_FREE.replace('"price_factor":0.0', '"price_factor":0.3').encode()
        rate = guard.parse_efficient_rates(blob)[0]
        self.assertEqual(rate.price_factor, 0.3)
        self.assertTrue(guard.is_billed(rate))

    def test_promo_end_object(self):
        rate = guard.parse_efficient_rates(LIVE_BILLED.encode())[0]
        self.assertTrue(guard.is_billed(rate))
        self.assertFalse(rate.is_free)

    def test_does_not_take_lite_or_performance(self):
        rates = guard.parse_efficient_rates(NEIGHBORS.encode())
        self.assertEqual([r.key for r in rates], ["efficient"])
        self.assertEqual(rates[0].price_factor, 0.0)

    def test_nested_strategies_braces(self):
        rates = guard.parse_efficient_rates(LIVE_FREE.encode())
        self.assertEqual(rates[0].display_name, "Efficient")

    def test_utf16le_catalog(self):
        blob = LIVE_BILLED.encode("utf-16le")
        rates = guard.parse_efficient_rates(blob)
        self.assertTrue(rates)
        self.assertTrue(guard.is_billed(rates[0]))

    def test_mixed_zero_and_positive_does_not_stop(self):
        blob = (LIVE_FREE + LIVE_BILLED).encode()
        decided = guard.decide_from_rates(guard.parse_efficient_rates(blob))
        self.assertEqual(decided.status, "mixed")
        self.assertEqual(decided.price_factor, 0.3)
        self.assertFalse(guard.should_stop(decided))

    def test_bool_price_factor_is_ignored(self):
        blob = b'{"key":"efficient","price_factor":true,"display_name":"Efficient"}'
        self.assertEqual(guard.parse_efficient_rates(blob), [])

    def test_missing_efficient_is_unknown(self):
        blob = b'{"key":"lite","price_factor":0.0,"display_name":"Lite"}'
        decided = guard.decide_from_rates(guard.parse_efficient_rates(blob))
        self.assertEqual(decided.status, "unknown")
        self.assertIsNone(decided.price_factor)

    def test_spaces_in_json(self):
        blob = b'{ "key" : "efficient" , "price_factor" : 0.0 , "is_free" : true }'
        rate = guard.parse_efficient_rates(blob)[0]
        self.assertFalse(guard.is_billed(rate))


class MapFilterTests(unittest.TestCase):
    def test_scans_anonymous_rw_heap(self):
        row = "7f12a000-7f22a000 rw-p 00000000 00:00 0  [heap]"
        self.assertTrue(guard._should_scan_map(row))

    def test_skips_file_backed_and_executable(self):
        file_row = "00400000-0c000000 r-xp 00000000 08:01 123  /home/person/.qoder/bin/qodercli/qodercli-1.1.47"
        self.assertFalse(guard._should_scan_map(file_row))
        stack = "7ffd0000-7ffd2000 rw-p 00000000 00:00 0  [stack]"
        self.assertFalse(guard._should_scan_map(stack))
        ro = "7f12a000-7f22a000 r--p 00000000 00:00 0"
        self.assertFalse(guard._should_scan_map(ro))


class DecisionTests(unittest.TestCase):
    def test_allow_start_free(self):
        d = guard.Decision(status="free", price_factor=0.0)
        self.assertTrue(guard.allow_start(d, sentinel_present=False))

    def test_allow_start_blocks_sentinel(self):
        d = guard.Decision(status="free", price_factor=0.0)
        self.assertFalse(guard.allow_start(d, sentinel_present=True))

    def test_allow_start_blocks_billed_and_unknown(self):
        self.assertFalse(
            guard.allow_start(guard.Decision(status="billed", price_factor=0.3), False)
        )
        self.assertFalse(
            guard.allow_start(guard.Decision(status="unknown", price_factor=None), False)
        )
        self.assertFalse(
            guard.allow_start(guard.Decision(status="mixed", price_factor=0.3), False)
        )

    def test_should_stop_only_when_billed(self):
        self.assertTrue(guard.should_stop(guard.Decision(status="billed", price_factor=0.3)))
        self.assertFalse(guard.should_stop(guard.Decision(status="free", price_factor=0.0)))
        self.assertFalse(guard.should_stop(guard.Decision(status="unknown", price_factor=None)))


class HookTests(unittest.TestCase):
    def test_pretool_denies_when_sentinel_exists(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        code, stderr, stdout = guard.hook_response(event, sentinel_present=True)
        self.assertEqual(code, 2)
        self.assertIn("price_factor", stderr)
        self.assertEqual(stdout, "")

    def test_prompt_denies_when_sentinel_exists(self):
        event = {"hook_event_name": "UserPromptSubmit", "prompt": "continue"}
        code, stderr, _ = guard.hook_response(event, sentinel_present=True)
        self.assertEqual(code, 2)
        self.assertIn("billing", stderr.lower())

    def test_session_start_stops_when_sentinel_exists(self):
        event = {"hook_event_name": "SessionStart", "source": "startup", "model": "Efficient"}
        code, _, stdout = guard.hook_response(event, sentinel_present=True)
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["continue"])

    def test_hook_allows_when_no_sentinel(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        code, stderr, stdout = guard.hook_response(event, sentinel_present=False)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "")


class StopTests(unittest.TestCase):
    def test_stop_all_writes_sentinel_pauses_kills_and_stops_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            calls: list[list[str]] = []

            def run(args: list[str], **kwargs):
                calls.append(args)
                if args[:2] == ["/usr/bin/orca-ide", "terminal"]:
                    listing = {
                        "result": {
                            "terminals": [
                                {
                                    "handle": "term_mc",
                                    "title": "qoder-efficient mc",
                                    "worktreePath": "/x",
                                },
                                {
                                    "handle": "term_other",
                                    "title": "htop",
                                    "worktreePath": "/y",
                                },
                            ]
                        }
                    }
                    return mock.Mock(returncode=0, stdout=json.dumps(listing), stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            notifies: list[str] = []
            decision = guard.Decision(
                status="billed",
                price_factor=0.3,
                original_price_factor=0.3,
                source="test",
            )
            killed: list[bool] = []

            def kill() -> list[int]:
                killed.append(True)
                return []

            guard.stop_all(
                decision,
                state_dir=state,
                run=run,
                notify=notifies.append,
                orca="/usr/bin/orca-ide",
                sleep=lambda _s: None,
                kill_qodercli=kill,
            )
            sentinel = state / "qoder-efficient-billed"
            self.assertTrue(sentinel.exists())
            body = json.loads(sentinel.read_text())
            self.assertEqual(body["price_factor"], 0.3)
            joined = [" ".join(c) for c in calls]
            self.assertTrue(any("/goal pause" in j for j in joined))
            self.assertTrue(any("term_mc" in j for j in joined))
            self.assertFalse(any("term_other" in j and "/goal pause" in j for j in joined))
            self.assertTrue(
                any("systemctl" in j and "qoder-nudge.timer" in j for j in joined)
            )
            self.assertTrue(killed)
            self.assertFalse(any("pkill" in j for j in joined))
            self.assertTrue(notifies)
            self.assertIn("0.3", notifies[0])


if __name__ == "__main__":
    sys.exit(unittest.main())
