"""Offline native archive recovery: integrity, path containment and empty destinations."""
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import shutil
import stat
import subprocess
from pathlib import Path
import tarfile
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
spec = importlib.util.spec_from_file_location('native_restore', ROOT / 'native_backup/restore.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class NativeRestoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, extra=(), bad_hash=False, links=(), version=1, inner_transform=None):
        sql = b'CREATE TABLE states (state_id BIGINT);\n'
        compressed = gzip.compress(sql) if version == 1 else lzma.compress(sql, preset=0)
        manifest = {'format': 1, 'file': 'recorder.sql.gz', 'compressed_bytes': len(compressed),
                    'uncompressed_bytes': len(sql), 'sha256': '0' * 64 if bad_hash else hashlib.sha256(compressed).hexdigest(),
                    'tables': [{'name': 'states', 'engine': 'InnoDB'}], 'completed_at': '2026-09-25T00:00:00+00:00'}
        if version == 2:
            manifest.update(format=2, compression='xz', file=f"recorder-{manifest['sha256']}.sql.xz")
        files = [(x, b'fixture') for x in ['configuration.yaml', 'secrets.yaml', '.storage/core.config_entries',
                                         '.storage/core.entity_registry', '.storage/auth']]
        files += [('recorder_backup/manifest.json', json.dumps(manifest).encode()), ('recorder_backup/' + manifest['file'], compressed)]
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode='w:gz') as tar:
            for name, value in files + list(extra):
                member = tarfile.TarInfo('data/' + name)
                member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
            for name, target in links:
                member = tarfile.TarInfo('data/' + name)
                member.type = tarfile.SYMTYPE
                member.linkname = target
                tar.addfile(member)
        payload = inner.getvalue()
        if inner_transform:
            payload = inner_transform(payload)
        path = self.root / 'native.tar'
        with tarfile.open(path, 'w') as tar:
            meta = {'slug': 'abc12345', 'date': '2026-09-25', 'protected': False, 'homeassistant': {'version': '2026.9.1'}}
            for name, value in [('./backup.json', json.dumps(meta).encode()), ('homeassistant.tar.gz', payload)]:
                member = tarfile.TarInfo(name);member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
        return path

    def test_recovers_identity_config_and_external_database(self):
        report = restore.extract(self.archive(), self.root / 'config')
        self.assertEqual(report['backup_id'], 'abc12345')
        self.assertEqual(report['database']['tables'], 1)
        self.assertFalse(report['homeassistant_started'])
        self.assertFalse(report['database_imported'])
        self.assertTrue((self.root / 'config/.storage/auth').is_file())

    def test_existing_shared_directory_is_private_before_reading_the_backup(self):
        destination = self.root / 'precreated'
        destination.mkdir()
        destination.chmod(0o777)
        original = restore.backup_metadata

        def verify_private(archive):
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            return original(archive)

        with patch.object(restore, 'backup_metadata', side_effect=verify_private):
            restore.extract(self.archive(), destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertTrue((destination / '.storage/auth').is_file())

    def test_isolated_copied_restore_bundle_extracts_xz_native_archive(self):
        bundle = self.root / 'bundle';bundle.mkdir()
        for name in ('restore.py', 'dump_format.py'):
            shutil.copyfile(ROOT / 'native_backup' / name, bundle / name)
        destination = self.root / 'restored';report = self.root / 'report.json'
        result = subprocess.run([sys.executable, str(bundle / 'restore.py'), str(self.archive(version=2)), '--destination', str(destination), '--report', str(report)], cwd=bundle, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = restore.verify_database(destination)
        self.assertEqual(meta['format'], 2)
        self.assertEqual(lzma.decompress((destination / 'recorder_backup' / meta['file']).read_bytes()), b'CREATE TABLE states (state_id BIGINT);\n')
        receipt = json.loads(report.read_text())
        self.assertEqual(receipt['database']['sha256'], meta['sha256'])
        self.assertFalse(receipt['homeassistant_started']);self.assertFalse(receipt['database_imported'])

    def test_checksum_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, 'checksum'):
            restore.extract(self.archive(bad_hash=True), self.root / 'config')

    def test_native_gzip_crc_corruption_is_rejected(self):
        def corrupt_crc(payload):
            return payload[:-8] + bytes([payload[-8] ^ 0xff]) + payload[-7:]
        with self.assertRaises(gzip.BadGzipFile):
            restore.extract(self.archive(inner_transform=corrupt_crc), self.root / 'config')

    def test_native_gzip_truncated_trailer_is_rejected(self):
        with self.assertRaises(EOFError):
            restore.extract(self.archive(inner_transform=lambda payload: payload[:-4]), self.root / 'config')

    def test_corrupt_gzip_member_after_tar_end_is_rejected(self):
        def trailing_member(payload):
            # A later gzip member is outside the first tar's end blocks, but
            # remains part of the compressed archive and must be validated.
            tail = gzip.compress(b'\0' * 1024)
            return payload + tail[:-8] + bytes([tail[-8] ^ 0xff]) + tail[-7:]
        with self.assertRaises(gzip.BadGzipFile):
            restore.extract(self.archive(inner_transform=trailing_member), self.root / 'config')

    def test_existing_config_is_never_overwritten(self):
        destination = self.root / 'config';destination.mkdir();(destination / 'keep').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty'):
            restore.extract(self.archive(), destination)
        self.assertEqual((destination / 'keep').read_text(), 'keep')

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            restore.extract(self.archive([('../escape', b'bad')]), self.root / 'config')
        self.assertFalse((self.root / 'escape').exists())

    def test_duplicate_members_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            restore.extract(self.archive([('secrets.yaml', b'bad')]), self.root / 'config')

    def test_archive_metadata_duplicates_are_rejected(self):
        path = self.archive()
        with tarfile.open(path, 'a') as tar:
            value = b'{}';member = tarfile.TarInfo('backup.json');member.size = len(value)
            tar.addfile(member, io.BytesIO(value))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            restore.backup_metadata(path)

    def test_symlinked_destination_is_rejected(self):
        destination = self.root / 'link';destination.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'empty'):
            restore.extract(self.archive(), destination)

    def test_absolute_config_root_link_is_remapped_inside_restore(self):
        destination = self.root / 'restored'
        report = restore.extract(self.archive(links=[('config', '/config')]), destination)
        self.assertEqual((destination / 'config').resolve(), destination.resolve())
        self.assertEqual(report['external_links_skipped'], [])

    def test_external_links_are_reported_without_recreating_them(self):
        destination = self.root / 'restored'
        report = restore.extract(self.archive(links=[('external', '/etc')]), destination)
        self.assertFalse((destination / 'external').is_symlink())
        self.assertEqual(report['external_links_skipped'], ['external'])


if __name__ == '__main__':
    unittest.main()


class ResumeTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('native_installer', ROOT / 'scripts/restore-native-backup.py')
        self.installer = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.installer)
        self.state = {'metadata': {'annotations': {'meta.helm.sh/release-name': 'ha-recovery'}}, 'spec': {'replicas': 0}}
        self.pod = {'metadata': {'labels': {'app': 'ha-recovery-restore'}}, 'spec': {'containers': [{'image': 'fixture', 'command': ['sleep', 'infinity']}]}}

    def test_stopped_owned_workspace_can_resume(self):
        self.installer.validate_resume(self.state, self.pod, 'fixture')

    def test_running_ha_is_never_resumed(self):
        self.state['spec']['replicas'] = 1
        with self.assertRaisesRegex(ValueError, 'stopped'):self.installer.validate_resume(self.state, self.pod, 'fixture')

    def test_unrelated_image_is_never_resumed(self):
        with self.assertRaisesRegex(ValueError, 'stopped'):self.installer.validate_resume(self.state, self.pod, 'another-image')
