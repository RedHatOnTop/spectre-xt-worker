import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "qoder-efficient-guard"
_loader = importlib.machinery.SourceFileLoader("qoder_efficient_guard", str(GUARD))
_spec = importlib.util.spec_from_loader("qoder_efficient_guard", _loader)
guard = importlib.util.module_from_spec(_spec)
sys.modules["qoder_efficient_guard"] = guard
_loader.exec_module(guard)
guard.WORK_LOG = Path(tempfile.mkdtemp()) / guard.LOG_NAME


def result_run(**fields):
    line = json.dumps({"type": "result", "is_error": False, **fields})

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=f"noise\n{line}\n", stderr="")

    return run


class ModelArgTest(unittest.TestCase):
    def test_short_long_and_equals_forms(self):
        self.assertEqual(guard.model_arg(["qodercli", "-p", "-m", "Qwen3.8-Flash", "x"]), "Qwen3.8-Flash")
        self.assertEqual(guard.model_arg(["qodercli", "--model", "DeepSeek-Flash"]), "DeepSeek-Flash")
        self.assertEqual(guard.model_arg(["qodercli", "--model=Efficient"]), "Efficient")

    def test_no_model(self):
        self.assertIsNone(guard.model_arg(["qodercli", "-p", "hello"]))
        self.assertIsNone(guard.model_arg(["qodercli", "-m"]))


class VerifiedFreeTest(unittest.TestCase):
    def test_fresh_zero_credit_probe_is_free(self):
        free = {"Qwen3.8-Flash": {"free": True, "checked_at": 1000.0}}
        self.assertTrue(guard.is_verified_free("Qwen3.8-Flash", free, 1000.0 + 60))

    def test_stale_probe_is_not_free(self):
        free = {"Qwen3.8-Flash": {"free": True, "checked_at": 1000.0}}
        self.assertFalse(guard.is_verified_free("Qwen3.8-Flash", free, 1000.0 + guard.FREE_TTL_S + 1))

    def test_billed_probe_unlisted_model_and_no_model_are_not_free(self):
        self.assertFalse(guard.is_verified_free("DeepSeek-Flash", {"DeepSeek-Flash": {"free": False, "checked_at": 1.0}}, 2.0))
        self.assertFalse(guard.is_verified_free("Efficient", {"Efficient": {"free": True, "checked_at": 1.0}}, 2.0))
        self.assertFalse(guard.is_verified_free(None, {}, 2.0))


class VerifyFreeTest(unittest.TestCase):
    def test_zero_credits_and_cost(self):
        self.assertTrue(guard.verify_free("Qwen3.8-Flash", run=result_run(total_credits=0, total_cost_usd=0)))

    def test_any_credit_is_billed(self):
        self.assertFalse(guard.verify_free("Qwen3.8-Flash", run=result_run(total_credits=0.3, total_cost_usd=0)))
        self.assertFalse(guard.verify_free("Qwen3.8-Flash", run=result_run(total_credits=0, total_cost_usd=0.01)))

    def test_error_or_missing_fields_are_unknown(self):
        self.assertIsNone(guard.verify_free("Qwen3.8-Flash", run=result_run(is_error=True, total_credits=0, total_cost_usd=0)))
        self.assertIsNone(guard.verify_free("Qwen3.8-Flash", run=result_run()))

        def blocked(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="Agent execution blocked by hook\n", stderr="")

        self.assertIsNone(guard.verify_free("Qwen3.8-Flash", run=blocked))

    def test_probe_runs_marked_and_headless(self):
        seen = {}

        def run(argv, **kwargs):
            seen.update(argv=argv, env=kwargs["env"])
            return result_run(total_credits=0, total_cost_usd=0)(argv)

        guard.verify_free("DeepSeek-Flash", qodercli="/q", run=run)
        self.assertEqual(seen["argv"][:4], ["/q", "-p", "-m", "DeepSeek-Flash"])
        self.assertEqual(seen["env"][guard.PROBE_ENV], "1")
        self.assertNotIn("DISPLAY", seen["env"])


class RefreshFreeTest(unittest.TestCase):
    def test_probes_stale_models_and_keeps_fresh_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            guard.free_path(state).write_text(json.dumps(
                {"Qwen3.8-Flash": {"free": True, "checked_at": 990.0, "probed_at": 990.0}}))
            probed = []
            free = guard.refresh_free(state, now=lambda: 1000.0, verify=lambda m: probed.append(m) or True)
            self.assertEqual(probed, ["DeepSeek-Flash"])
            self.assertEqual(free["DeepSeek-Flash"]["free"], True)
            self.assertEqual(json.loads(guard.free_path(state).read_text())["DeepSeek-Flash"]["checked_at"], 1000.0)

    def test_unknown_probe_keeps_the_last_verdict_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            guard.free_path(state).write_text(json.dumps(
                {m: {"free": True, "checked_at": 0.0, "probed_at": 0.0} for m in guard.FREE_MODELS}))
            free = guard.refresh_free(state, now=lambda: 5000.0, verify=lambda m: None)
            for model in guard.FREE_MODELS:
                self.assertEqual(free[model]["checked_at"], 0.0)
                self.assertEqual(free[model]["probed_at"], 5000.0)

    def test_billed_probe_records_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            free = guard.refresh_free(Path(tmp), now=lambda: 10.0, verify=lambda m: m != "DeepSeek-Flash")
            self.assertFalse(guard.is_verified_free("DeepSeek-Flash", free, 11.0))
            self.assertTrue(guard.is_verified_free("Qwen3.8-Flash", free, 11.0))


class HookTest(unittest.TestCase):
    def test_free_model_passes_with_sentinel(self):
        for name in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            self.assertEqual(guard.hook_response({"hook_event_name": name}, True, model_free=True), (0, "", ""))

    def test_other_models_stay_blocked(self):
        code, stderr, _ = guard.hook_response({"hook_event_name": "UserPromptSubmit"}, True)
        self.assertEqual(code, 2)
        self.assertIn("Efficient is billing", stderr)
        _, _, stdout = guard.hook_response({"hook_event_name": "SessionStart"}, True)
        self.assertFalse(json.loads(stdout)["continue"])

    def test_session_model_from_event(self):
        self.assertEqual(guard._session_model({"model": "DeepSeek-Flash"}, 0), "DeepSeek-Flash")


class KillKeepTest(unittest.TestCase):
    def test_kept_pids_are_not_signalled(self):
        killed = []
        original_pids, original_kill = guard._qodercli_pids, guard.os.kill
        guard._qodercli_pids = lambda: [11, 12]
        guard.os.kill = lambda pid, sig: killed.append(pid)
        try:
            left = guard._kill_qodercli(sleep=lambda s: None, keep=lambda pid: pid == 12)
        finally:
            guard._qodercli_pids, guard.os.kill = original_pids, original_kill
        self.assertEqual(set(killed), {11})
        self.assertEqual(left, [11])


if __name__ == "__main__":
    unittest.main()
