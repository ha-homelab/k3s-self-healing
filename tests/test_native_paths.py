"""Real filesystem aliases must not damage the previous verified SQL pair."""
from contextlib import redirect_stdout
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
spec = importlib.util.spec_from_file_location('paths_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RecorderPathsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'config'
        self.output = self.config / 'recorder_backup'
        self.stage = self.config / 'backups/recorder-export-staging'
        self.work = self.root / 'work'
        self.output.mkdir(parents=True)
        self.stage.mkdir(parents=True)
        self.sql = self.output / 'recorder.sql.gz'
        self.manifest = self.output / 'manifest.json'
        raw = b'INSERT INTO states VALUES (1);\n' * 100
        encoded = gzip.compress(raw)
        self.sql.write_bytes(encoded)
        self.manifest.write_text(json.dumps({
            'format': 1, 'file': self.sql.name,
            'sha256': hashlib.sha256(encoded).hexdigest(),
            'compressed_bytes': len(encoded), 'uncompressed_bytes': len(raw),
            'tables': [{'name': 'states'}],
        }))
        self.before = (self.sql.read_bytes(), self.manifest.read_bytes())
        self.old = runner.verify_dump(self.output)
        patched = patch.multiple(runner, CONFIG=self.config, OUTPUT=self.output,
                                 STAGE=self.stage, WORK=self.work)
        patched.start()
        self.addCleanup(patched.stop)

    def assert_previous_preserved(self):
        self.assertEqual((self.sql.read_bytes(), self.manifest.read_bytes()), self.before)
        self.assertEqual(runner.verify_dump(self.output), self.old)

    def reject_in_both_phases(self):
        for operation in (runner.prepare, runner.publish):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, 'recorder'):
                    operation()
                self.assert_previous_preserved()

    def test_staging_directory_alias_is_rejected_before_prepare_writes(self):
        self.stage.rmdir()
        self.stage.symlink_to(self.output, target_is_directory=True)
        self.reject_in_both_phases()

    def test_staging_parent_alias_is_rejected(self):
        self.stage.rmdir()
        self.stage.parent.rmdir()
        alternate = self.root / 'alternate'
        (alternate / self.stage.name).mkdir(parents=True)
        self.stage.parent.symlink_to(alternate, target_is_directory=True)
        self.reject_in_both_phases()
        self.assertEqual(list((alternate / self.stage.name).iterdir()), [])

    def test_manifest_symlink_and_hardlink_are_rejected(self):
        staged = self.stage / 'manifest.json'
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    staged.symlink_to(self.manifest)
                else:
                    os.link(self.manifest, staged)
                self.reject_in_both_phases()
                staged.unlink()

    def test_linked_output_directory_is_rejected(self):
        alternate = self.config / 'saved'
        self.output.rename(alternate)
        self.output.symlink_to(alternate, target_is_directory=True)
        self.reject_in_both_phases()

    def test_atomic_metadata_does_not_follow_legacy_temporary_link(self):
        temporary = self.stage / 'manifest.json.tmp'
        temporary.symlink_to(self.manifest)
        runner.write_json(self.stage / 'manifest.json', {'format': 2})
        self.assert_previous_preserved()
        self.assertEqual((self.stage / 'manifest.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.stage.glob('.manifest.json.*')), [])

    def test_failed_atomic_replace_leaves_previous_metadata_and_no_temp(self):
        with patch.object(runner.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                runner.write_json(self.manifest, {'format': 2})
        self.assert_previous_preserved()
        self.assertEqual(list(self.output.glob('.manifest.json.*')), [])

    def test_normal_prepare_creates_staging_without_touching_verified_pair(self):
        self.stage.rmdir()
        (self.config / 'secrets.yaml').write_text(
            'recorder_db_url: mysql://localhost/fixture\n')
        engine, connection = MagicMock(), MagicMock()
        engine.connect.return_value.__enter__.return_value = connection
        connection.exec_driver_sql.side_effect = [
            SimpleNamespace(scalar=lambda: '8.4.0'),
            [SimpleNamespace(_mapping={'name': 'states', 'engine': 'InnoDB'})],
        ]
        parsed = SimpleNamespace(get_backend_name=lambda: 'mysql', database='fixture',
                                 host='db.example.invalid', port=None,
                                 username='fixture', password='')
        sqlalchemy = SimpleNamespace(engine=SimpleNamespace(make_url=lambda _: parsed),
                                     create_engine=lambda _: engine)
        disk = SimpleNamespace(total=54 * runner.GIB, free=20 * runner.GIB)
        with (patch.dict(sys.modules, {'sqlalchemy': sqlalchemy}),
              patch.object(runner, 'measure_config', return_value=0),
              patch.object(runner, 'backup_filesystems', return_value=(disk, disk, False)),
              redirect_stdout(io.StringIO())):
            runner.prepare()
        self.assert_previous_preserved()
        self.assertEqual(json.loads((self.stage / 'manifest.json').read_text())['format'], 2)
        self.assertEqual((self.work / 'client.cnf').stat().st_mode & 0o777, 0o600)
