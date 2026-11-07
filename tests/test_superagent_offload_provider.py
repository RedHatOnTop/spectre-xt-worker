"""Provider resource operations retain exact route/configuration and literal failures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class OffloadProviderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = self.root / 'lightning.env'
        self.env.write_text('LIGHTNING_STUDIO=existing-studio\nLIGHTNING_TEAMSPACE=approved/teamspace\nLIGHTNING_MACHINE=CPU\n')
        self.env.chmod(0o600)
        self.target = {'studio': 'existing-studio', 'teamspace': 'approved/teamspace',
                       'remote_dir': 'offload/job_1', 'remote_boot_id': 'original-boot'}

    def module(self):
        from superagent_queue import offload_provider
        return offload_provider

    def test_status_and_stop_validate_original_route_and_use_current_private_config(self):
        module = self.module()
        status = subprocess.CompletedProcess([], 0, '[{"name":"existing-studio","status":"Running"}]', '')
        boot = subprocess.CompletedProcess([], 0, 'original-boot\n', '')
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module.subprocess, 'run', side_effect=[status, boot]) as run:
            result = module.query(self.target, 'status')
        self.assertEqual(result, {'status': 'Running', 'remote_boot_id': 'original-boot'})
        self.assertEqual(run.call_args_list[1].args[0][-1], 'cat /proc/sys/kernel/random/boot_id')
        stop = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module.subprocess, 'run', return_value=stop) as run:
            module.query(self.target, 'stop')
        self.assertIn('stop', run.call_args.args[0])

    def test_actual_shell_config_does_not_allow_inherited_paid_machine_or_foreign_studio(self):
        module = self.module()
        cli = self.root / 'lightning'
        cli.write_text('#!/bin/sh\nprintf \'[{"name":"existing-studio","status":"Stopped"}]\\n\'\n')
        cli.chmod(0o755)
        with patch.dict(os.environ, {'LIGHTNING_MACHINE': 'PAID_GPU', 'LIGHTNING_STUDIO': 'foreign'}), \
                patch.object(module, 'ENV_FILE', self.env), patch.object(module, 'CLI', cli):
            self.assertEqual(module.query(self.target, 'status'), {'status': 'Stopped'})
        self.env.write_text('LIGHTNING_STUDIO=foreign\nLIGHTNING_TEAMSPACE=approved/teamspace\nLIGHTNING_MACHINE=CPU\n')
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module, 'CLI', cli), \
                self.assertRaisesRegex(RuntimeError, 'configuration no longer matches'):
            module.query(self.target, 'stop')

    def test_failure_unknown_state_and_nonprivate_config_never_fake_success(self):
        module = self.module()
        failure = subprocess.CompletedProcess([], 1, 'partial output', 'provider521 verbatim')
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module.subprocess, 'run', return_value=failure), \
                self.assertRaisesRegex(RuntimeError, 'partial output\nstderr:\nprovider521 verbatim'):
            module.query(self.target, 'status')
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module.subprocess, 'run', return_value=
            subprocess.CompletedProcess([], 0, '[{"name":"existing-studio","status":"Unknown"}]', '')), self.assertRaises(ValueError):
            module.query(self.target, 'status')
        self.env.chmod(0o644)
        with patch.object(module, 'ENV_FILE', self.env), patch.object(module.subprocess, 'run') as run, self.assertRaises(ValueError):
            module.query(self.target, 'stop')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
