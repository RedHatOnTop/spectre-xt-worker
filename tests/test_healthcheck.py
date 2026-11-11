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

    def test_measurement_drift_is_not_a_new_failure(self) -> None:
        now = 1_000_000
        action, st = healthcheck.decide(
            ["disk:/=96%"], healthcheck.EMPTY_STATE, now, renotify_min=30
        )
        self.assertEqual(action, "notify_new")
        for minute, pct in enumerate([97, 98, 99, 100, 99, 100, 96], start=1):
            action, st = healthcheck.decide(
                [f"disk:/={pct}%"], st, now + minute * 60, renotify_min=30
            )
            self.assertEqual(action, "log_only", f"{pct}% at +{minute}m")
        self.assertEqual(st.bits, frozenset({"disk:/=96%"}))
        self.assertEqual(st.count, 1)

    def test_log_only_carries_the_latest_measurement(self) -> None:
        prev = state(frozenset({"disk:/=96%"}), since=1000, last=1300, count=1)
        _, nxt = healthcheck.decide(["disk:/=99%"], prev, now=1400, renotify_min=30)
        self.assertEqual(nxt.bits, frozenset({"disk:/=99%"}))
        self.assertEqual((nxt.since, nxt.last_notified, nxt.count), (1000, 1300, 1))

    def test_another_probe_joining_still_notifies(self) -> None:
        prev = state(frozenset({"disk:/=96%"}), since=1000, last=1300, count=1)
        action, _ = healthcheck.decide(
            ["disk:/=96%", "load1=9.10>=8"], prev, now=1400, renotify_min=30
        )
        self.assertEqual(action, "notify_new")

    def test_renotify_backs_off_to_the_cap(self) -> None:
        minutes = [
            healthcheck.renotify_interval_sec(30, count) // 60 for count in range(1, 8)
        ]
        self.assertEqual(minutes, [30, 60, 120, 240, 240, 240, 240])

    def test_backoff_never_undercuts_a_longer_configured_interval(self) -> None:
        self.assertEqual(healthcheck.renotify_interval_sec(360, 1) // 60, 360)
        self.assertEqual(healthcheck.renotify_interval_sec(360, 9) // 60, 360)

    def test_second_renotify_waits_out_the_doubled_window(self) -> None:
        prev = state(frozenset({"proxy_down"}), since=0, last=1000, count=2)
        action, _ = healthcheck.decide(
            ["proxy_down"], prev, now=1000 + 31 * 60, renotify_min=30
        )
        self.assertEqual(action, "log_only")
        action, nxt = healthcheck.decide(
            ["proxy_down"], prev, now=1000 + 61 * 60, renotify_min=30
        )
        self.assertEqual((action, nxt.count), ("renotify", 3))


class FailureKeyTest(unittest.TestCase):
    def test_measured_numbers_are_dropped(self) -> None:
        self.assertEqual(healthcheck.failure_key("disk:/=96%"), "disk:/=#%")
        self.assertEqual(healthcheck.failure_key("load1=6.12>=8"), "load1=#>=#")
        self.assertEqual(healthcheck.failure_key("swap_used=4.2G"), "swap_used=#G")

    def test_digits_in_names_and_words_survive(self) -> None:
        self.assertEqual(healthcheck.failure_key("disk:/mnt/data2=96%"), "disk:/mnt/data2=#%")
        self.assertNotEqual(
            healthcheck.failure_key("disk:/mnt/data1=96%"),
            healthcheck.failure_key("disk:/mnt/data2=96%"),
        )
        self.assertEqual(healthcheck.failure_key("proxy_status=empty"), "proxy_status=empty")
        self.assertNotEqual(
            healthcheck.failure_key("tailscale=down"), healthcheck.failure_key("tailscale=unreadable")
        )


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

    def test_swap_used_gib_alerts_without_percent_trip(self) -> None:
        # 7 GiB used of ~8 GiB total is under 90% but over 4 GiB.
        used = self.SAMPLE.replace(
            "SwapFree:        8388604", "SwapFree:         1048576"
        )
        result = healthcheck.probe_memory(
            800, 90, meminfo_text=used, swap_used_gib=4.0
        )
        self.assertIsNotNone(result)
        self.assertIn("swap_used=", result)
        self.assertNotIn("swap=", result)


class LoadProbeTest(unittest.TestCase):
    def test_threshold_is_max_8_or_twice_nproc(self) -> None:
        self.assertEqual(healthcheck.load_fail_threshold(1), 8.0)
        self.assertEqual(healthcheck.load_fail_threshold(4), 8.0)
        self.assertEqual(healthcheck.load_fail_threshold(8), 16.0)

    def test_parse_loadavg_first_field(self) -> None:
        self.assertEqual(
            healthcheck.parse_loadavg("10.57 12.00 13.29 21/1371 4052711"),
            10.57,
        )
        self.assertIsNone(healthcheck.parse_loadavg(""))
        self.assertIsNone(healthcheck.parse_loadavg("not-a-number"))

    def test_probe_load_trips_at_threshold(self) -> None:
        self.assertIsNone(
            healthcheck.probe_load("7.99 1.00 1.00 1/1 1", nproc=4)
        )
        hit = healthcheck.probe_load("8.00 1.00 1.00 1/1 1", nproc=4)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.startswith("load1="))

    def test_collect_failures_includes_load_and_does_not_need_proxy(self) -> None:
        cfg = healthcheck.config_from_env(
            {"REQUIRE_PROXY": "0", "REQUIRE_ZCODE": "0", "REQUIRE_ORCA": "0"}
        )
        with mock.patch.object(healthcheck, "probe_temp", return_value=None), \
                mock.patch.object(healthcheck, "probe_memory", return_value=None), \
                mock.patch.object(healthcheck, "probe_tmux", return_value=None), \
                mock.patch.object(healthcheck, "probe_ac", return_value=None), \
                mock.patch.object(healthcheck, "probe_tailscale", return_value=None), \
                mock.patch.object(healthcheck, "probe_disks", return_value=[]), \
                mock.patch.object(healthcheck, "probe_load", return_value="load1=9.00>=8"):
            self.assertIn("load1=9.00>=8", healthcheck.collect_failures(cfg))


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
                mock.patch.object(healthcheck, "probe_load", return_value=None), \
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
