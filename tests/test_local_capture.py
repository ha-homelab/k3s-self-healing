"""Local raw capture is bounded, isolated and preserves archive guards."""
from contextlib import closing
import copy
import errno
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from disaster_recovery import archive, local_capture as local, runner


class LocalCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source, self.output, self.scratch = (self.base / name for name in ('source', 'output', 'scratch'))
        for path in (self.source, self.output, self.scratch):
            path.mkdir(mode=0o700)
        self.destination = self.output / 'component.tar.gz'
        self.options = dict(directory=str(self.scratch), max_bytes=1024 * 1024, minimum_free_bytes=1024)
        (self.source / 'state').write_bytes(b'original bytes')

    def capture(self, **kwargs):
        return archive.create_archive(self.source, self.destination, staged_capture=True,
                                      local_capture=self.options, **kwargs)

    def assert_clean(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_invalid_config_rejected_before_side_effects(self):
        invalid = [True, 'path', {}, dict(self.options, extra=1)]
        invalid += [dict(self.options, directory=value) for value in ('/', '//capture', '/tmp/../capture', 'relative',
                    '/tmp//capture', '/tmp/capture/', '/tmp/\ncapture')]
        invalid += [dict(self.options, **{key: value}) for key in ('max_bytes', 'minimum_free_bytes')
                    for value in (True, False, 0, -1, 1.5, '1024', None, 2 ** 63)]
        for value in invalid:
            with self.subTest(value=value), patch.object(archive, '_create_archive_attempt') as create:
                with self.assertRaises(ValueError):
                    archive.create_archive(self.source, self.destination, staged_capture=True, local_capture=value)
                create.assert_not_called()
            for section in ('claims', 'host_sources'):
                config = {section: [{'name': 'optional-last', 'optional': True,
                                     'staged_capture': True, 'local_capture': value}]}
                with patch.object(runner, 'Kubernetes') as api, patch.object(runner.os, 'umask') as umask:
                    with self.assertRaises(ValueError):
                        runner.run(config, self.base / 'new-backups')
                    api.assert_not_called()
                    umask.assert_not_called()
                    self.assertFalse((self.base / 'new-backups').exists())
        with self.assertRaisesRegex(ValueError, 'requires staged_capture'):
            archive.create_archive(self.source, self.destination, local_capture=self.options)
        self.assert_clean()

    def test_runner_forwards_only_explicit_local_option(self):
        for options in ({}, {'staged_capture': True, 'local_capture': self.options}):
            with patch.object(runner, 'create_archive', return_value={}) as create:
                runner.archive_component(self.source, self.output, 'component', {}, **options)
                self.assertEqual(create.call_args.kwargs.get('local_capture'), options.get('local_capture'))

    def test_local_raw_only_and_changes_after_capture_preserve_original_data(self):
        with closing(sqlite3.connect(self.source / 'database')) as database, database:
            database.execute('CREATE TABLE saved(value)')
            database.execute("INSERT INTO saved VALUES ('committed')")
        compress = archive._compress_tar
        locations = []
        original_copy = archive._copy_regular

        def copy_file(root, relative, target, deadline, **kwargs):
            locations.append(target.parent.parent)
            return original_copy(root, relative, target, deadline, **kwargs)

        def after_capture(raw, output, deadline):
            self.assertIsInstance(raw, local.LocalRaw)
            self.assertEqual(output.parent.parent, self.output)
            self.assertEqual((self.scratch / raw.child).stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.scratch / raw.child / 'archive.tar').stat().st_mode & 0o777, 0o600)
            with raw.open('rb') as handle, tarfile.open(fileobj=handle, mode='r:') as captured:
                self.assertEqual(captured.extractfile('state').read(), b'original bytes')
            (self.source / 'state').write_bytes(b'later bytes')
            compress(raw, output, deadline)

        with patch.object(archive, '_copy_regular', side_effect=copy_file), \
             patch.object(archive, '_compress_tar', side_effect=after_capture):
            result = self.capture()
        self.assertTrue(result['staged_capture'])
        self.assertEqual(result['attempts'], 1)
        self.assertTrue(all(path == self.output for path in locations))
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile('state').read(), b'original bytes')
            restored = self.base / 'restored.db'
            restored.write_bytes(captured.extractfile('database').read())
        with closing(sqlite3.connect(restored)) as database:
            self.assertEqual(database.execute('SELECT value FROM saved').fetchall(), [('committed',)])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_capture_mutation_retries_with_same_deadline_and_cleans_both_locations(self):
        original = archive._copy_regular

        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            with (self.source / 'state').open('ab') as output:
                output.write(b'x')
            return result

        with patch.object(archive, '_copy_regular', side_effect=change), \
             patch.object(archive, '_compress_tar') as compress, \
             patch.object(archive, '_create_archive_attempt', wraps=archive._create_archive_attempt) as attempts:
            with self.assertRaises(archive.SourceChangedError):
                self.capture()
        self.assertEqual(attempts.call_count, 3)
        self.assertEqual(len({call.args[3] for call in attempts.call_args_list}), 1)
        compress.assert_not_called()
        self.assert_clean()

    def test_quiescent_sqlite_is_guarded_until_local_capture_finishes(self):
        database = self.source / 'inactive.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE saved(value)')
            connection.commit()
        (self.source / 'z-last').write_bytes(b'last')
        original = archive._snapshot_sqlite

        def mutate_after_snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            previous = database.stat()
            os.utime(database, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000000))
            return result

        failure = sqlite3.OperationalError('synthetic read-only WAL open failure')
        failure.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
        with patch.object(archive, '_snapshot_sqlite_online', side_effect=failure), \
             patch.object(archive, '_snapshot_sqlite', side_effect=mutate_after_snapshot), \
             patch.object(archive, '_compress_tar') as compress:
            with self.assertRaises(archive.SourceChangedError):
                self.capture()
        compress.assert_not_called()
        self.assert_clean()

    def test_corrupt_compression_and_existing_output_cannot_be_published(self):
        compress = archive._compress_tar
        def corrupt(raw, output, deadline):
            compress(raw, output, deadline)
            data = bytearray(output.read_bytes())
            data[-8] ^= 1
            output.write_bytes(data)
        with patch.object(archive, '_compress_tar', side_effect=corrupt), self.assertRaises(OSError):
            self.capture()
        self.assert_clean()
        self.destination.write_bytes(b'existing backup')
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual(self.destination.read_bytes(), b'existing backup')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_cap_counts_tar_headers_padding_and_close(self):
        # Payload is tiny, but a complete tar occupies at least one 10 KiB record.
        for limit in (1, 1024, 10239):
            self.options['max_bytes'] = limit
            with self.subTest(limit=limit), self.assertRaisesRegex(OSError, 'byte limit'):
                self.capture()
            self.assert_clean()
        self.options['max_bytes'] = 10240
        self.capture()
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_initial_space_and_mid_capture_space_fail_closed(self):
        for values in ([1024], [2 ** 30, 1024], [2 ** 30, 2 ** 30, 0]):
            free = iter(values)
            last = values[-1]
            def statvfs(_fd):
                return SimpleNamespace(f_bavail=next(free, last), f_frsize=1)
            with self.subTest(values=values), patch.object(local.os, 'fstatvfs', side_effect=statvfs):
                with self.assertRaisesRegex(OSError, 'space|floor'):
                    self.capture()
            self.assert_clean()

    def test_errors_and_deadline_cleanup_without_fallback(self):
        for failure in (TimeoutError('expired'), OSError(errno.EIO, 'read error'),
                        OSError(errno.ENOSPC, 'full')):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(archive, '_compress_tar', side_effect=failure):
                with self.assertRaises(type(failure)):
                    self.capture()
            self.assert_clean()
        now = [0.0]
        original = local.BoundedWriter.write
        def expired(writer, data):
            result = original(writer, data)
            now[0] = 20.0
            return result
        with patch.object(archive.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(local.BoundedWriter, 'write', new=expired):
            with self.assertRaises(TimeoutError):
                self.capture(timeout_seconds=10)
        self.assert_clean()

    def test_root_must_exist_and_symlink_ancestors_are_refused(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.scratch, target_is_directory=True)
        for path in (self.base / 'missing', alias):
            self.options['directory'] = str(path)
            with self.subTest(path=path), self.assertRaises((OSError, archive.SourceChangedError)):
                self.capture()
        alias.unlink()
        alias.symlink_to(self.base, target_is_directory=True)
        self.options['directory'] = str(alias / 'scratch')
        with self.assertRaises(OSError):
            self.capture()
        self.assert_clean()

    def test_overlap_in_both_directions_and_mount_alias_are_refused(self):
        child = self.source / 'child'
        child.mkdir(mode=0o700)
        for path in (self.source, child, self.base, self.output):
            self.options['directory'] = str(path)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'overlap'):
                self.capture()
        self.options['directory'] = str(self.scratch)
        mounts = [('8:1', PurePosixPath('/same'), PurePosixPath(str(self.scratch)), 'ext4', '1'),
                  ('8:1', PurePosixPath('/same/nested'), PurePosixPath(str(self.source)), 'ext4', '2')]
        with patch.object(local, '_mounts', return_value=mounts), self.assertRaisesRegex(ValueError, 'aliases'):
            self.capture()
        self.assert_clean()

    def test_untrusted_writable_root_or_ancestor_never_chmodded(self):
        self.scratch.chmod(0o777)
        for mounts in ([], [('8:1', PurePosixPath('/ordinary'), PurePosixPath(str(self.scratch)), 'ext4', '1')]):
            with patch.object(local, '_mounts', return_value=mounts), patch.object(local.os, 'fchmod') as chmod:
                with self.assertRaisesRegex(ValueError, 'private directory|Cannot verify'):
                    self.capture()
                chmod.assert_not_called()
        self.scratch.chmod(0o700)
        self.base.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, 'untrusted'):
                self.capture()
        finally:
            self.base.chmod(0o700)
        self.assert_clean()

    def test_only_verified_disk_emptydir_root_can_be_made_private(self):
        mount = ('8:1', PurePosixPath('/pods/example/volumes/kubernetes.io~empty-dir/capture'),
                 PurePosixPath(str(self.scratch)), 'ext4', '31')
        self.scratch.chmod(0o755)
        for invalid in ([], [(*mount[:3], 'tmpfs', mount[4])],
                        [(mount[0], mount[1] / 'subdirectory', *mount[2:])]):
            with patch.object(local, '_mounts', return_value=invalid), patch.object(local.os, 'fchmod') as chmod:
                with self.assertRaises(ValueError):
                    self.capture()
                chmod.assert_not_called()
        with patch.object(local, '_mounts', return_value=[mount]), \
             patch.object(local.os, 'fchmod', wraps=os.fchmod) as chmod:
            self.capture()
        chmod.assert_called_once()
        self.assertEqual(chmod.call_args.args[1], 0o700)
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.base.stat().st_mode & 0o777, 0o700)

    def test_mount_substitution_fails_before_chmod(self):
        self.scratch.chmod(0o755)
        mount = ('8:1', PurePosixPath('/pods/example/volumes/kubernetes.io~empty-dir/capture'),
                 PurePosixPath(str(self.scratch)), 'ext4', '31')
        with patch.object(local, '_mounts', side_effect=[[mount], []]), patch.object(local.os, 'fchmod') as chmod:
            with self.assertRaisesRegex(OSError, 'mount identity'):
                self.capture()
            chmod.assert_not_called()
        self.assert_clean()

    def test_private_root_needs_no_mountinfo_but_nonprivate_does(self):
        with patch.object(local.sys, 'platform', 'darwin'), \
             patch.object(local, '_mounts', side_effect=ValueError('unavailable')):
            self.capture()
        self.destination.unlink()
        with patch.object(local.sys, 'platform', 'linux'), \
             patch.object(local, '_mounts', side_effect=ValueError('malformed')), \
             self.assertRaisesRegex(ValueError, 'Cannot verify'):
            self.capture()
        self.scratch.chmod(0o755)
        with patch.object(local, '_mounts', side_effect=ValueError('malformed')), \
             self.assertRaisesRegex(ValueError, 'Cannot verify'):
            self.capture()
        self.assert_clean()

    def test_private_root_still_rejects_known_memory_or_network_filesystem(self):
        for kind in ('tmpfs', 'ramfs', 'nfs', 'nfs4', 'cifs', 'fuse.sshfs'):
            mount = ('8:1', PurePosixPath('/'), PurePosixPath(str(self.scratch)), kind, '31')
            with self.subTest(kind=kind), patch.object(local, '_mounts', return_value=[mount]), \
                 self.assertRaisesRegex(ValueError, 'memory or network'):
                self.capture()
        self.assert_clean()

    def test_stacked_mounts_fail_closed_instead_of_selecting_hidden_backing(self):
        ordinary = ('8:1', PurePosixPath('/ordinary'), PurePosixPath(str(self.scratch)), 'ext4', '31')
        alias = ('8:1', PurePosixPath('/source/subdir'), PurePosixPath(str(self.scratch)), 'ext4', '32')
        source = ('8:1', PurePosixPath('/source'), PurePosixPath(str(self.source)), 'ext4', '33')
        for mounts in ([ordinary, alias, source], [alias, ordinary, source]):
            with patch.object(local, '_mounts', return_value=mounts), \
                 self.assertRaisesRegex(ValueError, 'Ambiguous'):
                self.capture()
        self.assert_clean()

    def test_wrong_owner_and_child_open_failure_leave_no_scratch(self):
        with patch.object(local.os, 'geteuid', return_value=os.geteuid() + 1), \
             self.assertRaisesRegex(ValueError, 'owned by|untrusted'):
            self.capture()
        original = os.open
        def fail_child(path, flags, *args, **kwargs):
            if str(path).startswith('.ha-dr-capture-'):
                raise OSError(errno.EIO, 'synthetic directory open failure')
            return original(path, flags, *args, **kwargs)
        with patch.object(local.os, 'open', side_effect=fail_child), self.assertRaises(OSError):
            self.capture()
        self.assert_clean()

    def test_replaced_raw_file_or_directory_is_not_followed(self):
        with local.local_raw_capture(self.options, self.source, self.destination, lambda: None) as raw:
            with raw.writer() as writer:
                writer.write(b'payload')
            path = self.scratch / raw.child / 'archive.tar'
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(OSError, 'file changed'):
                raw.open('rb')
        self.assert_clean()
        displaced = self.base / 'displaced'
        with self.assertRaisesRegex(OSError, 'mount changed'):
            with local.local_raw_capture(self.options, self.source, self.destination, lambda: None) as raw:
                with raw.writer() as writer:
                    writer.write(b'payload')
                self.scratch.rename(displaced)
                self.scratch.mkdir(mode=0o700)
                raw.open('rb')
        self.assertEqual(list(displaced.iterdir()), [])
        self.assert_clean()


