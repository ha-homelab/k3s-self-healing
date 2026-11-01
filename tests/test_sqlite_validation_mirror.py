"""Validate closed SQLite snapshots locally without changing source semantics."""
from contextlib import closing
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from disaster_recovery import archive, local_capture as local, runner


class SQLiteMirrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source, self.output, self.scratch = [self.base / name for name in ('source', 'output', 'scratch')]
        for path in (self.source, self.output, self.scratch):
            path.mkdir(mode=0o700)
        self.destination = self.output / 'state.tar.gz'
        self.options = dict(directory=str(self.scratch), max_bytes=8 * 1024**2, minimum_free_bytes=1024)
        self.database = self.source / 'state.db'
        self.writer = sqlite3.connect(self.database, timeout=0)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE revision(value)')
        self.writer.execute('INSERT INTO revision VALUES (0)')
        self.writer.execute('CREATE TABLE payload(value)')
        self.writer.executemany('INSERT INTO payload VALUES (zeroblob(1024))', [()] * 2200)
        self.writer.commit()
        self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def capture(self, **kwargs):
        return archive.create_archive(self.source, self.destination,
                                      sqlite_validation_mirror=self.options, **kwargs)

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def instrument(self, *, normalization_error=None, mirror_error=None, commits=False):
        connect = sqlite3.connect
        owner = self
        state = {'commits': 0, 'sources': [], 'mirror_checks': 0, 'normalized': False, 'closed': False}

        class Source(sqlite3.Connection):
            def backup(self, output, **kwargs):
                progress = kwargs['progress']
                def step(status, remaining, total):
                    if commits and status == sqlite3.SQLITE_OK:
                        owner.writer.execute('UPDATE revision SET value=value+1')
                        owner.writer.commit()
                        state['commits'] += 1
                    progress(status, remaining, total)
                return super().backup(output, **dict(kwargs, progress=step))

        class Output(sqlite3.Connection):
            def execute(self, sql, *args):
                owner.assertNotEqual(sql, 'PRAGMA quick_check', 'NAS output must not run quick_check')
                if sql == 'PRAGMA journal_mode=DELETE':
                    owner.assertTrue(all(not source.in_transaction for source in state['sources']))
                    owner.assertEqual(owner.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
                    if normalization_error:
                        raise normalization_error
                    state['normalized'] = True
                return super().execute(sql, *args)

            def close(self):
                super().close()
                state['closed'] = True

        class Mirror(sqlite3.Connection):
            def execute(self, sql, *args):
                owner.assertEqual(sql, 'PRAGMA quick_check')
                owner.assertTrue(state['normalized'] and state['closed'])
                owner.assertEqual(owner.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
                state['mirror_checks'] += 1
                if mirror_error:
                    raise mirror_error
                return super().execute(sql, *args)

        def tracked(path, *args, **kwargs):
            if str(path).startswith(self.source.as_uri() + '/'):
                connection = connect(path, *args, **dict(kwargs, factory=Source))
                state['sources'].append(connection)
                return connection
            if str(path).startswith(self.scratch.as_uri() + '/'):
                self.assertTrue(str(path).endswith('?mode=ro'))
                return connect(path, *args, **dict(kwargs, factory=Mirror))
            self.assertTrue(Path(path).is_relative_to(self.output))
            return connect(path, *args, **dict(kwargs, factory=Output))
        return patch.object(archive.sqlite3, 'connect', side_effect=tracked), state

    def test_pinned_revision_local_check_and_exact_validated_payload(self):
        tracking, state = self.instrument(commits=True)
        captured = []
        original = archive._SQLiteMirror.capture
        def verified(mirror, *args):
            original(mirror, *args)
            with mirror.file.open('rb') as handle:
                captured.append(hashlib.sha256(handle.read()).hexdigest())
            self.assertEqual(mirror.file.path.stat().st_mode & 0o777, 0o600)
        with tracking, patch.object(archive._SQLiteMirror, 'capture', verified):
            result = self.capture(wal_snapshot={'minimum_free_bytes': 1})
        self.assertGreater(state['commits'], 1)
        self.assertEqual(state['mirror_checks'], 1)
        self.assertEqual(result['sqlite_validation_mirrors'], ['state.db'])
        self.assertEqual(result['sqlite_pinned_wal_snapshots'], ['state.db'])
        with tarfile.open(self.destination) as tar:
            payload = tar.extractfile('state.db').read()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), captured[0])
        restored = self.base / 'restored.db'
        restored.write_bytes(payload)
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM revision').fetchone(), (0,))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_output_and_mirror_errors_never_select_source_fallback(self):
        for where in ('normalization_error', 'mirror_error'):
            for code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_BUSY):
                error = sqlite3.OperationalError('injected private-output error')
                error.sqlite_errorcode = code
                tracking, _state = self.instrument(**{where: error})
                with self.subTest(where=where, code=code), tracking, \
                     patch.object(archive, '_snapshot_stable_wal') as busy, \
                     patch.object(archive, '_snapshot_quiescent_sqlite') as quiescent, \
                     self.assertRaises(archive.ArchiveError):
                    self.capture()
                busy.assert_not_called()
                quiescent.assert_not_called()
                self.assert_clean()

    def test_mirror_corruption_and_post_validation_changes_fail_closed(self):
        original = archive._SQLiteMirror.capture
        connect = sqlite3.connect
        def corrupt_at_open(path, *args, **kwargs):
            if str(path).startswith(self.scratch.as_uri() + '/'):
                next(self.scratch.glob('*/database.sqlite')).write_bytes(b'not a database')
            return connect(path, *args, **kwargs)
        with patch.object(archive.sqlite3, 'connect', side_effect=corrupt_at_open), self.assertRaises(archive.ArchiveError):
            self.capture()
        self.assert_clean()
        def mutate_verified(mirror, *args):
            original(mirror, *args)
            with mirror.file.path.open('ab') as output:
                output.write(b'changed')
        with patch.object(archive._SQLiteMirror, 'capture', mutate_verified), self.assertRaises(archive.ArchiveError):
            self.capture()
        self.assert_clean()

    def test_cap_floor_and_io_failure_clean_both_locations(self):
        self.options['max_bytes'] = 4096
        with self.assertRaisesRegex(OSError, 'byte limit'):
            self.capture()
        self.assert_clean()
        self.options['max_bytes'] = 8 * 1024**2
        with patch.object(local.os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)), \
             self.assertRaisesRegex(OSError, 'free space'):
            self.capture()
        self.assert_clean()
        write = local.BoundedWriter.write
        calls = []
        def full_after_first(writer, value):
            calls.append(len(value))
            if len(calls) > 1:
                raise OSError(errno.ENOSPC, 'injected full device')
            return write(writer, value)
        with patch.object(local.BoundedWriter, 'write', full_after_first), self.assertRaises(OSError):
            self.capture()
        self.assertGreater(len(calls), 1)
        self.assert_clean()

    def test_deadline_during_mirror_copy_cleans_up(self):
        write = local.BoundedWriter.write
        now = [0.0]
        def expire_after_write(writer, value):
            result = write(writer, value)
            now[0] = 20.0
            return result
        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(local.BoundedWriter, 'write', expire_after_write), self.assertRaises(TimeoutError):
            self.capture(timeout_seconds=10)
        self.assert_clean()

    def test_deadline_during_local_quick_check_and_cancellation_clean_up(self):
        connect = sqlite3.connect
        now = [0.0]
        class Expiring(sqlite3.Connection):
            def set_progress_handler(self, callback, steps):
                def expire():
                    now[0] = 20.0
                    return callback()
                super().set_progress_handler(expire, steps)
        def tracked(path, *args, **kwargs):
            if str(path).startswith(self.scratch.as_uri() + '/'):
                kwargs['factory'] = Expiring
            return connect(path, *args, **kwargs)
        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(archive.sqlite3, 'connect', side_effect=tracked), \
             self.assertRaisesRegex(archive.ArchiveError, 'mirror validation failed'):
            self.capture(timeout_seconds=10)
        self.assert_clean()
        with patch.object(local.BoundedWriter, 'write', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.capture()
        self.assert_clean()

    def test_midcopy_free_floor_and_during_tar_mutation_are_rejected(self):
        calls = []
        def capacity(_fd):
            calls.append(1)
            return SimpleNamespace(f_bavail=10**9 if len(calls) < 4 else 0, f_frsize=1)
        with patch.object(local.os, 'fstatvfs', side_effect=capacity), self.assertRaisesRegex(OSError, 'floor'):
            self.capture()
        self.assertGreaterEqual(len(calls), 4)
        self.assert_clean()
        addfile = tarfile.TarFile.addfile
        def mutate(tar, info, fileobj=None):
            if info.name == 'state.db':
                path = next(self.scratch.glob('*/database.sqlite'))
                with path.open('r+b') as handle:
                    handle.seek(-1, 2)
                    value = handle.read(1)
                    handle.seek(-1, 2)
                    handle.write(bytes([value[0] ^ 1]))
            return addfile(tar, info, fileobj)
        with patch.object(tarfile.TarFile, 'addfile', mutate), self.assertRaisesRegex(archive.ArchiveError, 'during archiving'):
            self.capture()
        self.assert_clean()

    def test_standalone_check_rejects_sidecar_symlinks_and_directories(self):
        target = self.output / 'closed.db'
        target.write_bytes(b'closed')
        sidecar = self.output / 'closed.db-wal'
        sidecar.symlink_to(self.base / 'missing')
        with self.assertRaises(archive.ArchiveError):
            archive._require_standalone_sqlite(target, 'closed.db')
        sidecar.unlink()
        sidecar.mkdir()
        with self.assertRaises(archive.ArchiveError):
            archive._require_standalone_sqlite(target, 'closed.db')

    def test_local_open_failure_cleans_known_sqlite_sidecars(self):
        connect = sqlite3.connect
        def fail_local(path, *args, **kwargs):
            if str(path).startswith(self.scratch.as_uri() + '/'):
                mirror = next(self.scratch.glob('*/database.sqlite'))
                for suffix in ('-wal', '-shm', '-journal'):
                    Path(str(mirror) + suffix).write_bytes(b'partial local SQLite sidecar')
                error = sqlite3.OperationalError('injected local open failure')
                error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
                raise error
            return connect(path, *args, **kwargs)
        with patch.object(archive.sqlite3, 'connect', side_effect=fail_local), self.assertRaises(archive.ArchiveError):
            self.capture()
        self.assert_clean()

    def test_wal_header_in_mirror_rejected_before_sqlite_open(self):
        write = local.BoundedWriter.write
        def wrong_header(writer, value):
            if writer.written == 0:
                value = bytearray(value)
                value[18:20] = b'\x02\x02'
            return write(writer, value)
        tracking, state = self.instrument()
        with tracking, patch.object(local.BoundedWriter, 'write', wrong_header), \
             self.assertRaisesRegex(archive.ArchiveError, 'not standalone'):
            self.capture()
        self.assertEqual(state['mirror_checks'], 0)
        self.assert_clean()

    def test_multiple_databases_reuse_one_cleaned_bounded_file(self):
        with closing(sqlite3.connect(self.source / 'another.db')) as connection:
            connection.execute('CREATE TABLE saved(value)')
        result = self.capture()
        self.assertEqual(result['sqlite_validation_mirrors'], ['another.db', 'state.db'])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_quiescent_fallback_still_uses_existing_guarded_path(self):
        self.writer.close()
        error = sqlite3.OperationalError('injected inactive WAL open failure')
        error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
        with patch.object(archive, '_snapshot_sqlite_online', side_effect=error):
            result = self.capture()
        self.assertEqual(result['sqlite_quiescent_copies'], ['state.db'])
        self.assertNotIn('sqlite_validation_mirrors', result)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_symlink_and_source_overlap_are_rejected(self):
        link = self.base / 'link'
        link.symlink_to(self.scratch, target_is_directory=True)
        for directory in (link, self.source, self.output):
            self.options['directory'] = str(directory)
            with self.subTest(directory=directory), self.assertRaises((ValueError, OSError)):
                self.capture()
            self.assert_clean()

    def test_invalid_options_and_combination_rejected_before_effects(self):
        bad = [True, {}, dict(self.options, extra=1), dict(self.options, directory='//mirror')]
        bad += [dict(self.options, **{key: value}) for key in ('max_bytes', 'minimum_free_bytes')
                for value in (True, 0, -1, 1.2, '1', 2**63)]
        for value in bad:
            for section in ('claims', 'host_sources'):
                config = {section: [{'sqlite_validation_mirror': value}]}
                with self.subTest(value=value, section=section), patch.object(runner, 'Kubernetes') as api, \
                     patch.object(runner.os, 'umask') as umask, self.assertRaises(ValueError):
                    runner.run(config, self.base / 'new-output')
                api.assert_not_called()
                umask.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'cannot share a component'):
            self.capture(staged_capture=True, local_capture=self.options)
        self.assertFalse((self.base / 'new-output').exists())
        self.assert_clean()

    def test_runner_forwards_only_explicit_option(self):
        for option in ({}, {'sqlite_validation_mirror': self.options}):
            with patch.object(runner, 'create_archive', return_value={}) as create:
                runner.archive_component(self.source, self.output, 'state', {}, **option)
            self.assertEqual(create.call_args.kwargs.get('sqlite_validation_mirror'), option.get('sqlite_validation_mirror'))
            if not option:
                self.assertNotIn('sqlite_validation_mirror', create.call_args.kwargs)


