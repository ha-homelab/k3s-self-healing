"""Native request/receipt correlation uses fake WebSocket services only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
spec = importlib.util.spec_from_file_location('receipt_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
AGENT = 'synology_dsm.fixture'


class NativeReceiptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir();(self.root / 'recorder_backup').mkdir()
        (self.root / 'recorder_backup/manifest.json').write_text('{"sha256": "fixture"}')
        self.token = self.root / 'token';self.token.write_text('offline-test-token')
        self.receipt = self.root / 'backups/recorder-native-receipt.json'
        self.result = {'backup_job_id': 'requested-id'}
        self.archives = [self.backup('requested-id'), self.backup('unrelated-newer-id', newer=True)]
        self.calls, self.receive_timeouts = [], []
        self.hook = None

    def backup(self, identifier, newer=False, failed=False, agents=None):
        return {'backup_id': identifier, 'date': '2026-09-29T05:00:0' + ('2' if newer else '1') + '+00:00',
                'agents': {AGENT: {}} if agents is None else agents,
                'failed_agent_ids': [AGENT] if failed else []}

    async def run_backup(self, **settings):
        fixture = self

        class Socket:
            last = None
            generated = False

            async def __aenter__(self):return self
            async def __aexit__(self, *args):pass
            async def send_json(self, value):
                self.last = value;fixture.calls.append(value['type'])

            async def receive_json(self, **kwargs):
                fixture.receive_timeouts.append(kwargs.get('timeout'))
                if self.last is None:return {'type': 'auth_required'}
                kind = self.last['type']
                if kind == 'auth':return {'type': 'auth_ok'}
                if fixture.hook:
                    reply = await fixture.hook(kind, self.generated)
                    if reply is not None:return reply
                if kind == 'backup/generate_with_automatic_settings':
                    self.generated = True;result = fixture.result
                elif kind == 'backup/info':
                    result = {'state': 'idle', 'backups': fixture.archives if self.generated else []}
                elif kind == 'backup/config/info':
                    result = {'config': {'create_backup': {'agent_ids': [AGENT]}}}
                else:result = {'version': '2026.9.1', 'components': ['backup']}
                return {'id': self.last['id'], 'success': True, 'result': result}

        class Session:
            def __init__(self, **kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self, *args):pass
            def ws_connect(self, _):return Socket()

        aiohttp = types.SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kwargs: None)
        limits = {'POLL_INTERVAL_SECONDS': 0.001, **settings}
        with patch.dict(sys.modules, {'aiohttp': aiohttp}), patch.dict(os.environ, {
                'HA_TOKEN_FILE': str(self.token), 'HA_URL': 'http://offline.invalid'}), patch.multiple(
                runner, CONFIG=self.root, OUTPUT=self.root / 'recorder_backup', **limits), patch.object(
                runner, 'measure_config', return_value=0), patch.object(runner, 'check_native_capacity', return_value={}), patch.object(
                runner, 'verify_runtime_contract', return_value={'homeassistant_version': '2026.9.1'}):
            await runner.native_backup()

    async def test_success_receipt_matches_requested_id_not_newest_archive(self):
        await self.run_backup()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt['backup_id'], 'requested-id')
        self.assertEqual(receipt['recorder']['sha256'], 'fixture')
        self.assertEqual(self.calls.count('backup/generate_with_automatic_settings'), 1)

    async def test_updater_identity_is_preserved_in_the_exact_receipt(self):
        identity = {'HA_UPDATE_RUN_ID': 'a' * 32,
                    'HA_UPDATE_JOB_UID': '12345678-1234-1234-1234-123456789abc'}
        with patch.dict(os.environ, identity):
            await self.run_backup()
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt['update_run_id'], identity['HA_UPDATE_RUN_ID'])
        self.assertEqual(receipt['update_job_uid'], identity['HA_UPDATE_JOB_UID'])
        self.assertEqual(receipt['backup_id'], 'requested-id')

    async def test_invalid_updater_identity_refused_before_api_activity(self):
        with patch.dict(os.environ, {'HA_UPDATE_RUN_ID': '../bad'}):
            with self.assertRaisesRegex(RuntimeError, 'run identifier'):
                await self.run_backup()
        self.assertEqual(self.calls, [])

    async def test_job_uid_without_run_identifier_is_refused(self):
        with patch.dict(os.environ, {'HA_UPDATE_JOB_UID': '12345678-1234-1234-1234-123456789abc'}):
            with self.assertRaisesRegex(RuntimeError, 'Job UID'):
                await self.run_backup()
        self.assertEqual(self.calls, [])

    async def test_failed_requested_backup_cannot_be_replaced_by_unrelated_success(self):
        self.archives[0] = self.backup('requested-id', failed=True)
        with self.assertRaisesRegex(RuntimeError, 'every configured destination'):
            await self.run_backup()
        self.assertFalse(self.receipt.exists())

    async def test_requested_backup_missing_destination_is_refused(self):
        self.archives[0] = self.backup('requested-id', agents={})
        with self.assertRaisesRegex(RuntimeError, 'every configured destination'):
            await self.run_backup()
        self.assertFalse(self.receipt.exists())

    async def test_unrelated_archive_does_not_replace_missing_requested_backup(self):
        self.archives = self.archives[1:]
        with self.assertRaisesRegex(RuntimeError, 'without its archive'):
            await self.run_backup()
        self.assertFalse(self.receipt.exists())

    async def test_missing_or_invalid_requested_id_fails_before_polling(self):
        for result in (None, [], {}, {'backup_job_id': ''}, {'backup_job_id': '  '}, {'backup_job_id': 7}):
            with self.subTest(result=result):
                self.calls.clear();self.result = result
                with self.assertRaisesRegex(RuntimeError, 'backup identifier'):
                    await self.run_backup()
                self.assertEqual(self.calls.count('backup/info'), 2)  # Preflight only.
                self.assertFalse(self.receipt.exists())

    async def test_native_deadline_bounds_final_websocket_wait(self):
        async def blocked(kind, generated):
            if generated and kind == 'backup/info':await asyncio.Event().wait()
        self.hook = blocked
        task = asyncio.create_task(self.run_backup(NATIVE_TIMEOUT_SECONDS=0.04))
        try:
            done, _ = await asyncio.wait([task], timeout=1)
            self.assertIn(task, done, 'Native deadline did not bound the pending API call')
            with self.assertRaises(TimeoutError):await task
        finally:
            task.cancel();await asyncio.gather(task, return_exceptions=True)
        self.assertLessEqual(self.receive_timeouts[-1], 0.04)
        self.assertFalse(self.receipt.exists())

    async def test_native_deadline_shortens_poll_sleep(self):
        task = asyncio.create_task(self.run_backup(NATIVE_TIMEOUT_SECONDS=0.02, POLL_INTERVAL_SECONDS=15))
        try:
            done, _ = await asyncio.wait([task], timeout=1)
            self.assertIn(task, done, 'Polling slept beyond the remaining native deadline')
            with self.assertRaises(TimeoutError):await task
        finally:
            task.cancel();await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.calls.count('backup/info'), 2)
        self.assertFalse(self.receipt.exists())

    async def test_unrelated_messages_do_not_extend_api_request_deadline(self):
        async def events(kind, generated):
            if kind == 'backup/config/info':
                await asyncio.sleep(0.001)
                return {'type': 'unrelated-event'}
        self.hook = events
        task = asyncio.create_task(self.run_backup(API_TIMEOUT_SECONDS=0.02))
        try:
            done, _ = await asyncio.wait([task], timeout=1)
            self.assertIn(task, done, 'Unrelated messages renewed the API request deadline')
            with self.assertRaises(TimeoutError):await task
        finally:
            task.cancel();await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn('backup/generate_with_automatic_settings', self.calls)
        self.assertFalse(self.receipt.exists())
