"""Pinned resumable worker profile leaves original runtime and providers intact."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import session_profile

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / 'patches/dsh-worker-session/headless-0.1.5-rc.1.js'


class SessionProfileTest(unittest.TestCase):
    def test_render_uses_core_create_resume_and_retains_original_runner(self):
        source = PINNED.read_text()
        rendered = session_profile.render(source)
        self.assertIn('openAgent(agents, {', rendered)
        self.assertIn('}, process.env);', rendered)
        self.assertIn('summarize(agent.session, firstSeq)', rendered)
        self.assertIn('await sessions.flush(agent.session)', rendered)
        self.assertNotIn('await agents.create({', rendered)
        self.assertEqual(source, PINNED.read_text())

    def test_changed_runtime_source_is_rejected_before_profile_install(self):
        with self.assertRaisesRegex(ValueError, 'pinned'):
            session_profile.render(PINNED.read_text() + '\n')

    def test_go_profile_preserves_and_verifies_its_local_adapter_package(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name) / 'home'
            profile = home / 'profiles/headless'
            adapter = profile / 'node_modules/spectre-dsh-go-session'
            adapter.mkdir(parents=True)
            (profile / 'package.json').write_text('{}')
            (profile / 'cordis.patch.yml').write_text('- insert:\n    - id: spectre-go-pi-ai\n'
                '      name: spectre-dsh-go-session\n')
            files = {'index.js': 'export const apply = () => {};',
                     'session.mjs': 'export const goSessionHeaders = () => ({});',
                     'package.json': '{"name":"spectre-dsh-go-session","type":"module","exports":"./index.js"}'}
            for path, body in files.items():
                (adapter / path).write_text(body)
            helper = ROOT / 'scripts/dsh-worker-session.mjs'
            session_profile.stage(home, PINNED, helper)
            target = home / 'profiles' / session_profile.PROFILE / 'node_modules/spectre-dsh-go-session'
            for path, body in files.items():
                self.assertEqual((target / path).read_text(), body)
                self.assertEqual((target / path).stat().st_mode & 0o777, 0o600)
            session_profile.verify(home, PINNED, helper)
            (target / 'index.js').unlink()
            with self.assertRaises(OSError):
                session_profile.verify(home, PINNED, helper)


    def test_staging_adds_distinct_profile_without_rewriting_provider_or_original(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name) / 'home'
            profile = home / 'profiles/headless'
            profile.mkdir(parents=True)
            source = PINNED.read_text()
            (profile / 'package.json').write_text('{}')
            (profile / 'cordis.patch.yml').write_text('- id: custom-provider\n  disabled: false\n')
            (home / 'settings.yaml').write_text('original provider profile')
            before = {p.name: p.read_bytes() for p in (profile / 'package.json', profile / 'cordis.patch.yml', home / 'settings.yaml')}
            helper = ROOT / 'scripts/dsh-worker-session.mjs'
            session_profile.stage(home, PINNED, helper)
            session_profile.stage(home, PINNED, helper)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (profile / 'package.json', profile / 'cordis.patch.yml', home / 'settings.yaml')})
            worker = home / 'profiles' / session_profile.PROFILE
            patch = (worker / 'cordis.patch.yml').read_text()
            self.assertIn('custom-provider', patch)
            self.assertIn('id: headless-runner', patch)
            self.assertIn('name: "@deepseek-ai/dsh-headless"\n  disabled: true', patch)
            self.assertIn('id: spectre-session-runner', patch)
            self.assertIn('name: spectre-dsh-worker-runner', patch)
            self.assertEqual((worker / 'node_modules/spectre-dsh-worker-runner/index.js').read_text(), session_profile.render(source))
            self.assertEqual((worker / 'node_modules/spectre-dsh-worker-runner/session.mjs').read_bytes(), helper.read_bytes())
            self.assertEqual(session_profile.verify(home, PINNED, helper)['profile'], session_profile.PROFILE)
            (worker / 'node_modules/spectre-dsh-worker-runner/package.json').write_text('{"exports":"./unreviewed.js"}')
            with self.assertRaisesRegex(ValueError, 'reviewed'):
                session_profile.verify(home, PINNED, helper)
            (worker / 'node_modules/spectre-dsh-worker-runner/package.json').write_text(session_profile.package_manifest())
            (worker / 'node_modules/spectre-dsh-worker-runner/session.mjs').write_text('unreviewed runner')
            with self.assertRaisesRegex(ValueError, 'reviewed'):
                session_profile.verify(home, PINNED, helper)


if __name__ == '__main__':
    unittest.main()
