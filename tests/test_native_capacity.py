"""Capacity scenarios use local fixtures; no database/HA/NAS services are called."""
import importlib.util
import io
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
spec = importlib.util.spec_from_file_location('capacity_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
GIB = 1024**3


class CapacityTest(unittest.TestCase):
    def test_maximum_keeps_reserve_in_both_phases(self):
        for total in (24 * GIB, 45 * GIB, runner.EXPANDED_FILESYSTEM_MIN, 200 * GIB):
            for free in (7 * GIB, 9 * GIB, 20 * GIB):
                for old in (0, 2 * GIB, 3 * GIB, 5 * GIB):
                    for measured in (0, 640 * 1024**2, 2 * GIB):
                        reserve = runner.reserve_bytes(total)
                        try:
                            limit = runner.compressed_budget(free, old, total, measured)
                        except RuntimeError:
                            continue
                        self.assertLessEqual(limit, runner.reviewed_export_ceiling(total))
                        self.assertEqual(limit % 1024, 0)
                        self.assertGreaterEqual(free - limit, reserve)
                        self.assertGreaterEqual(free + old - 2 * limit - runner.archive_overhead(measured), reserve)

    def test_constrained_filesystem_keeps_eviction_margin(self):
        total, free, old, measured = 45 * GIB, 9 * GIB, 2 * GIB, 640 * 1024**2
        limit = runner.compressed_budget(free, old, total, measured)
        self.assertEqual(limit, 3 * GIB)
        self.assertGreaterEqual(runner.reserve_bytes(total), total // 20 + GIB)
        self.assertGreaterEqual(free + old - 2 * limit - runner.archive_overhead(measured), runner.reserve_bytes(total))

    def test_low_space_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'free space'):
            runner.compressed_budget(2 * GIB, 2 * GIB, 24 * GIB, 0)

    def test_ceiling_changes_only_at_reviewed_filesystem_size(self):
        for total, ceiling in ((45 * GIB, 3 * GIB),
                               (runner.EXPANDED_FILESYSTEM_MIN - 1, 3 * GIB),
                               (runner.EXPANDED_FILESYSTEM_MIN, 5 * GIB)):
            self.assertEqual(runner.reviewed_export_ceiling(total), ceiling)
            self.assertEqual(runner.compressed_budget(20 * GIB, 5 * GIB, total, 640 * 1024**2), ceiling)

    def test_expanded_filesystem_does_not_bypass_live_space_or_config_growth(self):
        total = runner.EXPANDED_FILESYSTEM_MIN
        reserve = runner.reserve_bytes(total)
        for measured in (640 * 1024**2, 2 * GIB):
            free = reserve + 4 * GIB
            limit = runner.compressed_budget(free, 0, total, measured)
            self.assertLess(limit, 3 * GIB)
            self.assertGreaterEqual(free - 2 * limit - runner.archive_overhead(measured), reserve)

    def test_expanded_host_fits_two_full_sized_weeks(self):
        # Synthetic disk/config sizes exercise two consecutive backup generations.
        total, free, old, measured = 54 * GIB, 17 * GIB, 2 * GIB, 704 * 1024**2
        first = runner.compressed_budget(free, old, total, measured)
        self.assertEqual(first, 5 * GIB)
        # After publishing/uploading, the previous export was reclaimed while
        # the new 5 GiB generation remains. Next week must coexist with it.
        next_free = free + old - first
        second = runner.compressed_budget(next_free, first, total, measured)
        self.assertEqual(second, 5 * GIB)
        reserve = runner.reserve_bytes(total)
        self.assertEqual(reserve, 3789 * 1024**2)
        self.assertEqual(next_free, 14 * GIB)
        for available, reclaimable in ((free, old), (next_free, first)):
            self.assertEqual(available + reclaimable - 2 * first - runner.archive_overhead(measured), 8032 * 1024**2)
        self.assertGreaterEqual(next_free - second, reserve)
        self.assertGreaterEqual(next_free + first - 2 * second - runner.archive_overhead(measured), reserve)

    def test_retained_margin_exceeds_reviewed_five_gib_stream_expansion(self):
        # Default zlib stored blocks: at most five bytes per 16 KiB. Add a
        # generous 64 KiB for final-block/gzip framing and tar alignment,
        # then securetar v3's 17 bytes per MiB and two tar records for framing.
        sql = runner.EXPANDED_SQL_CEILING
        gzip_growth = ((sql + 16383) // 16384) * 5 + 64 * 1024
        cipher_growth = ((sql + gzip_growth + 1024**2 - 1) // 1024**2) * 17
        self.assertLess(gzip_growth + cipher_growth + 2 * tarfile.RECORDSIZE,
                        runner.archive_overhead(0))

    def test_final_guard_uses_actual_size_and_preserves_export_on_failure(self):
        sql = Mock()
        sql.is_symlink.return_value = False;sql.is_file.return_value = True
        sql.stat.return_value.st_size = 5 * GIB
        total, measured = runner.EXPANDED_FILESYSTEM_MIN, 640 * 1024**2
        required = runner.reserve_bytes(total) + 5 * GIB + runner.archive_overhead(measured)
        with patch.object(runner, 'active_export', return_value=sql):
            for free in (required - 1, required):
                disk = shutil._ntuple_diskusage(total, total - free, free)
                with patch.object(runner.shutil, 'disk_usage', return_value=disk):
                    if free < required:
                        with self.assertRaisesRegex(RuntimeError, 'verified SQL export retained'):
                            runner.check_native_capacity(measured)
                    else:
                        self.assertEqual(runner.check_native_capacity(measured)['required_bytes'], required)
            sql.stat.return_value.st_size = 5 * GIB + 1
            with patch.object(runner.shutil, 'disk_usage', return_value=disk):
                with self.assertRaisesRegex(RuntimeError, 'size-limited'):
                    runner.check_native_capacity(measured)
            sql.stat.return_value.st_size = 5 * GIB
            small_disk = shutil._ntuple_diskusage(45 * GIB, 0, 45 * GIB)
            with patch.object(runner.shutil, 'disk_usage', return_value=small_disk):
                with self.assertRaisesRegex(RuntimeError, 'size-limited'):
                    runner.check_native_capacity(measured)
        sql.unlink.assert_not_called()

    def test_failed_capacity_reports_numbers_before_raising_and_retains_export(self):
        sql = Mock();sql.is_symlink.return_value = False;sql.is_file.return_value = True
        sql.stat.return_value.st_size = 3 * GIB
        total, measured = 54 * GIB, 1000
        required = runner.reserve_bytes(total) + 3 * GIB + runner.archive_overhead(measured)
        disk = shutil._ntuple_diskusage(total, total - required + 123, required - 123)
        output = io.StringIO();real_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            termination = Path(directory) / 'termination';termination.touch()
            def open_report(path, flags):
                self.assertEqual(path, '/dev/termination-log')
                return real_open(termination, flags)
            with patch.object(runner, 'active_export', return_value=sql), patch.object(runner.shutil, 'disk_usage', return_value=disk), patch.object(runner.os, 'open', side_effect=open_report), patch.dict(os.environ, {'HA_UPDATE_RUN_ID': 'a' * 32}), redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, 'verified SQL export retained'):
                    runner.check_native_capacity(measured)
            report = json.loads(termination.read_text())
            self.assertEqual(report['shortfall_bytes'], 123)
            self.assertEqual(report['required_bytes'], required)
            self.assertEqual(report['sql_bytes'], 3 * GIB)
            self.assertEqual(report['update_run_id'], 'a' * 32)
            self.assertTrue(report['sql_export_retained'])
            self.assertLess(len(termination.read_bytes()), 4096)
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(lines[0]['native_capacity']['shortfall_bytes'], 123)
            self.assertEqual(lines[1]['backup_failure'], report)
        sql.unlink.assert_not_called()

    def test_termination_report_failure_does_not_bypass_capacity_guard(self):
        sql = Mock();sql.is_symlink.return_value = False;sql.is_file.return_value = True
        sql.stat.return_value.st_size = 3 * GIB
        disk = shutil._ntuple_diskusage(54 * GIB, 53 * GIB, GIB)
        with patch.object(runner, 'active_export', return_value=sql), patch.object(runner.shutil, 'disk_usage', return_value=disk), patch.object(runner.os, 'open', side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, 'verified SQL export retained'):
                runner.check_native_capacity(1000)
        sql.unlink.assert_not_called()

    def test_unreviewed_native_paths_refused(self):
        good = {'version': '2026.9.1', 'components': ['backup', 'synology_dsm']}
        config = lambda agents: {'create_backup': {'agent_ids': agents}}
        with patch.object(runner, 'verify_runtime_contract', return_value={'homeassistant_version': '2026.9.1'}):
            self.assertEqual(runner.validate_native_path(good, config(['synology_dsm.test'])), ['synology_dsm.test'])
            for agents in ([], ['backup.local'], ['synology_dsm.test', 'backup.local'], ['other.nas']):
                with self.assertRaises(RuntimeError):runner.validate_native_path(good, config(agents))
            for bad in ({**good, 'version': '2026.10.0'}, {**good, 'components': ['hassio']}):
                with self.assertRaises(RuntimeError):runner.validate_native_path(bad, config(['synology_dsm.test']))
        with patch.object(runner, 'verify_runtime_contract', return_value={'homeassistant_version': '2026.10.0'}):
            self.assertEqual(runner.validate_native_path({**good, 'version': '2026.10.0'}, config(['synology_dsm.test'])), ['synology_dsm.test'])

    def test_unreviewed_image_cannot_start_native_backup(self):
        with patch.object(runner, 'verify_runtime_contract', side_effect=RuntimeError('unreviewed source')):
            with self.assertRaisesRegex(RuntimeError, 'unreviewed source'):
                runner.validate_native_path({'version': '2026.10.0'}, {'create_backup': {'agent_ids': ['synology_dsm.test']}})


class ConfigMeasurementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'config';self.root.mkdir()
        self.config_patch = patch.object(runner, 'CONFIG', self.root);self.config_patch.start();self.addCleanup(self.config_patch.stop)
        self.output_patch = patch.object(runner, 'OUTPUT', self.root / 'recorder_backup');self.output_patch.start();self.addCleanup(self.output_patch.stop)

    def write(self, name, value):
        path = self.root / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(value);return path

    def export(self):
        data = gzip.compress(b'fixture SQL' * 1024)
        path = self.write('recorder_backup/recorder.sql.gz', data)
        self.write('recorder_backup/manifest.json', json.dumps({'format': 1, 'file': path.name, 'sha256': hashlib.sha256(data).hexdigest(), 'compressed_bytes': len(data), 'uncompressed_bytes': 11264, 'tables': [{'name': 'states'}]}).encode())
        return path

    def test_exact_exclusions_sql_and_symlinks_without_output_archive(self):
        expected = ['configuration.yaml', 'backups/rollback/data.tar', 'backups/saved.tar.gz', 'nested/backups/saved.tar', 'recorder_backup/other.sql.xz', 'recorder_backup/other.sql.gz']
        for name in expected:self.write(name, b'keep')
        self.export()
        expected.append('recorder_backup/manifest.json')
        for name in ['backups/old.tar', 'tmp_backups/current.tar']:
            self.write(name, os.urandom(65536))
        outside = Path(self.temp.name) / 'private';outside.mkdir();(outside / 'secret').write_bytes(os.urandom(65536))
        (self.root / 'external').symlink_to(outside)
        (self.root / 'loop').symlink_to(self.root)
        real_open = tarfile.open;captured = io.BytesIO()
        def capture(*args, **kwargs):
            # Inspect a test-only copy of the counted stream; production writes
            # only to a byte counter and cannot allocate an archive file.
            sink = kwargs['fileobj'];original = sink.write
            def write(block):captured.write(block);return original(block)
            sink.write = write
            return real_open(*args, **kwargs)
        with patch.object(runner.tarfile, 'open', side_effect=capture):
            size = runner.measure_config(['backups/*.tar', 'tmp_backups/*.tar'])
        self.assertEqual(size, len(captured.getvalue()))
        with real_open(fileobj=io.BytesIO(captured.getvalue()), mode='r:gz') as archive:
            names = archive.getnames()
            for name in expected:self.assertIn('data/' + name, names)
            for name in ['backups/old.tar', 'tmp_backups/current.tar', 'recorder_backup/recorder.sql.gz', 'external/secret']:
                self.assertNotIn('data/' + name, names)
            self.assertTrue(archive.getmember('data/external').issym())
            self.assertTrue(archive.getmember('data/loop').issym())

    def test_xz_manifest_selects_only_active_generation_for_exclusion(self):
        data = lzma.compress(b'fixture SQL' * 1024, preset=0)
        digest = hashlib.sha256(data).hexdigest();name = f'recorder-{digest}.sql.xz'
        active = self.write('recorder_backup/' + name, data)
        self.write('recorder_backup/manifest.json', json.dumps({'format': 2, 'compression': 'xz', 'file': name, 'sha256': digest, 'compressed_bytes': len(data), 'uncompressed_bytes': 11264, 'tables': [{'name': 'states'}]}).encode())
        baseline = runner.measure_config([])
        self.write('recorder_backup/unrelated.sql.xz', os.urandom(65536))
        self.assertGreater(runner.measure_config([]), baseline + 60000)
        stale = self.write('recorder_backup/recorder.sql.gz', b'old')
        with self.assertRaisesRegex(RuntimeError, 'Inactive'):runner.measure_config([])
        runner.active_export(cleanup=True)
        self.assertFalse(stale.exists());self.assertEqual(runner.active_export(), active)

    def test_hardlinks_and_symlinks_never_count_as_reclaimed_space(self):
        old = self.write('old', b'preserve')
        self.assertGreater(runner.reclaimable_export(old), 0)
        link = self.root / 'link';link.symlink_to(old)
        self.assertEqual(runner.reclaimable_export(link), 0)
        os.link(old, self.root / 'hardlink')
        self.assertEqual(runner.reclaimable_export(old), 0)

    def test_post_publish_space_drop_preserves_export(self):
        export = self.export(); original = export.read_bytes()
        disk = shutil._ntuple_diskusage(24 * GIB, 22 * GIB, 2 * GIB)
        with patch.object(runner, 'measure_config', return_value=640 * 1024**2), patch.object(runner.shutil, 'disk_usage', return_value=disk):
            with self.assertRaisesRegex(RuntimeError, 'verified SQL export retained'):
                runner.check_native_capacity(640 * 1024**2)
        self.assertEqual(export.read_bytes(), original)

    def test_measurement_failure_is_not_treated_as_zero_size(self):
        with patch.object(runner.tarfile, 'open', side_effect=OSError('unreadable config')):
            with self.assertRaises(OSError):runner.measure_config([])



class ApiCapacityTest(unittest.IsolatedAsyncioTestCase):
    async def test_space_failure_never_requests_backup(self):
        import types
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / 'token';token.write_text('offline-test-token')
            calls = []
            measured_first = [False]
            async def measure(*args):measured_first[0] = True;return 640 * 1024**2
            class Socket:
                last = None
                async def __aenter__(self):return self
                async def __aexit__(self, *args):pass
                async def send_json(self, value):self.last = value;calls.append(value['type'])
                async def receive_json(self, **kwargs):
                    if self.last is None:return {'type': 'auth_required'}
                    if self.last['type'] == 'auth':return {'type': 'auth_ok'}
                    result = {'backup/info': {'state': 'idle'},
                              'backup/config/info': {'config': {'create_backup': {'agent_ids': ['synology_dsm.fixture']}}},
                              'get_config': {'version': '2026.9.1', 'components': ['backup']}}[self.last['type']]
                    return {'id': self.last['id'], 'success': True, 'result': result}
            class Session:
                def __init__(self, **kwargs):
                    if not measured_first[0]:raise AssertionError('WebSocket opened before config scan')
                async def __aenter__(self):return self
                async def __aexit__(self, *args):pass
                def ws_connect(self, _):return Socket()
            aiohttp = types.SimpleNamespace(TraceConfig=lambda: types.SimpleNamespace(on_request_redirect=[]), ClientSession=Session, ClientTimeout=lambda **kw: None)
            with patch.dict('sys.modules', {'aiohttp': aiohttp}), patch.dict(os.environ, {'HA_TOKEN_FILE': str(token), 'HA_URL': 'http://offline.invalid'}), patch.object(runner.asyncio, 'to_thread', new=AsyncMock(side_effect=measure)), patch.object(runner, 'check_native_capacity', side_effect=RuntimeError('Insufficient free space')), patch.object(runner, 'verify_runtime_contract', return_value={'homeassistant_version': '2026.9.1'}):
                with self.assertRaisesRegex(RuntimeError, 'free space'):await runner.native_backup()
            self.assertNotIn('backup/generate_with_automatic_settings', calls)


class ReaderPermissionsTest(unittest.TestCase):
    def test_only_reading_containers_gain_read_search(self):
        import yaml
        cron = next(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
        pod = cron['spec']['jobTemplate']['spec']['template']['spec']
        containers = {c['name']:c for c in pod['initContainers'] + pod['containers']}
        for name, container in containers.items():
            context = container['securityContext']
            self.assertFalse(context['allowPrivilegeEscalation'])
            self.assertTrue(context['readOnlyRootFilesystem'])
            self.assertEqual(context['capabilities']['drop'], ['ALL'])
            self.assertEqual(context['capabilities'].get('add', []), [] if name == 'mysql-dump' else ['DAC_READ_SEARCH'])
