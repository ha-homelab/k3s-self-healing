"""Protect source ownership, immutable pins and the public import boundary."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import ha_sources as sources

REVISION = 'a' * 40


def fixtures():
    return {
        'config/packages/core_updates.yaml': (
            '# Generic one\n# Generic two\n# Site detail\n# Site detail\n\n'
            'rest_command:\n  approve:\n    url: http://192.0.2.10/approve\n'
            '    notify_service: notify.fixture_one\n'
            '    notify_service: notify.fixture_two\n'
            '    notify_service: notify.fixture_three\n').encode(),
        'config/packages/hacs_maintenance.yaml': b'script: {}\n',
        'config/blueprints/automation/ha_homelab/motion_light_off.yaml': b'blueprint: {}\n',
        'tests/test_core_updates.py': (
            "PACKAGE = Path(__file__).resolve().parents[1] / 'config/packages/core_updates.yaml'\n"
            "FixtureLoader.add_constructor('!secret', lambda loader, node: 'Bearer TEST_ONLY')\n"
            "self.assertEqual(notification['notify_service'], 'notify.fixture_one')\n").encode(),
        'tests/test_hacs_maintenance.py': (
            'try:\n    import homeassistant\nexcept ImportError:\n    HA_AVAILABLE = False\n'
            "PACKAGE = Path(__file__).resolve().parents[1] / 'config/packages/hacs_maintenance.yaml'\n").encode(),
    }


class SourceLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.originals = fixtures()
        self.fetch = Mock(side_effect=lambda revision, source: self.originals[source])
        self.lock = sources.refresh(REVISION, self.root, self.fetch)
        self.fetch.reset_mock()

    def test_offline_check_uses_no_network_and_online_check_reproduces_all_paths(self):
        sources.check(self.root, fetch=self.fetch)
        self.fetch.assert_not_called()
        sources.check(self.root, upstream=True, fetch=self.fetch)
        self.assertEqual([call.args for call in self.fetch.call_args_list],
                         [(REVISION, entry[1]) for entry in sources.FILES])

    def test_independent_edit_is_rejected(self):
        path = self.root / sources.FILES[1][0]
        path.write_text('script: {independent: true}\n')
        with self.assertRaisesRegex(ValueError, 'changed independently'):
            sources.check(self.root)

    def test_changing_output_digest_cannot_bypass_upstream_identity(self):
        path = self.root / sources.FILES[1][0]
        path.write_text('script: {independent: true}\n')
        self.lock['files'][1]['output_sha256'] = sources.digest(path.read_bytes())
        (self.root / sources.LOCK).write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(ValueError, 'regeneration mismatch'):
            sources.check(self.root, upstream=True, fetch=self.fetch)

    def test_changed_upstream_content_is_rejected_before_transform(self):
        self.originals[sources.FILES[0][1]] += b'# changed\n'
        with self.assertRaisesRegex(ValueError, 'source digest mismatch'):
            sources.check(self.root, upstream=True, fetch=self.fetch)

    def test_lock_cannot_redirect_repository_or_add_source_files(self):
        variants = [('repository', 'example/other'), ('revision', 'main'), ('files', self.lock['files'] + [self.lock['files'][0]])]
        for key, value in variants:
            with self.subTest(key=key):
                invalid = dict(self.lock, **{key: value})
                (self.root / sources.LOCK).write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    sources.check(self.root)

    def test_lock_cannot_change_a_reviewed_source_path_or_transform(self):
        for key, value in [('source', 'config/secrets.yaml'), ('transformation', 'unknown'), ('source_sha256', 'b' * 39)]:
            with self.subTest(key=key):
                invalid = json.loads(json.dumps(self.lock))
                invalid['files'][0][key] = value
                (self.root / sources.LOCK).write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    sources.check(self.root)

    def test_malformed_lock_types_fail_with_validation_error(self):
        variants = [[], None, dict(self.lock, format=True), dict(self.lock, files={}),
                    dict(self.lock, files=[[]] + self.lock['files'][1:])]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                (self.root / sources.LOCK).write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    sources.check(self.root)

    def test_refresh_privacy_failure_does_not_modify_any_existing_output(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        sensitive = '+' + '1' + '2025550199'
        self.originals[sources.FILES[-1][1]] += ('# ' + sensitive + '\n').encode()
        with self.assertRaisesRegex(ValueError, 'privacy checks') as caught:
            sources.refresh('b' * 40, self.root, self.fetch)
        self.assertNotIn(sensitive, str(caught.exception))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_generated_symlink_is_not_followed(self):
        path = self.root / sources.FILES[1][0]
        target = self.root / 'outside.yaml'
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'non-symlink'):
            sources.check(self.root)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            sources.refresh(REVISION, self.root, self.fetch)

    def test_lock_symlink_is_not_read_or_overwritten(self):
        path = self.root / sources.LOCK
        target = self.root / 'outside.json'
        original = path.read_bytes()
        target.write_bytes(original)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'non-symlink'):
            sources.check(self.root)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            sources.refresh(REVISION, self.root, self.fetch)
        self.assertEqual(target.read_bytes(), original)

    def test_nonregular_lock_stops_refresh_before_network_or_source_writes(self):
        path = self.root / sources.LOCK
        path.unlink()
        path.mkdir()
        before = {item: item.read_bytes() for item in self.root.rglob('*') if item.is_file()}
        with self.assertRaisesRegex(ValueError, 'regular path'):
            sources.refresh(REVISION, self.root, self.fetch)
        self.fetch.assert_not_called()
        self.assertEqual(before, {item: item.read_bytes() for item in self.root.rglob('*') if item.is_file()})

    def test_core_transform_removes_installation_recipients_and_endpoint(self):
        text = (self.root / sources.FILES[0][0]).read_text()
        self.assertNotIn('Site detail', text)
        self.assertNotIn('notify.fixture_', text)
        self.assertNotIn('192.0.2.10', text)
        self.assertEqual(text.count('!secret optional_phone_notify_service'), 3)
        self.assertIn('!secret ha_core_updater_approve_url', text)

    def test_transform_structure_changes_fail_closed(self):
        original = self.originals[sources.FILES[0][1]]
        with self.assertRaisesRegex(ValueError, 'structure changed'):
            sources.transform(original.replace(b'notify_service:', b'other_service:', 1), 'core-package-v1')
        with self.assertRaisesRegex(ValueError, 'header changed'):
            sources.transform(original.replace(b'# Site detail\n', b'', 1), 'core-package-v1')
        with self.assertRaises(ValueError):
            sources.transform(b'test', 'unknown')


class FetchBoundaryTests(unittest.TestCase):
    def test_unpinned_revision_and_unreviewed_path_never_reach_network(self):
        with patch.object(sources.urllib.request, 'build_opener') as opener:
            for revision, path in [('main', sources.FILES[0][1]), (REVISION, '../secrets.yaml')]:
                with self.assertRaises(ValueError):
                    sources.fetch_source(revision, path)
            opener.assert_not_called()

    def test_fetch_uses_fixed_https_commit_path_and_bounded_read(self):
        response = Mock()
        response.read.return_value = b'example\n'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(sources.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(sources.fetch_source(REVISION, sources.FILES[0][1]), b'example\n')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f'https://raw.githubusercontent.com/ha-homelab/home-assistant/{REVISION}/{sources.FILES[0][1]}')
        response.read.assert_called_once_with(sources.MAX_SOURCE_BYTES + 1)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 30)

    def test_oversized_payload_and_redirect_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Invalid HA source'):
            sources.transform(b'x' * (sources.MAX_SOURCE_BYTES + 1), 'identity-v1')
        with self.assertRaisesRegex(ValueError, 'redirects'):
            sources.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com/other')


if __name__ == '__main__':
    unittest.main()
