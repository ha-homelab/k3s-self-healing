"""Exercise read-only WAL snapshots with real commits between backup steps."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disaster_recovery import archive, runner


class WalSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source, self.output = self.base / 'source', self.base / 'output'
        self.source.mkdir()
        self.output.mkdir()
        self.database = self.source / 'state.db'
        self.destination = self.output / 'state.tar.gz'
        self.options = {'minimum_free_bytes': 1}
        self.writer = sqlite3.connect(self.database, timeout=0)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute('CREATE TABLE revision(value)')
        self.writer.execute('INSERT INTO revision VALUES (0)')
        self.writer.execute('CREATE TABLE payload(data)')
        self.writer.executemany('INSERT INTO payload VALUES (zeroblob(1024))', [()] * 2200)
        self.writer.commit()
        self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def instrument(self, *, failure=None, mutate=True, control_limit=0,
                   mode_change=False, schema_error=False):
        connect = sqlite3.connect
        owner = self
        state = {'commits': 0, 'samples': [], 'rollback_checkpoints': [],
                 'validation_checkpoints': [], 'source_sql': [], 'sources': []}

        class Source(sqlite3.Connection):
            def execute(self, sql, *args):
                cursor = super().execute(sql, *args)
                if schema_error and sql == 'SELECT rootpage FROM sqlite_schema LIMIT 1':
                    cursor.close()
                    raise OSError('injected schema read failure after BEGIN')
                if mode_change and sql == 'PRAGMA journal_mode' and self.in_transaction:
                    cursor.close()
                    return SimpleNamespace(fetchone=lambda: ('delete',))
                return cursor

            def backup(self, target, **kwargs):
                if mode_change:
                    owner.assertFalse(self.in_transaction)
                progress = kwargs['progress']
                def step(status, remaining, total):
                    state['samples'].append((status, remaining, total))
                    if status == sqlite3.SQLITE_OK and mutate:
                        owner.writer.execute('UPDATE revision SET value=value+1')
                        owner.writer.commit()
                        state['commits'] += 1
                    progress(status, remaining, total)
                    if failure is not None and status == sqlite3.SQLITE_OK:
                        raise failure
                    if control_limit and len(state['samples']) >= control_limit:
                        raise TimeoutError('bounded control for automatic restarts')
                return super().backup(target, **dict(kwargs, progress=step))

            def rollback(self):
                super().rollback()
                state['rollback_checkpoints'].append(
                    owner.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())

        class Output(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql in ('PRAGMA quick_check', 'PRAGMA journal_mode=DELETE'):
                    state['validation_checkpoints'].append(
                        owner.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
                    owner.assertTrue(all(not source.in_transaction for source in state['sources']))
                return super().execute(sql, *args)

        def tracked(*args, **kwargs):
            if kwargs.get('uri'):
                connection = connect(*args, **dict(kwargs, factory=Source))
                connection.set_trace_callback(state['source_sql'].append)
                state['sources'].append(connection)
                return connection
            return connect(*args, **dict(kwargs, factory=Output))
        return patch.object(archive.sqlite3, 'connect', side_effect=tracked), state

    def test_pinned_wal_keeps_one_revision_while_writer_commits(self):
        tracking, state = self.instrument()
        with tracking:
            result = archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
        self.assertGreater(state['commits'], 1)
        self.assertEqual(result['sqlite_pinned_wal_snapshots'], ['state.db'])
        self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
        self.assertEqual(state['validation_checkpoints'], [(0, 0, 0), (0, 0, 0)])
        remaining = [row[1] for row in state['samples']]
        self.assertEqual(remaining, sorted(remaining, reverse=True))
        with tarfile.open(self.destination) as capture:
            restored = self.base / 'restored.db'
            restored.write_bytes(capture.extractfile('state.db').read())
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchall(), [('ok',)])
            self.assertEqual(connection.execute('SELECT value FROM revision').fetchone(), (0,))
            self.assertEqual(connection.execute('SELECT count(*) FROM payload').fetchone(), (2200,))
        self.assertEqual(self.writer.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertFalse(any('journal_mode=' in sql.lower() for sql in state['source_sql']))
        statements = state['source_sql']
        first_mode = statements.index('PRAGMA journal_mode')
        begin = statements.index('BEGIN')
        read = statements.index('SELECT rootpage FROM sqlite_schema LIMIT 1')
        second_mode = statements.index('PRAGMA journal_mode', first_mode + 1)
        self.assertLess(first_mode, begin)
        self.assertLess(begin, read)
        self.assertLess(read, second_mode)

    def test_changed_mode_releases_transaction_before_legacy_backup(self):
        tracking, state = self.instrument(mutate=False, mode_change=True)
        with tracking:
            result = archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
        self.assertNotIn('sqlite_pinned_wal_snapshots', result)
        self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
        self.assertEqual(state['validation_checkpoints'], [(0, 0, 0), (0, 0, 0)])

    def test_schema_read_failure_after_begin_releases_transaction(self):
        tracking, state = self.instrument(schema_error=True)
        with tracking, self.assertRaisesRegex(OSError, 'schema read failure'):
            archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
        self.assertEqual(state['samples'], [])
        self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_default_keeps_existing_restart_behavior_without_pinning(self):
        tracking, state = self.instrument(control_limit=12)
        with tracking, self.assertRaises(TimeoutError):
            archive.create_archive(self.source, self.destination)
        self.assertGreater(state['commits'], 1)
        self.assertNotIn('BEGIN', state['source_sql'])
        self.assertNotIn('PRAGMA journal_mode', state['source_sql'])
        self.assertEqual(state['rollback_checkpoints'], [])
        self.assertGreater(state['samples'][-1][1], 0)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_delete_journal_is_not_pinned_or_subject_to_wal_floor(self):
        self.writer.execute('PRAGMA journal_mode=DELETE')
        tracking, state = self.instrument(mutate=False)
        with tracking, patch.object(archive, '_check_wal_space') as floor:
            result = archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
        floor.assert_not_called()
        self.assertNotIn('BEGIN', state['source_sql'])
        self.assertEqual(state['rollback_checkpoints'], [])
        self.assertNotIn('sqlite_pinned_wal_snapshots', result)
        self.assertEqual(self.writer.execute('PRAGMA journal_mode').fetchone(), ('delete',))

    def test_failure_and_timeout_release_before_source_close_and_clean_output(self):
        for failure in (TimeoutError('expired'), OSError('injected I/O failure')):
            tracking, state = self.instrument(failure=failure)
            with self.subTest(error=type(failure).__name__), tracking, self.assertRaises(type(failure)):
                archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
            self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
            self.assertEqual(state['validation_checkpoints'], [])
            self.assertEqual(list(self.output.iterdir()), [])

    def test_source_floor_uses_held_directory_and_releases_after_midcopy_drop(self):
        original = archive.os.fstatvfs
        for drop_after in (0, 1):
            tracking, state = self.instrument()
            calls = []
            def capacity(fd):
                self.assertEqual(os.fstat(fd).st_ino, self.source.stat().st_ino)
                calls.append(fd)
                original(fd)  # descriptor remains usable; no pathname lookup is needed.
                return SimpleNamespace(f_bavail=1 if len(calls) <= drop_after else 0, f_frsize=4096)
            with self.subTest(drop_after=drop_after), tracking, \
                 patch.object(archive.os, 'fstatvfs', side_effect=capacity), \
                 self.assertRaisesRegex(OSError, 'free-space floor'):
                archive.create_archive(self.source, self.destination, wal_snapshot=self.options)
            if drop_after:
                self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
            else:
                self.assertEqual(state['samples'], [])
                self.assertNotIn('BEGIN', state['source_sql'])
            self.assertEqual(list(self.output.iterdir()), [])

    def test_shared_deadline_still_aborts_pinned_copy_and_releases_reader(self):
        tracking, state = self.instrument()
        original = archive._check_wal_space
        now = [0.0]
        calls = 0
        def expire(fd, floor):
            nonlocal calls
            original(fd, floor)
            calls += 1
            if calls > 1:
                now[0] = 20.0
        with tracking, patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(archive, '_check_wal_space', side_effect=expire), \
             self.assertRaises(TimeoutError):
            archive.create_archive(self.source, self.destination, timeout_seconds=10, wal_snapshot=self.options)
        self.assertEqual(state['rollback_checkpoints'], [(0, 0, 0)])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_pin_reports_only_actual_wal_snapshots_in_both_capture_modes(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                result = archive.create_archive(self.source, self.destination, staged_capture=staged,
                                                wal_snapshot=self.options)
                self.assertEqual(result['sqlite_pinned_wal_snapshots'], ['state.db'])
                self.destination.unlink()


class WalValidationTests(unittest.TestCase):
    def test_bad_options_fail_before_filesystem_cluster_or_checkpoint(self):
        invalid = [True, 1, '', [], {}, {'other': 1}]
        invalid += [{'minimum_free_bytes': value} for value in (True, False, 0, -1, 1.0, '1', None, 2 ** 63)]
        invalid.append({'minimum_free_bytes': 1, 'other': 2})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'new'
            for option in invalid:
                with self.subTest(option=option), patch.object(archive, '_create_archive_attempt') as create:
                    with self.assertRaises(ValueError):
                        archive.create_archive(root / 'source', root / 'output', wal_snapshot=option)
                    create.assert_not_called()
                for section in ('claims', 'host_sources'):
                    config = {section: [{'optional': True, 'wal_snapshot': option}]}
                    with patch.object(runner, 'Kubernetes') as api, patch.object(runner.os, 'umask') as umask:
                        with self.assertRaises(ValueError):
                            runner.run(config, root)
                        api.assert_not_called()
                        umask.assert_not_called()
                        self.assertFalse(root.exists())

    def test_forwarding_and_default_do_not_change_other_archive_options(self):
        for option in (None, {'minimum_free_bytes': 4096}):
            with patch.object(runner, 'create_archive', return_value={}) as create:
                runner.archive_component(Path('/source'), Path('/output'), 'fixture', {}, wal_snapshot=option)
            self.assertEqual(create.call_args.kwargs.get('wal_snapshot'), option)
            self.assertEqual(create.call_args.kwargs['timeout_seconds'], 900)
            self.assertIs(create.call_args.kwargs['staged_capture'], False)

    def test_direct_helper_requires_directory_descriptor_before_opening_database(self):
        with patch.object(archive.sqlite3, 'connect') as connect:
            with self.assertRaisesRegex(ValueError, 'held source-directory'):
                archive._snapshot_sqlite_online(Path('/source'), 'state.db', Path('/output'), 100,
                                                wal_snapshot={'minimum_free_bytes': 1})
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
