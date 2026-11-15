"""Exercise capture custody through the real runner using synthetic local files."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from disaster_recovery import mysql_capture, runner


class MySQLRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.capture = self.base / 'ready'
        self.capture.mkdir()
        self.cipher = self.capture / 'k3s-mysql.sql.gz.age'
        self.cipher.write_bytes(b'age-encrypted-synthetic-fixture')
        now = int(time.time())
        self.receipt = {'status': 'qualified', 'format_version': 1, 'client_version': '8.4.10',
            'client_image': mysql_capture.CLIENT_IMAGE, 'schema': 'kine_fixture',
            'started_epoch': now - 100, 'completed_epoch': now,
            'sql_sha256': 'a' * 64, 'cipher_sha256': hashlib.sha256(self.cipher.read_bytes()).hexdigest(),
            'cipher_bytes': self.cipher.stat().st_size,
            'proof': {'rows': 4, 'revision': 1500, 'compact_revision': 500,
                      'next_id': 2001, 'typed_rows_sha256': 'b' * 64}}
        self.save_receipt()
        self.host = self.base / 'host'
        (self.host / 'etc/rancher/k3s').mkdir(parents=True)
        (self.host / 'etc/machine-id').write_text('fixture-machine')
        (self.host / 'etc/rancher/k3s/mysql-datastore.env').write_text(
            'K3S_DATASTORE_ENDPOINT=mysql://reader:fixture@tcp(127.0.0.1:13306)/kine_fixture')
        self.server = self.host / 'var/lib/rancher/k3s/server'
        (self.server / 'db').mkdir(parents=True)
        for name in ('state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal'):
            (self.server / 'db' / name).write_bytes(b'stale synthetic SQLite state')
        (self.server / 'token').write_text('synthetic-token')
        self.mqtt = self.host / 'mosquitto'
        self.mqtt.mkdir()
        (self.mqtt / 'mosquitto.db').write_bytes(b'synthetic persistence')
        self.pod = {'metadata': {'namespace': 'homeassistant', 'name': 'fixture-broker', 'uid': 'fixture-uid'},
                    'spec': {'containers': [{'name': 'mosquitto'}]}, 'status': {'phase': 'Running'}}
        self.api = Mock(spec=['get', 'list', 'checkpoint_mosquitto'])
        self.api.get.side_effect = {
            '/api/v1/nodes/fixture-node': {'status': {'nodeInfo': {'machineID': 'fixture-machine'}}},
            '/version': {'gitVersion': 'fixture'},
        }.__getitem__
        self.api.list.side_effect = {
            '/apis/apiextensions.k8s.io/v1/customresourcedefinitions': {'items': []},
            '/api/v1/pods': {'items': [self.pod]},
        }.__getitem__
        def checkpoint(_pod):
            path = self.mqtt / 'mosquitto.db'
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            return {'status': 'fixture-checkpoint'}
        self.api.checkpoint_mosquitto.side_effect = checkpoint
        self.config = {'node': 'fixture-node', 'minimum_free_bytes': 0, 'retain_completed': 2,
                       'namespaces': ['homeassistant'], 'api_collections': [],
                       'claims': [{'name': 'mosquitto'}],
                       'host_sources': [{'name': 'k3s-state', 'path': '/var/lib/rancher/k3s/server'}],
                       'k3s_mysql_capture': '/kine-capture/ready'}
        self.output = self.base / 'backups'

    def save_receipt(self):
        (self.capture / 'capture.json').write_text(json.dumps(self.receipt))

    def run_fixture(self, mutation=None, failure=None):
        validate = mysql_capture.validate_capture
        context = mysql_capture.verified_capture
        archive = runner.archive_component
        def mounted_validate(path):
            self.assertEqual(path, '/kine-capture/ready')
            return validate(self.capture)
        def mounted_context(path):
            self.assertEqual(path, '/kine-capture/ready')
            return context(self.capture)
        def archive_component(source, work, name, metadata, *args, **kwargs):
            if name == 'k3s-mysql':
                self.assertEqual(source, Path('/kine-capture/ready'))
                result = archive(self.capture, work, name, metadata, *args, **kwargs)
                if mutation:
                    mutation()
                if failure:
                    raise failure
                return result
            return archive(source, work, name, metadata, *args, **kwargs)
        previous_umask = os.umask(0o077)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {'NODE_NAME': 'fixture-node'}))
                stack.enter_context(patch.object(mysql_capture, 'validate_capture', side_effect=mounted_validate))
                stack.enter_context(patch.object(runner, 'verified_capture', side_effect=mounted_context))
                stack.enter_context(patch.object(runner, 'resolve_claim', return_value=(self.mqtt, {'type': 'fixture'})))
                stack.enter_context(patch.object(runner, 'archive_component', side_effect=archive_component))
                return runner.run(self.config, self.output, host=self.host, api=self.api)
        finally:
            os.umask(previous_umask)

    def assert_not_published(self):
        self.assertFalse((self.output / 'latest-success.json').exists())
        self.assertFalse(any(runner.SNAPSHOT_NAME.fullmatch(p.name) for p in self.output.iterdir()))
        latest = json.loads((self.output / 'latest-attempt.json').read_text())
        self.assertEqual(latest['state'], 'failed')
        self.assertIs(latest['snapshot_verified'], False)
        incomplete = list(self.output.glob('.incomplete-*'))
        self.assertEqual(len(incomplete), 1)
        self.assertFalse((incomplete[0] / 'manifest.json').exists())
        self.assertTrue((incomplete[0] / 'failure-private.txt').is_file())

    def test_valid_capture_is_archived_and_stale_sqlite_is_excluded(self):
        identity = self.cipher.stat()
        read = os.read
        hashed_bytes = []
        def reading(descriptor, count):
            info = os.fstat(descriptor)
            result = read(descriptor, count)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                hashed_bytes.append(len(result))
            return result
        with patch.object(mysql_capture.os, 'read', side_effect=reading):
            snapshot = self.run_fixture()
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        self.assertIs(manifest['completed'], True)
        component = manifest['components'][0]
        self.assertEqual(component['name'], 'k3s-mysql')
        self.assertEqual(component['source']['capture'], self.receipt)
        with tarfile.open(snapshot / component['archive'], 'r:gz') as archive:
            self.assertEqual(archive.extractfile('k3s-mysql.sql.gz.age').read(), self.cipher.read_bytes())
        state = next(c for c in manifest['components'] if c['name'] == 'k3s-state')
        with tarfile.open(snapshot / state['archive'], 'r:gz') as archive:
            self.assertIn('token', archive.getnames())
            self.assertFalse(any('state.db' in name for name in archive.getnames()))
        # One early validation and one held-context validation. Context exit
        # checks identities/metadata and does not perform a third full hash.
        self.assertEqual(sum(hashed_bytes), 2 * self.receipt['cipher_bytes'])
        self.assertEqual(sum(size > 0 for size in hashed_bytes), 2)

    def test_same_bytes_receipt_replacement_during_archive_is_not_published(self):
        def replace():
            path = self.capture / 'capture.json'
            replacement = self.base / 'replacement'
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        with self.assertRaises(ValueError):
            self.run_fixture(replace)
        self.assert_not_published()

    def test_same_bytes_cipher_replacement_during_archive_is_not_published(self):
        def replace():
            replacement = self.base / 'replacement'
            replacement.write_bytes(self.cipher.read_bytes())
            replacement.replace(self.cipher)
        with self.assertRaises(ValueError):
            self.run_fixture(replace)
        self.assert_not_published()

    def test_root_replacement_during_archive_is_not_published(self):
        def replace():
            previous = self.base / 'old-ready'
            self.capture.rename(previous)
            self.capture.mkdir()
            for name in ('capture.json', 'k3s-mysql.sql.gz.age'):
                (self.capture / name).write_bytes((previous / name).read_bytes())
        with self.assertRaises(ValueError):
            self.run_fixture(replace)
        self.assert_not_published()

    def test_in_place_mutation_during_archive_is_not_published(self):
        def mutate():
            info = self.cipher.stat()
            self.cipher.write_bytes(b'X' * info.st_size)
            os.utime(self.cipher, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(ValueError):
            self.run_fixture(mutate)
        self.assert_not_published()

    def test_parent_directory_rename_restore_during_archive_is_not_published(self):
        mount = self.base / 'mount'
        mount.mkdir()
        self.capture.rename(mount / 'ready')
        self.capture = mount / 'ready'
        self.cipher = self.capture / 'k3s-mysql.sql.gz.age'
        saved = self.base / 'held-mount'
        archive = runner.archive_component
        changed = []
        def swapping(source, work, name, metadata, *args, **kwargs):
            if name != 'k3s-mysql':
                return archive(source, work, name, metadata, *args, **kwargs)
            before = mount.stat()
            mount.rename(saved)
            self.capture.mkdir(parents=True)
            self.save_receipt()
            self.cipher.write_bytes(b'X' * self.receipt['cipher_bytes'])
            try:
                return archive(source, work, name, metadata, *args, **kwargs)
            finally:
                shutil.rmtree(mount)
                saved.rename(mount)
                after = mount.stat()
                self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
                changed.append(True)
        with patch.object(runner, 'archive_component', side_effect=swapping):
            with self.assertRaises(ValueError):
                self.run_fixture()
        self.assertEqual(changed, [True])
        self.assert_not_published()

    def test_archive_failure_remains_primary_and_no_snapshot_is_published(self):
        failure = OSError('synthetic archive failure')
        def remove():
            (self.capture / 'capture.json').unlink()
        with self.assertRaises(OSError) as raised:
            self.run_fixture(remove, failure)
        self.assertIs(raised.exception, failure)
        self.assert_not_published()

    def test_invalid_capture_refuses_before_snapshot_or_cluster_side_effects(self):
        self.receipt['completed_epoch'] -= 1900
        self.receipt['started_epoch'] -= 1900
        self.save_receipt()
        with patch.object(runner.os, 'umask') as umask:
            with self.assertRaises(ValueError):
                self.run_fixture()
        self.api.get.assert_not_called()
        self.api.list.assert_not_called()
        self.assertFalse(self.output.exists())
        # run_fixture owns its test umask; the runner must not set one before
        # rejecting the unqualified capture.
        self.assertEqual(umask.call_count, 2)
