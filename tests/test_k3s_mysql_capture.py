import copy
import hashlib
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from disaster_recovery.mysql_capture import CLIENT_IMAGE, mysql_schema, prepare_capture, validate_capture
from disaster_recovery import mysql_capture as capture_module

class KineCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.capture=self.root/'ready';self.capture.mkdir()
        cipher=b'age-encrypted-fixture';(self.capture/'k3s-mysql.sql.gz.age').write_bytes(cipher)
        self.receipt={'status':'qualified','format_version':1,'client_version':'8.4.10',
          'client_image':CLIENT_IMAGE,'schema':'kine_fixture','started_epoch':900,'completed_epoch':1000,
          'sql_sha256':'a'*64,'cipher_sha256':hashlib.sha256(cipher).hexdigest(),'cipher_bytes':len(cipher),
          'proof':{'rows':4,'revision':1500,'compact_revision':500,'next_id':2001,'typed_rows_sha256':'b'*64}}
        self.save()
    def save(self): (self.capture/'capture.json').write_text(json.dumps(self.receipt))
    def test_valid_and_stale(self):
        self.assertEqual(validate_capture(self.capture,now=1100)['proof']['next_id'],2001)
        with self.assertRaises(ValueError):validate_capture(self.capture,now=3000)
    def test_corruption_fails(self):
        (self.capture/'k3s-mysql.sql.gz.age').write_bytes(b'age-encrypted-fixturX')
        with self.assertRaises(ValueError):validate_capture(self.capture,now=1100)
    def test_no_extra_plaintext_or_symlinks(self):
        (self.capture/'plain.sql').write_text('private')
        with self.assertRaises(ValueError):validate_capture(self.capture,now=1100)
        (self.capture/'plain.sql').unlink();(self.capture/'capture.json').unlink()
        (self.capture/'capture.json').symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):validate_capture(self.capture,now=1100)
    def test_revision_and_client_guards(self):
        for change in ({'client_version':'8.4.9'},{'status':'running'},{'cipher_bytes':0}):
            original=self.receipt.copy();self.receipt.update(change);self.save()
            with self.assertRaises(ValueError):validate_capture(self.capture,now=1100)
            self.receipt=original
        self.receipt['proof']['next_id']=100;self.save()
        with self.assertRaises(ValueError):validate_capture(self.capture,now=1100)
    def test_mysql_missing_capture_refuses_before_stale_sqlite(self):
        folder=self.root/'etc/rancher/k3s';folder.mkdir(parents=True)
        (folder/'mysql-datastore.env').write_text('K3S_DATASTORE_ENDPOINT="mysql://private@tcp(127.0.0.1:13306)/kine_fixture"')
        with self.assertRaisesRegex(ValueError,'stale SQLite'):prepare_capture({},self.root)
        with patch('disaster_recovery.mysql_capture.validate_capture',return_value={'qualified':True,'schema':'kine_fixture'}) as verify:
            self.assertEqual(prepare_capture({'k3s_mysql_capture':'/kine-capture/ready'},self.root),{'qualified':True,'schema':'kine_fixture'})
            verify.assert_called_once_with('/kine-capture/ready')
    def test_sqlite_profile_does_not_require_mysql(self):
        self.assertIsNone(prepare_capture({},self.root))
        with self.assertRaises(ValueError):prepare_capture({'k3s_mysql_capture':'/kine-capture/ready'},self.root)

    def test_managed_endpoint_quotes_malformed_and_schema_binding(self):
        folder=self.root/'etc/rancher/k3s';folder.mkdir(parents=True)
        path=folder/'mysql-datastore.env'
        endpoint='mysql://reader:private@tcp(127.0.0.1:13306)/kine_fixture'
        for value in (endpoint, '"'+endpoint+'"'):
            path.write_text('K3S_DATASTORE_ENDPOINT='+value+'\n')
            self.assertEqual(mysql_schema(self.root),'kine_fixture')
        with patch('disaster_recovery.mysql_capture.validate_capture',return_value={'schema':'kine_canary_wrong'}):
            with self.assertRaisesRegex(ValueError,'different datastore'):
                prepare_capture({'k3s_mysql_capture':'/kine-capture/ready'},self.root)
        for value in ('', 'K3S_DATASTORE_ENDPOINT="mysql://broken', 'K3S_DATASTORE_ENDPOINT=sqlite:///old',
                      'K3S_DATASTORE_ENDPOINT='+endpoint+'\nK3S_DATASTORE_ENDPOINT='+endpoint):
            path.write_text(value)
            with self.assertRaises(ValueError):mysql_schema(self.root)

    def test_dangling_managed_endpoint_never_falls_back_to_sqlite(self):
        folder = self.root / 'etc/rancher/k3s'
        folder.mkdir(parents=True)
        (folder / 'mysql-datastore.env').symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError):
            mysql_schema(self.root)
        with self.assertRaises(ValueError):
            prepare_capture({}, self.root)

    def test_managed_endpoint_parent_symlink_is_refused(self):
        (self.root / 'etc').mkdir()
        (self.root / 'etc/rancher').symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError):
            mysql_schema(self.root)

    def test_removed_endpoint_during_read_is_not_treated_as_absent(self):
        folder = self.root / 'etc/rancher/k3s'
        folder.mkdir(parents=True)
        path = folder / 'mysql-datastore.env'
        path.write_text('K3S_DATASTORE_ENDPOINT=mysql://reader:fixture@tcp(127.0.0.1:13306)/kine_fixture')
        read = os.read
        removed = []
        def changing(fd, size):
            block = read(fd, size)
            if block.startswith(b'K3S_DATASTORE_ENDPOINT=') and not removed:
                path.unlink()
                removed.append(True)
            return block
        with patch.object(capture_module.os, 'read', side_effect=changing):
            with self.assertRaises(ValueError):
                mysql_schema(self.root)
        self.assertEqual(removed, [True])

    def test_oversized_receipt_is_refused_before_json_parsing(self):
        (self.capture / 'capture.json').write_bytes(b' ' * (capture_module.MAX_RECEIPT + 1))
        with patch.object(capture_module.json, 'loads') as parse:
            with self.assertRaises(ValueError):
                validate_capture(self.capture, now=1100)
        parse.assert_not_called()

    def test_duplicate_or_plaintext_receipt_fields_are_refused(self):
        path = self.capture / 'capture.json'
        valid = json.dumps(self.receipt)
        for payload in (valid[:-1] + ',"schema":"kine_other"}',
                        json.dumps(self.receipt | {'plaintext_sql': 'private-fixture'})):
            with self.subTest(payload=payload):
                path.write_text(payload)
                with self.assertRaises(ValueError):
                    validate_capture(self.capture, now=1100)

    def test_receipt_boolean_and_future_clock_fields_are_refused(self):
        valid = copy.deepcopy(self.receipt)
        for change in ({'format_version': True}, {'cipher_bytes': True},
                       {'started_epoch': False}, {'completed_epoch': 1200},
                       {'proof': []}):
            self.receipt = valid | change
            self.save()
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_capture(self.capture, now=1100)
        self.receipt = valid
        self.save()
        for now in (True, float('nan'), float('inf')):
            with self.subTest(now=now), self.assertRaises(ValueError):
                validate_capture(self.capture, now=now)

    def verify_during_cipher_read(self, mutation):
        read = os.read
        changed = []
        cipher_descriptor = []
        self.cipher_read_sizes = []
        def changing(fd, size):
            block = read(fd, size)
            if block.startswith(b'age-encrypted') and not changed:
                cipher_descriptor.append(fd)
                self.cipher_read_sizes.append(size)
                mutation()
                changed.append(True)
            elif cipher_descriptor and fd == cipher_descriptor[0]:
                self.cipher_read_sizes.append(size)
            return block
        try:
            with patch.object(capture_module.os, 'read', side_effect=changing):
                return validate_capture(self.capture, now=1100)
        finally:
            self.assertEqual(changed, [True])

    def test_cipher_growth_is_bounded_by_declared_bytes_plus_one(self):
        def grow():
            with (self.capture / 'k3s-mysql.sql.gz.age').open('ab') as stream:
                stream.write(b'x' * 4096)
        with self.assertRaises(ValueError):
            self.verify_during_cipher_read(grow)
        self.assertLessEqual(max(self.cipher_read_sizes), self.receipt['cipher_bytes'] + 1)
        self.assertLessEqual(len(self.cipher_read_sizes), 2)

    def test_same_size_cipher_mutation_with_restored_mtime_is_refused(self):
        path = self.capture / 'k3s-mysql.sql.gz.age'
        before = path.stat()
        def mutate():
            path.write_bytes(b'X' * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ValueError):
            self.verify_during_cipher_read(mutate)

    def test_cipher_replacement_with_same_bytes_is_refused(self):
        path = self.capture / 'k3s-mysql.sql.gz.age'
        def replace():
            replacement = self.root / 'replacement'
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        with self.assertRaises(ValueError):
            self.verify_during_cipher_read(replace)

    def test_receipt_replacement_while_hashing_cipher_is_refused(self):
        path = self.capture / 'capture.json'
        def replace():
            replacement = self.root / 'replacement'
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        with self.assertRaises(ValueError):
            self.verify_during_cipher_read(replace)

    def test_directory_replacement_while_hashing_is_refused(self):
        def replace():
            old = self.root / 'old'
            self.capture.rename(old)
            self.capture.mkdir()
            for name in ('capture.json', 'k3s-mysql.sql.gz.age'):
                (self.capture / name).write_bytes((old / name).read_bytes())
        with self.assertRaises(ValueError):
            self.verify_during_cipher_read(replace)

    def test_real_clock_is_rechecked_after_hashing(self):
        with patch.object(capture_module.time, 'time', side_effect=[1100, 3000]):
            with self.assertRaises(ValueError):
                validate_capture(self.capture)

    def test_consumption_context_refuses_same_bytes_replacements_and_mutation(self):
        for changed in ('capture.json', 'k3s-mysql.sql.gz.age', 'directory', 'in_place'):
            with self.subTest(changed=changed):
                self.save()
                cipher = self.capture / 'k3s-mysql.sql.gz.age'
                cipher.write_bytes(b'age-encrypted-fixture')
                with self.assertRaises(ValueError):
                    with capture_module.verified_capture(self.capture, now=1100):
                        if changed == 'directory':
                            old = self.root / 'old-context'
                            self.capture.rename(old)
                            self.capture.mkdir()
                            for name in ('capture.json', 'k3s-mysql.sql.gz.age'):
                                (self.capture / name).write_bytes((old / name).read_bytes())
                        elif changed == 'in_place':
                            before = cipher.stat()
                            cipher.write_bytes(b'X' * before.st_size)
                            os.utime(cipher, ns=(before.st_atime_ns, before.st_mtime_ns))
                        else:
                            path = self.capture / changed
                            replacement = self.root / 'context-replacement'
                            replacement.write_bytes(path.read_bytes())
                            os.replace(replacement, path)

    def test_consumption_context_preserves_original_failure_and_closes_descriptors(self):
        for failure in (RuntimeError('synthetic consumer failure'), OSError('synthetic IO failure')):
            self.save()
            descriptors = []
            opened = capture_module.os.open
            def opening(*args, **kwargs):
                descriptor = opened(*args, **kwargs)
                descriptors.append(descriptor)
                return descriptor
            with patch.object(capture_module.os, 'open', side_effect=opening):
                with self.assertRaises(type(failure)) as raised:
                    with capture_module.verified_capture(self.capture, now=1100):
                        (self.capture / 'capture.json').unlink()
                        raise failure
            self.assertIs(raised.exception, failure)
            self.assertEqual(len(descriptors), 4)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_consumption_context_rechecks_freshness_before_acceptance(self):
        with patch.object(capture_module.time, 'time', side_effect=[1100, 1100, 3000]):
            with self.assertRaises(ValueError):
                with capture_module.verified_capture(self.capture):
                    pass
