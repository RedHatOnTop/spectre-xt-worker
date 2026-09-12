#!/usr/bin/env python3
"""Unit tests for the healthcheck notification state machine and parsers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import healthcheck  # noqa: E402

HOUR = 3600


def state(bits: frozenset[str], since: int, last: int, count: int) -> "healthcheck.State":
    return healthcheck.State(bits=bits, since=since, last_notified=last, count=count)


class DecideTest(unittest.TestCase):
    def test_first_failure_notifies_and_starts_streak(self) -> None:
        now = 1_000_000
        action, nxt = healthcheck.decide(
            ["proxy_down"], healthcheck.EMPTY_STATE, now, renotify_min=30
        )
        self.assertEqual(action, "notify_new")
        self.assertEqual(nxt.bits, frozenset({"proxy_down"}))
        self.assertEqual(nxt.since, now)
        self.assertEqual(nxt.count, 1)

    def test_recovery_clears_state(self) -> None:
        prev = state(frozenset({"proxy_down"}), since=1000, last=1100, count=1)
        action, nxt = healthcheck.decide([], prev, now=2000, renotify_min=30)
        self.assertEqual(action, "notify_recover")
        self.assertEqual(nxt, healthcheck.EMPTY_STATE)

    def test_same_failure_within_window_logs_only(self) -> None:
        prev = state(frozenset({"zcode_missing"}), since=1000, last=1300, count=1)
        action, nxt = healthcheck.decide(
            ["zcode_missing"], prev, now=1300 + 10 * 60, renotify_min=30
        )
        self.assertEqual(action, "log_only")
        self.assertEqual(nxt, prev)

    def test_same_failure_past_window_renotifies_with_count(self) -> None:
        prev = state(frozenset({"zcode_missing"}), since=1000, last=1300, count=1)
        now = 1300 + 31 * 60
        action, nxt = healthcheck.decide(["zcode_missing"], prev, now=now, renotify_min=30)
        self.assertEqual(action, "renotify")
        self.assertEqual(nxt.count, 2)
        self.assertEqual(nxt.since, 1000)
        self.assertEqual(nxt.last_notified, now)

    def test_changed_composition_notifies_but_keeps_since(self) -> None:
        prev = state(frozenset({"proxy_down"}), since=1000, last=1050, count=1)
        now = 1400
        action, nxt = healthcheck.decide(
            ["proxy_down", "ac_offline"], prev, now=now, renotify_min=30
        )
        self.assertEqual(action, "notify_new")
        self.assertEqual(nxt.since, 1000)
        self.assertEqual(nxt.count, 2)

    def test_bridge_failure_flap_notifies_on_change(self) -> None:
        _, st = healthcheck.decide(
            ["bridge_down"], healthcheck.EMPTY_STATE, now=1000, renotify_min=30
        )
        self.assertEqual(st.bits, frozenset({"bridge_down"}))
        action, nxt = healthcheck.decide(
            ["bridge_auth_http=200"], st, now=1100, renotify_min=30
        )
        self.assertEqual(action, "notify_new")
        self.assertEqual(nxt.since, 1000)
        self.assertEqual(nxt.count, 2)

    def test_recovered_then_new_failure_is_fresh_streak(self) -> None:
        _, after_recover = healthcheck.decide(
            [], state(frozenset({"disk:/=95%"}), 0, 500, 3), 600, renotify_min=30
        )
        action, fresh = healthcheck.decide(["disk:/=95%"], after_recover, 700, 30)
        self.assertEqual(action, "notify_new")
        self.assertEqual(fresh.since, 700)


class MessageTest(unittest.TestCase):
    def test_renotify_includes_duration(self) -> None:
        st = state(frozenset({"proxy_down", "ac_offline"}), since=0, last=0, count=1)
        msg = healthcheck.message_for("renotify", st, now=2 * HOUR + 5 * 60)
        self.assertIn("still failing 2h05m", msg)
        self.assertIn("proxy_down", msg)
        self.assertIn("ac_offline", msg)

    def test_plain_join_for_new(self) -> None:
        st = state(frozenset({"temp=90C"}), since=1, last=1, count=1)
        self.assertEqual(healthcheck.message_for("notify_new", st, now=2), "temp=90C")


class MeminfoTest(unittest.TestCase):
    SAMPLE = (
        "MemTotal:       12194304 kB\n"
        "MemFree:         8234567 kB\n"
        "MemAvailable:    6543210 kB\n"
        "SwapTotal:       8388604 kB\n"
        "SwapFree:        8388604 kB\n"
        "HugePages_Total:       0\n"
    )

    def test_parse_meminfo_kb_values(self) -> None:
        info = healthcheck.parse_meminfo(self.SAMPLE)
        self.assertEqual(info["MemTotal"], 12_194_304)
        self.assertEqual(info["MemAvailable"], 6_543_210)
        self.assertNotIn("HugePages_Total", info)

    def test_memory_ok_when_above_floor(self) -> None:
        result = healthcheck.probe_memory(800, 90, meminfo_text=self.SAMPLE)
        self.assertIsNone(result)

    def test_memory_low_available_fails(self) -> None:
        low = self.SAMPLE.replace("MemAvailable:    6543210", "MemAvailable:     500000")
        result = healthcheck.probe_memory(800, 90, meminfo_text=low)
        self.assertIsNotNone(result)
        self.assertIn("mem_avail=", result)

    def test_swap_exhaustion_fails(self) -> None:
        result = healthcheck.probe_memory(800, 90, meminfo_text=self.SAMPLE)
        self.assertIsNone(result)  # sample swap fully free
        exhausted = self.SAMPLE.replace(
            "SwapFree:        8388604", "SwapFree:          10000"
        )
        result = healthcheck.probe_memory(800, 90, meminfo_text=exhausted)
        self.assertIsNotNone(result)
        self.assertIn("swap=", result)


class TempExtractTest(unittest.TestCase):
    def test_extracts_nested_temp_inputs(self) -> None:
        node = {
            "coretemp": {"isa": {"0": {"Package id 0": {"temp1_input": 45.5}, "Core 0": {"temp2_input": 47.0}}}},
            "acpitz": {"temp1": {"temp1_input": 44.25}},
        }
        temps = healthcheck._extract_temps(node)
        self.assertEqual(sorted(temps), [44.25, 45.5, 47.0])


class OrcaProbeTest(unittest.TestCase):
    def test_failure_mapping(self) -> None:
        self.assertEqual(healthcheck._orca_reason(""), "orca_down")
        self.assertEqual(healthcheck._orca_reason("502"), "orca_http=502")
        self.assertIsNone(healthcheck._orca_reason("200"))

    def test_collect_failures_gates_orca_on_require_flag(self) -> None:
        off = healthcheck.config_from_env(
            {"REQUIRE_PROXY": "0", "REQUIRE_ZCODE": "0", "REQUIRE_ORCA": "0"}
        )
        on = healthcheck.config_from_env(
            {"REQUIRE_PROXY": "0", "REQUIRE_ZCODE": "0", "REQUIRE_ORCA": "1"}
        )
        with mock.patch.object(healthcheck, "probe_orca", return_value="orca_down"), \
                mock.patch.object(healthcheck, "probe_temp", return_value=None), \
                mock.patch.object(healthcheck, "probe_memory", return_value=None), \
                mock.patch.object(healthcheck, "probe_tmux", return_value=None), \
                mock.patch.object(healthcheck, "probe_ac", return_value=None), \
                mock.patch.object(healthcheck, "probe_tailscale", return_value=None), \
                mock.patch.object(healthcheck, "probe_disks", return_value=[]):
            self.assertEqual(healthcheck.collect_failures(off), [])
            self.assertEqual(healthcheck.collect_failures(on), ["orca_down"])


class SlackBackendTest(unittest.TestCase):
    def test_args_shape(self) -> None:
        args = healthcheck.slack_notify_args("proxy_down")
        self.assertEqual(args[0], "spectre-slack-notify")
        self.assertEqual(args[args.index("--agent") + 1], "healthcheck")
        self.assertEqual(args[args.index("--channel") + 1], "alerts")
        self.assertIn("--text=proxy_down", args)
        self.assertNotIn("--recovery", args)

    def test_recovery_flag_included(self) -> None:
        args = healthcheck.slack_notify_args("recovered", recovery=True)
        self.assertIn("--recovery", args)
        self.assertIn("--text=recovered", args)

    def test_send_slack_logs_failure_on_empty_output(self) -> None:
        cfg = healthcheck.config_from_env({})
        with mock.patch.object(healthcheck, "run", return_value=""), \
                mock.patch.object(healthcheck, "append_log") as log:
            healthcheck.send_slack(cfg, "proxy_down")
        log.assert_called_once_with("SLACK_NOTIFY_FAILED")

    def test_send_slack_silent_when_notifier_answers(self) -> None:
        cfg = healthcheck.config_from_env({})
        with mock.patch.object(healthcheck, "run", return_value="slack-notify: disabled"), \
                mock.patch.object(healthcheck, "append_log") as log:
            healthcheck.send_slack(cfg, "proxy_down")
        log.assert_not_called()


class ConfigTest(unittest.TestCase):
    def test_env_overrides_and_defaults(self) -> None:
        cfg = healthcheck.config_from_env({})
        self.assertEqual(cfg.proxy_url, healthcheck.DEFAULT_PROXY_URL)
        self.assertEqual(cfg.renotify_min, 30)
        self.assertTrue(cfg.require_proxy)
        self.assertTrue(cfg.require_zcode)
        self.assertFalse(cfg.require_claude)
        self.assertFalse(cfg.require_bridge)
        self.assertTrue(cfg.require_orca)
        self.assertEqual(cfg.orca_url, healthcheck.DEFAULT_ORCA_URL)
        self.assertFalse(cfg.require_slack)
        self.assertEqual(cfg.bridge_health_url, healthcheck.DEFAULT_BRIDGE_URL)
        cfg = healthcheck.config_from_env({"RENOTIFY_MIN": "15", "NTFY_TOPIC": "t"})
        self.assertEqual(cfg.renotify_min, 15)
        self.assertEqual(cfg.ntfy_topic, "t")

    def test_load_env_file_skips_comments(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "health.env"
            p.write_text("# hi\nREQUIRE_PROXY=0\n\nNTFY_TOPIC=box\n", encoding="utf-8")
            got = healthcheck.load_env_file(p)
        self.assertEqual(got["REQUIRE_PROXY"], "0")
        self.assertEqual(got["NTFY_TOPIC"], "box")
        self.assertNotIn("# hi", got)

    def test_require_flags_accept_off_values(self) -> None:
        cfg = healthcheck.config_from_env(
            {
                "REQUIRE_PROXY": "0",
                "REQUIRE_ZCODE": "false",
                "REQUIRE_CLAUDE": "yes",
                "REQUIRE_BRIDGE": "1",
                "BRIDGE_HEALTH_URL": "http://127.0.0.1:9999/mcp",
            }
        )
        self.assertFalse(cfg.require_proxy)
        self.assertFalse(cfg.require_zcode)
        self.assertTrue(cfg.require_claude)
        self.assertTrue(cfg.require_bridge)
        self.assertEqual(cfg.bridge_health_url, "http://127.0.0.1:9999/mcp")


if __name__ == "__main__":
    unittest.main()
