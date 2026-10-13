"""Run the actual dump pipeline against local fixtures, never a database or HA."""
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('bash') and shutil.which('xz'), 'bash and xz required')
class ExportPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.bin = self.root / 'bin'
        self.config = self.root / 'config'
        for directory in (self.work, self.bin, self.config / 'backups/recorder-export-staging',
                          self.config / 'recorder_backup'):
            directory.mkdir(parents=True, exist_ok=True)
        (self.work / 'database').write_text('fixture')
        (self.work / 'limit-blocks').write_text('1024')
        self.previous = self.config / 'recorder_backup/recorder.sql.gz'
        self.previous.write_bytes(b'previous verified export')
        self.pending = self.config / 'backups/recorder-export-staging/recorder.sql.xz.pending'
        objects = list(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
        pod = objects[0]['spec']['jobTemplate']['spec']['template']['spec']
        container = next(c for c in pod['initContainers'] if c['name'] == 'mysql-dump')
        self.command = container['command'][2].replace('/ha-config', str(self.config)).replace('/work', str(self.work))
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def run_export(self, program):
        executable = self.bin / 'mysqldump'
        executable.write_text('#!' + sys.executable + '\n' + program)
        executable.chmod(0o700)
        result = subprocess.run(['bash', '-ec', self.command], env=self.environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(self.previous.read_bytes(), b'previous verified export')
        return result

    def test_success_keeps_valid_pending_dump_for_publication(self):
        result = self.run_export("print('INSERT INTO states VALUES (1);' * 1000)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Transaction-consistent recorder export finished.', result.stdout)
        with lzma.open(self.pending, 'rt') as stream:
            self.assertEqual(stream.read(), 'INSERT INTO states VALUES (1);' * 1000 + '\n')

    def test_database_failure_removes_partial_but_preserves_verified_dump(self):
        result = self.run_export("print('incomplete export'); raise SystemExit(42)\n")
        self.assertEqual(result.returncode, 42)
        self.assertFalse(self.pending.exists())
        self.assertIn('previous verified export retained', result.stderr)

    def test_size_limit_failure_removes_truncated_xz(self):
        (self.work / 'limit-blocks').write_text('1')
        result = self.run_export('import os; os.write(1, os.urandom(65536))\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pending.exists())
        self.assertIn('incomplete output removed', result.stderr)

    def test_validation_failure_removes_pending_dump(self):
        xz_path = shutil.which('xz')
        executable = self.bin / 'xz'
        executable.write_text('#!/bin/sh\nif [ "$1" = "-t" ]; then exit 43; fi\n'
                              + 'exec "' + xz_path + '" "$@"\n')
        executable.chmod(0o700)
        result = self.run_export("print('database export')\n")
        self.assertEqual(result.returncode, 43)
        self.assertFalse(self.pending.exists())

    def test_strict_shell_validation_rejects_trailing_xz_garbage(self):
        xz_path = shutil.which('xz')
        executable = self.bin / 'xz'
        executable.write_text('#!/bin/sh\nif [ "$1" = "-t" ]; then exec "' + xz_path + '" "$@"; fi\n'
                              + '"' + xz_path + '" "$@" || exit $?\nprintf garbage\n')
        executable.chmod(0o700)
        result = self.run_export("print('INSERT INTO states VALUES (1);' * 1000)\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.pending.exists())
        self.assertIn('previous verified export retained', result.stderr)
