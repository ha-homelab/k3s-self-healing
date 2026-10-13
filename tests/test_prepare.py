"""Offline metadata preparation: root checks mocked, no elevated host writes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disaster_recovery import prepare, restore


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.snapshot = self.base / 'snapshot'
        self.snapshot.mkdir()
        self.workspace = self.base / 'prepared'
        self.manifest = {'format_version': 1, 'completed': True, 'components': []}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(prepare.os, 'geteuid', return_value=0))
        stack.enter_context(patch.object(prepare, '_check_workspace_owner'))
        self.trust_guard = prepare._check_trusted_directory
        stack.enter_context(patch.object(prepare, '_check_trusted_directory'))
        self.chown = stack.enter_context(patch.object(prepare.os, 'fchown'))

    def component(self, name='application', special=False, unsafe=False, links=False, root_mode=0o750):
        archive = self.snapshot / (name + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as output:
            for path, mode, data in [('.', root_mode, None), ('nested', 0o710, None),
                                     ('nested/helper', 0o4755 if special else 0o755, b'never execute this fixture')]:
                member = tarfile.TarInfo('../outside' if unsafe and data else path)
                member.mode, member.uid, member.gid, member.mtime = mode, 1883, 1884, 1234567890
                if data is None:
                    member.type = tarfile.DIRTYPE
                else:
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data) if data else None)
            if links:
                for path, target, kind in [('safe-alias', 'nested/helper', tarfile.SYMTYPE),
                                           ('absolute-alias', '/etc', tarfile.SYMTYPE),
                                           ('hard-alias', 'nested/helper', tarfile.LNKTYPE)]:
                    member = tarfile.TarInfo(path)
                    member.type, member.linkname = kind, target
                    member.mode, member.uid, member.gid, member.mtime = 0o755, 1883, 1884, 1234567890
                    output.addfile(member)
        self.manifest['components'].append({'name': name, 'archive': archive.name,
            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'bytes': archive.stat().st_size})
        (self.snapshot / 'manifest.json').write_text(json.dumps(self.manifest))

    def receipt(self):
        return json.loads((self.workspace / 'preparation.json').read_text())

    def test_selected_metadata_mtime_and_hardlinks_with_private_outer_workspace(self):
        self.component(links=True)
        self.component('untouched')
        result = prepare.prepare_snapshot(self.snapshot, self.workspace, ['application'])
        selected = self.workspace / 'data/application'
        payload = selected / 'nested/helper'
        self.assertEqual(stat.S_IMODE(payload.stat().st_mode), 0o755)
        self.assertEqual(int(payload.stat().st_mtime), 1234567890)
        self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o750)
        self.assertEqual(int(selected.stat().st_mtime), 1234567890)
        self.assertEqual(payload.stat().st_ino, (selected / 'hard-alias').stat().st_ino)
        self.assertTrue((selected / 'safe-alias').is_symlink())
        self.assertFalse((selected / 'absolute-alias').exists())
        self.assertEqual(len(result['links_not_prepared']), 2)
        self.assertEqual({(args[1], args[2]) for args, _ in self.chown.call_args_list}, {(1883, 1884)})
        self.assertEqual(self.chown.call_count, 3)  # File and two directories, no alias dereference.
        self.assertEqual(stat.S_IMODE((self.workspace / 'data/untouched/nested/helper').stat().st_mode), 0o600)
        for path in (self.workspace, self.workspace / 'data', self.workspace / 'data/untouched'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.workspace / 'preparation.json').stat().st_mode), 0o600)
        self.assertEqual(self.receipt()['status'], 'prepared')
        self.assertFalse(result['host_apply_performed'])

    def test_existing_destination_and_missing_or_invalid_selection_are_rejected(self):
        self.component()
        self.workspace.mkdir()
        sentinel = self.workspace / 'sentinel'
        sentinel.write_text('untouched')
        with self.assertRaises(prepare.PreparationError):
            prepare.prepare_snapshot(self.snapshot, self.workspace, ['application'])
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertEqual(list(self.workspace.iterdir()), [sentinel])
        for selection in ([], ['application', 'application'], ['../escape']):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                prepare.prepare_snapshot(self.snapshot, self.base / 'new', selection)
        with self.assertRaises(prepare.PreparationError):
            prepare.prepare_snapshot(self.snapshot, self.base / 'missing', ['absent'])
        self.assertFalse(json.loads((self.base / 'missing/preparation.json').read_text())['completed'])
        self.chown.assert_not_called()

    def test_nonroot_and_workspace_inside_snapshot_fail_before_creating_files(self):
        self.component()
        with patch.object(prepare.os, 'geteuid', return_value=1000):
            with self.assertRaises(prepare.PreparationError):
                prepare.prepare_snapshot(self.snapshot, self.workspace, ['application'])
        self.assertFalse(self.workspace.exists())
        with self.assertRaises(prepare.PreparationError):
            prepare.prepare_snapshot(self.snapshot, self.snapshot / 'prepared', ['application'])
        self.assertFalse((self.snapshot / 'prepared').exists())

    def test_legitimate_directory_setgid_and_sticky_are_preserved_but_setuid_is_rejected(self):
        self.component('setgid', root_mode=0o2777)
        self.component('sticky', root_mode=0o1777)
        report = prepare.prepare_snapshot(self.snapshot, self.workspace, ['setgid', 'sticky'])
        self.assertEqual(stat.S_IMODE((self.workspace / 'data/setgid').stat().st_mode), 0o2777)
        self.assertEqual(stat.S_IMODE((self.workspace / 'data/sticky').stat().st_mode), 0o1777)
        self.assertEqual(len(report['directory_special_modes']), 2)
        self.component('setuid', root_mode=0o4755)
        with self.assertRaises(prepare.PreparationError):
            prepare.prepare_snapshot(self.snapshot, self.base / 'setuid-refused', ['setuid'])

    def test_workspace_ancestry_rejects_nonroot_owners_and_unprotected_writable_parents(self):
        for uid, mode in ((1000, 0o700), (0, 0o777), (0, 0o775)):
            with self.subTest(uid=uid, mode=mode), patch.object(prepare.os, 'fstat', return_value=SimpleNamespace(st_uid=uid, st_mode=mode)):
                with self.assertRaises(prepare.PreparationError):
                    self.trust_guard(99)
        for mode in (0o700, 0o755, 0o1777):
            with patch.object(prepare.os, 'fstat', return_value=SimpleNamespace(st_uid=0, st_mode=mode)):
                self.trust_guard(99)

    def test_all_selected_special_bits_are_rejected_before_first_metadata_change(self):
        self.component('valid')
        self.component('special', special=True)
        with self.assertRaisesRegex(prepare.PreparationError, 'Special permission'):
            prepare.prepare_snapshot(self.snapshot, self.workspace, ['valid', 'special'])
        self.chown.assert_not_called()
        self.assertEqual(stat.S_IMODE((self.workspace / 'data/valid/nested/helper').stat().st_mode), 0o600)
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_malicious_archive_is_rejected_without_any_metadata_change(self):
        self.component(unsafe=True)
        with self.assertRaises(restore.RestoreError):
            prepare.prepare_snapshot(self.snapshot, self.workspace, ['application'])
        self.chown.assert_not_called()
        self.assertFalse((self.base / 'outside').exists())
        self.assertFalse(self.receipt()['completed'])

    def test_replaced_ancestor_symlink_and_external_hardlink_cannot_modify_outside_files(self):
        self.component()
        original = restore.verify_snapshot
        outside = self.base / 'outside'
        outside.mkdir()
        sentinel = outside / 'helper'
        sentinel.write_bytes(b'never execute this fixture')
        sentinel.chmod(0o640)
        for attack in ('symlink', 'hardlink'):
            destination = self.base / ('prepared-' + attack)

            def tampered(snapshot, extract_to):
                report = original(snapshot, extract_to=extract_to)
                nested = extract_to / 'application/nested'
                (nested / 'helper').unlink()
                if attack == 'symlink':
                    nested.rmdir()
                    nested.symlink_to(outside, target_is_directory=True)
                else:
                    os.link(sentinel, nested / 'helper')
                return report

            with self.subTest(attack=attack), patch.object(prepare.restore, 'verify_snapshot', side_effect=tampered):
                with self.assertRaises((prepare.PreparationError, OSError)):
                    prepare.prepare_snapshot(self.snapshot, destination, ['application'])
            self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o640)
            self.assertEqual(sentinel.read_bytes(), b'never execute this fixture')
        self.chown.assert_not_called()

    def test_failed_metadata_write_has_private_incomplete_receipt_and_safe_console(self):
        self.component()
        with patch.object(prepare.os, 'fchown', side_effect=OSError('private detail must not appear')):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = prepare.main([str(self.snapshot), '--workspace', str(self.workspace), '--component', 'application'])
        self.assertEqual(status, 1)
        self.assertNotIn('private detail', errors.getvalue())
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.receipt()['status'], 'failed')
        self.assertFalse(self.receipt()['completed'])


if __name__ == '__main__':
    unittest.main()
