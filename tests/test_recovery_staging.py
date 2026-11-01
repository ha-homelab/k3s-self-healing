"""Exercise opt-in capture staging with synthetic files and fault injection."""

from contextlib import closing
import errno
import gzip
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from disaster_recovery import archive, runner


INVALID_FLAGS = (None, 0, 1, 0.0, 'true', 'false', [], {})


class StagedCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'
        self.output = self.base / 'output'
        self.source.mkdir()
        self.output.mkdir()
        self.destination = self.output / 'component.tar.gz'
        (self.source / 'state.json').write_bytes(b'old state')

    def capture(self, **options):
        return archive.create_archive(self.source, self.destination, staged_capture=True, **options)

    def assert_no_output(self):
        self.assertEqual(list(self.output.iterdir()), [])

    def test_invalid_flag_fails_before_archive_or_runner_side_effects(self):
        for value in INVALID_FLAGS:
            with self.subTest(value=value), patch.object(archive.tempfile, 'TemporaryDirectory') as scratch:
                with self.assertRaisesRegex(ValueError, 'staged_capture'):
                    archive.create_archive(self.base / 'absent', self.destination, staged_capture=value)
                scratch.assert_not_called()
                self.assert_no_output()
            with self.subTest(value=value), patch.object(runner, 'create_archive') as create:
                with self.assertRaisesRegex(ValueError, 'staged_capture'):
                    runner.archive_component(self.source, self.output, 'component', {}, staged_capture=value)
                create.assert_not_called()
            for section in ('claims', 'host_sources'):
                config = {'claims': [{'name': 'first-claim'}], 'host_sources': [{'name': 'first-host'}]}
                config[section].append({'name': 'last-entry', 'optional': True, 'staged_capture': value})
                root = self.base / 'not-created'
                with self.subTest(value=value, section=section), \
                     patch.object(runner, 'Kubernetes') as api, patch.object(runner.os, 'umask') as umask:
                    with self.assertRaisesRegex(ValueError, 'staged_capture'):
                        runner.run(config, root)
                    api.assert_not_called()
                    umask.assert_not_called()
                    self.assertFalse(root.exists())

    def test_helper_flag_is_independent_of_timeout(self):
        for enabled in (False, True):
            for seconds in (900, 3600):
                with self.subTest(enabled=enabled, seconds=seconds), \
                     patch.object(runner, 'create_archive', return_value={}) as create:
                    runner.archive_component(self.source, self.output, 'component', {},
                                             staged_capture=enabled, archive_timeout_seconds=seconds)
                    self.assertIs(create.call_args.kwargs['staged_capture'], enabled)
                    self.assertEqual(create.call_args.kwargs['timeout_seconds'], seconds)

    def test_direct_mode_does_not_create_raw_tar_or_timing_metadata(self):
        with patch.object(archive, '_compress_tar') as compress:
            result = archive.create_archive(self.source, self.destination)
        compress.assert_not_called()
        self.assertNotIn('staged_capture', result)
        self.assertNotIn('capture_seconds', result)
        self.assertNotIn('compression_seconds', result)
        self.assertEqual(list(self.output.iterdir()), [self.destination])

    def test_staged_memory_boundary_preserves_large_payloads_and_sqlite_path(self):
        self.assertEqual(archive.STAGED_MEMORY_FILE_LIMIT, 64 * 1024 * 1024)
        sizes = {'above-direct': archive.MEMORY_FILE_LIMIT + 1,
                 'at-staged': archive.STAGED_MEMORY_FILE_LIMIT,
                 'above-staged': archive.STAGED_MEMORY_FILE_LIMIT + 1}
        for name, size in sizes.items():
            with (self.source / name).open('wb') as output:
                output.write(b'first')
                output.seek(size - 4)
                output.write(b'last')
        with closing(sqlite3.connect(self.source / 'small.db')) as database, database:
            database.execute('CREATE TABLE values_to_restore (value)')
            database.execute("INSERT INTO values_to_restore VALUES ('retained')")
        original = archive._copy_regular
        observed, buffers = {}, []

        def capture(*args, **options):
            result = original(*args, **options)
            _metadata, is_sqlite, memory = result
            observed[args[1]] = (is_sqlite, isinstance(memory, io.BytesIO), args[2].exists())
            if memory is not None:
                buffers.append(memory)
            return result

        with patch.object(archive, '_copy_regular', side_effect=capture):
            result = self.capture()
        self.assertEqual(observed['above-direct'], (False, True, False))
        self.assertEqual(observed['at-staged'], (False, True, False))
        self.assertEqual(observed['above-staged'], (False, False, True))
        self.assertEqual(observed['small.db'], (True, False, False))
        self.assertTrue(all(buffer.closed for buffer in buffers))
        self.assertEqual(result['sqlite_databases'], ['small.db'])
        with tarfile.open(self.destination) as captured:
            for name, size in sizes.items():
                self.assertEqual(captured.getmember(name).size, size)
                with (self.source / name).open('rb') as original_file, captured.extractfile(name) as restored:
                    self.assertEqual(hashlib.file_digest(restored, 'sha256').digest(),
                                     hashlib.file_digest(original_file, 'sha256').digest())
            restored_database = self.base / 'restored.db'
            restored_database.write_bytes(captured.extractfile('small.db').read())
        with closing(sqlite3.connect(restored_database)) as database:
            self.assertEqual(database.execute('SELECT value FROM values_to_restore').fetchall(),
                             [('retained',)])

    def test_staged_growth_past_cap_retries_on_disk_and_rejects_changed_inventory(self):
        source = self.source / 'growing.bin'
        with source.open('wb') as output:
            output.truncate(archive.STAGED_MEMORY_FILE_LIMIT - 1)
        original_copy, constructor = archive._copy_stream, archive._MemoryFile
        buffers, changed = [], False

        def remember(*args, **options):
            buffer = constructor(*args, **options)
            buffers.append(buffer)
            return buffer

        def grow_once(input_file, output_file, deadline):
            nonlocal changed
            if not changed:
                changed = True
                with source.open('r+b') as updated:
                    updated.truncate(archive.STAGED_MEMORY_FILE_LIMIT + 1)
            try:
                return original_copy(input_file, output_file, deadline)
            finally:
                if isinstance(output_file, io.BytesIO):
                    self.assertLessEqual(output_file.tell(), archive.STAGED_MEMORY_FILE_LIMIT)

        with patch.object(archive, '_MemoryFile', side_effect=remember), \
             patch.object(archive, '_copy_stream', side_effect=grow_once):
            result = self.capture()
        self.assertEqual(result['attempts'], 2)
        self.assertTrue(all(buffer.closed for buffer in buffers))
        self.assertEqual(list(self.output.iterdir()), [self.destination])
        with tarfile.open(self.destination) as captured, source.open('rb') as original_file:
            with captured.extractfile('growing.bin') as restored:
                self.assertEqual(hashlib.file_digest(restored, 'sha256').digest(),
                                 hashlib.file_digest(original_file, 'sha256').digest())

    def test_staged_memory_exception_closes_payload_and_cleans_scratch(self):
        source = self.source / 'above-direct'
        with source.open('wb') as output:
            output.truncate(archive.MEMORY_FILE_LIMIT + 1)
        constructor, original_copy = archive._MemoryFile, archive._copy_stream
        for error in (TimeoutError('expired'), OSError(errno.EIO, 'read failed')):
            buffers = []

            def remember(*args, **options):
                buffer = constructor(*args, **options)
                buffers.append(buffer)
                return buffer

            def fail_after_copy(input_file, output_file, deadline):
                original_copy(input_file, output_file, deadline)
                raise error

            with self.subTest(error=type(error).__name__), \
                 patch.object(archive, '_MemoryFile', side_effect=remember), \
                 patch.object(archive, '_copy_stream', side_effect=fail_after_copy):
                with self.assertRaises(type(error)):
                    self.capture()
            self.assertEqual(len(buffers), 1)
            self.assertTrue(buffers[0].closed)
            self.assert_no_output()

    def test_staged_mutation_retains_three_file_retries_per_component_attempt(self):
        source = self.source / 'above-direct'
        with source.open('wb') as output:
            output.truncate(archive.MEMORY_FILE_LIMIT + 1)
        original_copy = archive._copy_stream
        calls = 0

        def mutate_after_copy(input_file, output_file, deadline):
            nonlocal calls
            copied = original_copy(input_file, output_file, deadline)
            calls += 1
            with source.open('ab') as updated:
                updated.write(b'x')
            return copied

        with patch.object(archive, '_copy_stream', side_effect=mutate_after_copy):
            with self.assertRaises(archive.SourceChangedError):
                self.capture()
        self.assertEqual(calls, 9)
        self.assert_no_output()

    def test_source_changes_during_compression_preserve_complete_captured_bytes(self):
        original = archive._compress_tar

        def change_source(captured, destination, deadline):
            self.assertEqual(captured.stat().st_mode & 0o777, 0o600)
            self.assertEqual(captured.parent.stat().st_mode & 0o777, 0o700)
            # Reading the complete tar here also proves its writer was closed.
            with tarfile.open(captured, 'r:') as raw:
                self.assertEqual(raw.extractfile('state.json').read(), b'old state')
            (self.source / 'state.json').write_bytes(b'new state after capture')
            (self.source / 'new.json').write_bytes(b'new member')
            original(captured, destination, deadline)

        with patch.object(archive, '_compress_tar', side_effect=change_source):
            result = self.capture()
        self.assertTrue(result['staged_capture'])
        self.assertEqual(result['attempts'], 1)
        self.assertGreaterEqual(result['capture_seconds'], 0)
        self.assertGreaterEqual(result['compression_seconds'], 0)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.output.iterdir()), [self.destination])
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), ['.', 'state.json'])
            self.assertEqual(captured.extractfile('state.json').read(), b'old state')

    def test_repeated_replacement_during_capture_rejects_all_attempts(self):
        (self.source / 'z-last').write_bytes(b'last')
        original = archive._copy_regular

        def replace_earlier(root_fd, relative, target, deadline, **options):
            result = original(root_fd, relative, target, deadline, **options)
            if relative == 'z-last':
                replacement = self.source / '.replacement'
                replacement.write_bytes(b'old state')
                replacement.replace(self.source / 'state.json')
            return result

        with patch.object(archive, '_copy_regular', side_effect=replace_earlier), \
             patch.object(archive, '_compress_tar') as compress, \
             patch.object(archive, '_create_archive_attempt', wraps=archive._create_archive_attempt) as attempts, \
             patch.object(archive.time, 'monotonic', return_value=100.0):
            with self.assertRaises(archive.SourceChangedError):
                self.capture(timeout_seconds=90)
        compress.assert_not_called()
        self.assertEqual(attempts.call_count, 3)
        self.assertEqual([call.args[3] for call in attempts.call_args_list], [190.0] * 3)
        self.assert_no_output()

    def test_retry_and_compression_keep_the_original_deadline(self):
        (self.source / 'z-last').write_bytes(b'last')
        original_copy, original_compress = archive._copy_regular, archive._compress_tar
        now = [100.0]
        changed = False

        def change_once(root_fd, relative, target, deadline, **options):
            nonlocal changed
            result = original_copy(root_fd, relative, target, deadline, **options)
            if relative == 'z-last' and not changed:
                changed = True
                (self.source / 'state.json').write_bytes(b'retried state')
                now[0] += 10
            return result

        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(archive, '_copy_regular', side_effect=change_once), \
             patch.object(archive, '_compress_tar', wraps=original_compress) as compress:
            result = self.capture(timeout_seconds=90)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(compress.call_args.args[2], 190.0)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile('state.json').read(), b'retried state')

    def test_capture_timeout_cleans_raw_tar(self):
        now = [0.0]
        original = archive._copy_regular

        def expire(*args, **options):
            result = original(*args, **options)
            now[0] = 20.0
            return result

        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(archive, '_copy_regular', side_effect=expire), \
             patch.object(archive, '_compress_tar') as compress:
            with self.assertRaises(TimeoutError):
                self.capture(timeout_seconds=10)
        compress.assert_not_called()
        self.assert_no_output()

    def test_compression_deadline_is_checked_between_bounded_chunks(self):
        (self.source / 'state.json').write_bytes(b'a' * (archive.COPY_CHUNK * 2))
        now = [0.0]
        original = gzip.GzipFile.write
        sizes = []

        def expire_after_write(compressed, data):
            sizes.append(len(data))
            result = original(compressed, data)
            now[0] = 20.0
            return result

        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(gzip.GzipFile, 'write', new=expire_after_write):
            with self.assertRaises(TimeoutError):
                self.capture(timeout_seconds=10)
        self.assertEqual(sizes, [archive.COPY_CHUNK])
        self.assert_no_output()

    def test_capture_and_compression_io_failures_never_publish(self):
        for target, error_code in (('tar', errno.ENOSPC), ('gzip', errno.ENOSPC), ('gzip', errno.EIO)):
            failure = OSError(error_code, 'synthetic destination failure')
            patched = patch.object(tarfile.TarFile, 'addfile', side_effect=failure) if target == 'tar' else \
                patch.object(gzip.GzipFile, 'write', side_effect=failure)
            with self.subTest(target=target, error_code=error_code), patched:
                with self.assertRaises(OSError) as raised:
                    self.capture()
                self.assertEqual(raised.exception.errno, error_code)
            self.assert_no_output()

    def test_corrupt_compression_fails_verification_and_cleans_scratch(self):
        original = archive._compress_tar

        def corrupt(captured, destination, deadline):
            original(captured, destination, deadline)
            data = bytearray(destination.read_bytes())
            data[-8] ^= 1
            destination.write_bytes(data)

        with patch.object(archive, '_compress_tar', side_effect=corrupt):
            with self.assertRaises(gzip.BadGzipFile):
                self.capture()
        self.assert_no_output()

    def test_sqlite_snapshot_modes_links_and_exclusions_are_preserved(self):
        (self.source / 'state.json').chmod(0o640)
        (self.source / 'state-link').symlink_to('state.json')
        (self.source / 'ignored.log').write_text('ignored')
        with closing(sqlite3.connect(self.source / 'state.db')) as live:
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('CREATE TABLE saved(value TEXT)')
            live.execute("INSERT INTO saved VALUES ('committed')")
            live.commit()
            result = self.capture(excludes=['ignored.log'])
        self.assertEqual(result['sqlite_databases'], ['state.db'])
        with tarfile.open(self.destination) as captured:
            self.assertEqual(set(captured.getnames()), {'.', 'state.json', 'state-link', 'state.db'})
            self.assertEqual(captured.getmember('state.json').mode, 0o640)
            self.assertEqual(captured.getmember('state-link').linkname, 'state.json')
            restored = self.base / 'restored.db'
            restored.write_bytes(captured.extractfile('state.db').read())
        with closing(sqlite3.connect(restored)) as database:
            self.assertEqual(database.execute('PRAGMA quick_check').fetchall(), [('ok',)])
            self.assertEqual(database.execute('SELECT value FROM saved').fetchall(), [('committed',)])

    def test_quiescent_sqlite_guard_still_covers_the_complete_capture(self):
        database = self.source / 'inactive.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE saved(value TEXT)')
            connection.commit()
        (self.source / 'z-last').write_bytes(b'last')
        original = archive._copy_regular

        def mutate_after_snapshot(root_fd, relative, target, deadline, **options):
            result = original(root_fd, relative, target, deadline, **options)
            if relative == 'z-last':
                current = database.stat()
                os.utime(database, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
            return result

        error = sqlite3.OperationalError('synthetic read-only WAL open failure')
        error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
        with patch.object(archive, '_snapshot_sqlite_online', side_effect=error), \
             patch.object(archive, '_copy_regular', side_effect=mutate_after_snapshot), \
             patch.object(archive, '_compress_tar') as compress:
            with self.assertRaises(archive.SourceChangedError):
                self.capture()
        compress.assert_not_called()
        self.assert_no_output()

    def test_exclusive_wal_fallback_preserves_latest_committed_state(self):
        database = self.source / 'exclusive.db'
        with closing(sqlite3.connect(database)) as live:
            live.execute('PRAGMA locking_mode=EXCLUSIVE')
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('PRAGMA wal_autocheckpoint=0')
            live.execute('CREATE TABLE saved(value TEXT)')
            live.commit()
            live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            live.execute("INSERT INTO saved VALUES ('latest committed state')")
            live.commit()
            before = {path.name: path.read_bytes() for path in self.source.iterdir()}
            with patch.object(archive, 'SQLITE_BUSY_WAIT_SECONDS', 0.01):
                result = self.capture(timeout_seconds=20)
            self.assertEqual(before, {path.name: path.read_bytes() for path in self.source.iterdir()})
        self.assertEqual(result['sqlite_stable_wal_copies'], ['exclusive.db'])
        with tarfile.open(self.destination) as captured:
            self.assertEqual(set(captured.getnames()), {'.', 'state.json', 'exclusive.db'})
            restored = self.base / 'restored.db'
            restored.write_bytes(captured.extractfile('exclusive.db').read())
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchall(), [('ok',)])
            self.assertEqual(connection.execute('SELECT value FROM saved').fetchall(),
                             [('latest committed state',)])


if __name__ == '__main__':
    unittest.main()