class BoundedWriterTests(unittest.TestCase):
    def test_partial_writes_account_actual_bytes_and_never_cross_cap(self):
        output = io.BytesIO()
        class Partial:
            def write(self, data):
                return output.write(data[:3])
        enough = SimpleNamespace(f_bavail=10 ** 6, f_frsize=1)
        with patch.object(local.os, 'fstatvfs', return_value=enough):
            writer = local.BoundedWriter(Partial(), 123, 7, 2, lambda: None)
            self.assertEqual(writer.write(b'1234567'), 7)
            with self.assertRaisesRegex(OSError, 'byte limit'):
                writer.write(b'8')
        self.assertEqual(output.getvalue(), b'1234567')
        self.assertEqual(writer.written, 7)

    def test_zero_write_and_allocation_headroom_fail(self):
        enough = SimpleNamespace(f_bavail=10 ** 6, f_frsize=1)
        with patch.object(local.os, 'fstatvfs', return_value=enough):
            writer = local.BoundedWriter(Mock(write=Mock(return_value=0)), 123, 7, 2, lambda: None)
            with self.assertRaisesRegex(OSError, 'no progress'):
                writer.write(b'x')
        block = SimpleNamespace(f_bavail=1, f_frsize=4096)
        with patch.object(local.os, 'fstatvfs', return_value=block):
            writer = local.BoundedWriter(io.BytesIO(), 123, 7, 1, lambda: None)
            with self.assertRaisesRegex(OSError, 'floor'):
                writer.write(b'x')


