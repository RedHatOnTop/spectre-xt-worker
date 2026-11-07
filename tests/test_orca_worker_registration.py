"""Dedicated DSH worker definitions preserve every existing native agent contract."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from orca_runtime import archive, worker_registration


class ArchiveTest(unittest.TestCase):
    def test_build_validates_every_pinned_module_before_writing_a_separate_archive(self):
        expected = {row['path']: b'patched-' + row['path'].encode() for row in worker_registration.EDITS}
        with patch.object(worker_registration.archive, 'read', return_value=b'pinned-source'), \
                patch.object(worker_registration, 'transform', side_effect=lambda path, body: expected[path]), \
                patch.object(worker_registration.archive, 'patch', return_value={'verified': True}) as write:
            result = worker_registration.build(Path('source.asar'), Path('target.asar'))
        self.assertEqual(result, {'verified': True})
        write.assert_called_once_with(Path('source.asar'), Path('target.asar'), expected)

    def test_archive_repack_preserves_packed_unpacked_and_integrity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            input_path = root / 'input.asar'
            header = {'files': {'packed.js': {'size': 3, 'offset': '0'},
                                'outside.js': {'size': 7, 'unpacked': True}}}
            archive.write(input_path, header, {'packed.js': b'one'})
            unpacked = Path(str(input_path) + '.unpacked')
            unpacked.mkdir()
            (unpacked / 'outside.js').write_bytes(b'outside')
            output_path = root / 'output.asar'
            result = archive.patch(input_path, output_path, {'packed.js': b'changed', 'outside.js': b'new external'})
            self.assertEqual(archive.read(output_path, 'packed.js'), b'changed')
            self.assertEqual(archive.read(output_path, 'outside.js'), b'new external')
            self.assertEqual(archive.read(input_path, 'packed.js'), b'one')
            self.assertEqual(result['changed'], ['outside.js', 'packed.js'])
            self.assertTrue(archive.verify(output_path))

    def test_source_digest_and_anchor_count_reject_wrong_runtime(self):
        value = worker_registration.EDITS[0]
        with self.assertRaisesRegex(ValueError, 'digest'):
            worker_registration.transform(value['path'], b'wrong source')
        with self.assertRaisesRegex(ValueError, 'anchor'):
            worker_registration.replace_exact('none', 'missing', 'replacement')
        with self.assertRaisesRegex(ValueError, 'anchor'):
            worker_registration.replace_exact('same same', 'same', 'replacement')

    def test_dedicated_route_definitions_have_distinct_names_and_no_original_agent_alias(self):
        configs = worker_registration.definitions()
        self.assertEqual(set(configs), {'dsh-worker', 'dsh-worker-go'})
        for name, route in (('dsh-worker', 'clinepass'), ('dsh-worker-go', 'opencode-go')):
            row = configs[name]
            self.assertEqual(row['launchCmd'], name)
            self.assertEqual(row['expectedProcess'], name)
            self.assertEqual(row['promptInjectionMode'], 'stdin-after-start')
            self.assertEqual(row['detectCmd'], name)

    def test_each_pinned_module_transforms_only_its_exact_registration_anchor(self):
        import hashlib
        fixtures = {
            worker_registration.CONFIG: 'const TUI_AGENT_CONFIG_SOURCE = { unchanged: {} };',
            worker_registration.MAIN_CONFIG: 'var n={claude:{detectCmd:`claude`}};',
            worker_registration.NAMES: "    muse: 'Muse',",
            worker_registration.MAIN_NAMES: 'const e={claude:`Claude`};',
            worker_registration.KIND: "    muse: 'muse'",
            worker_registration.ENUM: "    'muse',\n    'other'",
            worker_registration.MAIN: 'muse:`muse`};function other(){}; const x=`.trae.muse.other`.split(`.`);',
            worker_registration.READINESS: 'function xb(e){if(!e||e.length<1)return null}',
        }
        for name, value in fixtures.items():
            with self.subTest(name=name), patch.object(worker_registration, 'EDITS', (
                    {'path': name, 'sha256': hashlib.sha256(value.encode()).hexdigest()},)):
                transformed = worker_registration.transform(name, value.encode()).decode()
            if name == worker_registration.READINESS:
                self.assertIn('DSH Worker', transformed)
            else:
                self.assertIn('dsh-worker', transformed)
                self.assertIn('dsh-worker-go', transformed)
            self.assertEqual(value, fixtures[name])


if __name__ == '__main__':
    unittest.main()