class SQLiteMirrorRenderTests(unittest.TestCase):
    def objects(self):
        options = dict(directory='/capture', max_bytes=3 * local.GIB, minimum_free_bytes=5 * local.GIB)
        mirror = dict(directory='/sqlite-mirror', max_bytes=4 * local.GIB, minimum_free_bytes=5 * local.GIB)
        config = {'claims': [{'staged_capture': True, 'local_capture': options}],
                  'host_sources': [{'sqlite_validation_mirror': mirror}]}
        cron = yaml.safe_load((Path(__file__).resolve().parents[1] / 'disaster_recovery/deploy/cronjob.yaml').read_text())
        return [{'kind': 'ConfigMap', 'data': {'config.json': json.dumps(config)}}, cron]

    def test_separate_disks_and_peak_serial_budget_are_explicit(self):
        objects = self.objects()
        local.configure_local_capture(objects)
        pod = objects[1]['spec']['jobTemplate']['spec']['template']['spec']
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertEqual(volumes['capture-scratch']['emptyDir'], {'sizeLimit': str(4 * local.GIB)})
        self.assertEqual(volumes['sqlite-validation-scratch']['emptyDir'], {'sizeLimit': str(5 * local.GIB)})
        resources = pod['containers'][0]['resources']
        self.assertEqual(resources['requests']['ephemeral-storage'], str(4 * local.GIB))
        self.assertEqual(resources['limits']['ephemeral-storage'], str(5 * local.GIB))
        before = copy.deepcopy(objects)
        local.configure_local_capture(objects)
        self.assertEqual(objects, before)

    def test_combination_or_aliased_mount_rejected(self):
        for same_component in (False, True):
            objects = self.objects()
            config = json.loads(objects[0]['data']['config.json'])
            if same_component:
                config['claims'][0].update(config['host_sources'][0])
            else:
                config['host_sources'][0]['sqlite_validation_mirror']['directory'] = '/capture/child'
            objects[0]['data']['config.json'] = json.dumps(config)
            with self.assertRaises(ValueError):
                local.configure_local_capture(objects)


if __name__ == '__main__':
    unittest.main()