class LocalRenderTests(unittest.TestCase):
    def objects(self, option=None):
        config = {'claims': [{'name': 'application'}], 'host_sources': []}
        if option is not None:
            config['claims'][0].update(staged_capture=True, local_capture=option)
        cron = yaml.safe_load((Path(__file__).resolve().parents[1] / 'disaster_recovery/deploy/cronjob.yaml').read_text())
        return [{'kind': 'ConfigMap', 'data': {'config.json': json.dumps(config)}}, cron]

    def test_default_serialized_objects_are_identical(self):
        objects = self.objects()
        before = yaml.safe_dump_all(objects, sort_keys=False)
        local.configure_local_capture(objects)
        self.assertEqual(yaml.safe_dump_all(objects, sort_keys=False), before)

    def test_optin_disk_volume_and_budgets_preserve_unrelated_fields(self):
        option = dict(directory='/capture', max_bytes=3 * local.GIB, minimum_free_bytes=5 * local.GIB)
        objects = self.objects(option)
        original = copy.deepcopy(objects)
        local.configure_local_capture(objects)
        pod = objects[1]['spec']['jobTemplate']['spec']['template']['spec']
        before = original[1]['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod['volumes'][:-1], before['volumes'])
        self.assertEqual(pod['volumes'][-1], {'name': 'capture-scratch', 'emptyDir': {'sizeLimit': str(4 * local.GIB)}})
        worker = pod['containers'][0]
        self.assertEqual(worker['volumeMounts'][-1], {'name': 'capture-scratch', 'mountPath': '/capture'})
        self.assertEqual(worker['resources']['requests']['ephemeral-storage'], str(3 * local.GIB))
        self.assertEqual(worker['resources']['limits']['ephemeral-storage'], str(4 * local.GIB))
        for section in ('requests', 'limits'):
            for key, value in before['containers'][0]['resources'][section].items():
                self.assertEqual(worker['resources'][section][key], value)
        once = copy.deepcopy(objects)
        local.configure_local_capture(objects)
        self.assertEqual(objects, once)
        self.assertEqual(objects[1]['spec']['jobTemplate']['spec']['activeDeadlineSeconds'], 5400)

    def test_conflicting_mounts_or_multiple_local_roots_are_rejected(self):
        option = dict(directory='/capture', max_bytes=1024, minimum_free_bytes=1024)
        for path in ('/capture/child', '/nfs-source', '/host/etc/capture'):
            objects = self.objects(dict(option, directory=path))
            if path == '/capture/child':
                objects[1]['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['volumeMounts'].append(
                    {'name': 'existing', 'mountPath': '/capture'})
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'overlaps'):
                local.configure_local_capture(objects)
        objects = self.objects(option)
        config = json.loads(objects[0]['data']['config.json'])
        config['host_sources'] = [{'staged_capture': True, 'local_capture': dict(option, directory='/different')}]
        objects[0]['data']['config.json'] = json.dumps(config)
        with self.assertRaisesRegex(ValueError, 'one local_capture'):
            local.configure_local_capture(objects)

    def test_both_source_configmaps_include_imported_helper(self):
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((root / 'disaster_recovery/kustomization.yaml').read_text())
        for generator in config['configMapGenerator']:
            if 'archive.py' in generator['files']:
                self.assertIn('local_capture.py', generator['files'])


if __name__ == '__main__':
    unittest.main()
