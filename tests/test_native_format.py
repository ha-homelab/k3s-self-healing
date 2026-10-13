"""Real codecs and fault injection preserve a usable recovery generation."""
import gzip
import hashlib
import importlib.util
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
from dump_format import read_manifest, verify_dump
spec = importlib.util.spec_from_file_location('format_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class FormatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, name in [('CONFIG', 'config'), ('OUTPUT', 'config/recorder_backup'), ('STAGE', 'stage'), ('WORK', 'work')]:
            path = self.root / name;path.mkdir(parents=True, exist_ok=True)
            p = patch.object(runner, key, path);p.start();self.addCleanup(p.stop)
        self.sql = b'INSERT INTO states VALUES (1);\n' * 1000
        self.old = self.fixture(self.sql, 1)
        self.stage(self.sql + b'-- next\n')

    def fixture(self, sql, version):
        data = gzip.compress(sql) if version == 1 else lzma.compress(sql, preset=0)
        digest = hashlib.sha256(data).hexdigest()
        meta = {'format': version, 'file': 'recorder.sql.gz' if version == 1 else f'recorder-{digest}.sql.xz',
                'sha256': digest, 'compressed_bytes': len(data), 'uncompressed_bytes': len(sql),
                'tables': [{'name': 'states', 'engine': 'InnoDB'}]}
        if version == 2:meta['compression'] = 'xz'
        (runner.OUTPUT / meta['file']).write_bytes(data)
        runner.write_json(runner.OUTPUT / 'manifest.json', meta)
        return meta

    def stage(self, sql):
        self.pending = runner.STAGE / 'recorder.sql.xz.pending'
        self.pending.write_bytes(lzma.compress(sql, preset=0))
        runner.write_json(runner.STAGE / 'manifest.json', {'format': 2, 'compression': 'xz', 'tables': [{'name': 'states'}]})

    def test_xz_roundtrip_and_legacy_removal(self):
        self.assertEqual(verify_dump(runner.OUTPUT), self.old)
        runner.publish()
        new = verify_dump(runner.OUTPUT)
        self.assertEqual(new['format'], 2)
        self.assertEqual(new['uncompressed_bytes'], len(self.sql + b'-- next\n'))
        self.assertFalse((runner.OUTPUT / self.old['file']).exists())
        self.assertFalse(self.pending.exists())

    def test_corrupt_and_truncated_new_dump_preserve_valid_old_pair(self):
        for content in (b'broken', self.pending.read_bytes()[:-8], lzma.compress(self.sql, format=lzma.FORMAT_ALONE)):
            self.pending.write_bytes(content)
            with self.assertRaises((lzma.LZMAError, EOFError)):runner.publish()
            self.assertEqual(verify_dump(runner.OUTPUT), self.old)

    def test_malformed_staged_metadata_preserves_previous_pair(self):
        for tables in ([], None, ['states'], [{}]):
            runner.write_json(runner.STAGE / 'manifest.json', {'format': 2, 'compression': 'xz', 'tables': tables})
            with self.assertRaises(ValueError):runner.publish()
            self.assertEqual(verify_dump(runner.OUTPUT), self.old)
            self.assertTrue(self.pending.exists())

    def test_both_legacy_and_xz_precommit_crash_preserve_previous_pair(self):
        for version in (1, 2):
            for path in runner.OUTPUT.iterdir():path.unlink()
            old = self.fixture(self.sql, version);self.stage(self.sql + b'next')
            with patch.object(runner, 'write_json', side_effect=OSError('injected manifest failure')):
                with self.assertRaises(OSError):runner.publish()
            self.assertEqual(verify_dump(runner.OUTPUT), old)
            with self.assertRaisesRegex(RuntimeError, 'Inactive'):runner.check_native_capacity(0)
            self.assertEqual(runner.active_export(cleanup=True).name, old['file'])
            self.assertEqual(len([p for p in runner.OUTPUT.iterdir() if p.name.endswith(('.gz', '.xz'))]), 1)

    def test_postcommit_cleanup_failure_keeps_new_valid_pair_and_blocks_native(self):
        original = Path.unlink
        def refuse(path, *args, **kwargs):
            if path == runner.OUTPUT / self.old['file']:raise PermissionError('injected cleanup failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', refuse):
            with self.assertRaises(PermissionError):runner.publish()
        new = verify_dump(runner.OUTPUT);self.assertEqual(new['format'], 2)
        with self.assertRaisesRegex(RuntimeError, 'Inactive'):runner.check_native_capacity(0)
        runner.active_export(cleanup=True)
        self.assertEqual(verify_dump(runner.OUTPUT), new)

    def test_missing_or_corrupt_manifest_never_deletes_recovery_data(self):
        known = runner.OUTPUT / ('recorder-' + 'f' * 64 + '.sql.xz');known.write_bytes(b'orphan')
        for bad in (None, '{}', '{'):
            manifest = runner.OUTPUT / 'manifest.json'
            if bad is None:manifest.unlink(missing_ok=True)
            else:manifest.write_text(bad)
            with self.assertRaises((ValueError, FileNotFoundError)):runner.active_export(cleanup=True)
            self.assertTrue(known.exists());self.assertTrue((runner.OUTPUT / self.old['file']).exists())

    def test_bad_codec_filename_hash_and_size_rejected(self):
        runner.publish();valid = verify_dump(runner.OUTPUT)
        for changes in ({'compression': 'gzip'}, {'format': 9}, {'format': True}, {'format': 2.0}, {'file': '../escape.sql.xz'}, {'sha256': 'f' * 64}, {'compressed_bytes': 1}):
            runner.write_json(runner.OUTPUT / 'manifest.json', {**valid, **changes})
            with self.assertRaises((ValueError, FileNotFoundError)):verify_dump(runner.OUTPUT)

    def test_identical_generation_is_reused_without_overwriting(self):
        runner.publish();meta = verify_dump(runner.OUTPUT);source = runner.OUTPUT / meta['file'];inode = source.stat().st_ino
        self.stage(self.sql + b'-- next\n');runner.publish()
        self.assertEqual(source.stat().st_ino, inode)
        self.assertEqual(verify_dump(runner.OUTPUT)['sha256'], meta['sha256'])

    def test_cleanup_preserves_unrecognized_files_and_refuses_symlinks(self):
        other = runner.OUTPUT / 'my-data.sql.xz';other.write_bytes(b'preserve')
        stale = runner.OUTPUT / ('recorder-' + 'e' * 64 + '.sql.xz');stale.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):runner.active_export(cleanup=True)
        self.assertTrue(stale.is_symlink());self.assertEqual(other.read_bytes(), b'preserve')

    def test_sparse_old_file_credit_is_limited_to_allocated_bytes(self):
        path = self.root / 'sparse'
        with path.open('wb') as stream:stream.seek(16 * 1024**2);stream.write(b'x')
        self.assertLessEqual(runner.reclaimable_export(path), path.stat().st_blocks * 512)

    def test_write_sync_precedes_manifest_pointer(self):
        events=[];real_replace=os.replace;real_sync=os.fsync
        def replace(src,dst):events.append(('replace',Path(dst).name));return real_replace(src,dst)
        def sync(fd):events.append(('sync',None));return real_sync(fd)
        with patch.object(runner.os,'replace',replace),patch.object(runner.os,'fsync',sync):runner.publish()
        pointer=events.index(('replace','manifest.json'))
        self.assertGreaterEqual(sum(e[0]=='sync' for e in events[:pointer]),3)
        self.assertEqual(events[pointer+1],('sync',None))

    def test_shared_module_is_bundled_by_both_tools(self):
        for name in ('install-native-backup.py','restore-native-backup.py'):
            source=(ROOT/'scripts'/name).read_text()
            self.assertIn("'dump_format.py'",source)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('xz'), 'GNU stat and xz required')
class ImportPipelineTest(unittest.TestCase):
    def test_gzip_and_xz_import_real_decode_with_fake_mysql(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);client=root/'client.cnf';client.write_text('[client]\n');client.chmod(0o600)
            sql=b'CREATE TABLE states(id int);\n';count=root/'count';captured=root/'captured'
            executable=root/'mysql'
            executable.write_text('#!'+sys.executable+'\nimport pathlib,sys\np=pathlib.Path('+repr(str(count))+')\nif "-e" in sys.argv:\n print(1 if p.exists() else 0)\nelse:\n pathlib.Path('+repr(str(captured))+').write_bytes(sys.stdin.buffer.read());p.write_text("done")\n')
            executable.chmod(0o700)
            for name,data in [('recorder.sql.gz',gzip.compress(sql)),('recorder-'+ 'a'*64+'.sql.xz',lzma.compress(sql,preset=0))]:
                count.unlink(missing_ok=True);dump=root/name;dump.write_bytes(data)
                result=subprocess.run(['bash',str(ROOT/'native_backup/import_database.sh'),str(client),str(dump),'fixture','1'],env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH']),capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(captured.read_bytes(),sql)
