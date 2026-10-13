"""Approval must be durable, exact, single-use and independent of MQTT."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core_updates'))
from model import Store

IMAGE = 'ghcr.io/home-assistant/home-assistant:2026.10.0@sha256:' + 'a' * 64

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'status.json')
        self.offer = self.store.offer('2026.9.1', '2026.10.0', IMAGE, 'old-image', 'https://example.test')
        self.body = {'action':'install', 'target_version':'2026.10.0', 'candidate_id':self.offer['candidate_id']}

    def test_acceptance_survives_restart_and_cannot_be_replayed(self):
        self.store.approve(self.body)
        reloaded = Store(self.store.path)
        self.assertEqual(reloaded.read()['stage'], 'preparing')
        with self.assertRaises(RuntimeError): reloaded.approve(self.body)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_approvals_only_accept_once(self):
        def attempt(_):
            try: self.store.approve(self.body); return True
            except RuntimeError: return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 1)

    def test_stale_expired_different_version_and_extra_fields_rejected(self):
        for changes in ({'candidate_id':'0'*32},{'target_version':'2026.10.1'}):
            with self.assertRaises(RuntimeError): self.store.approve(self.body | changes)
        with self.assertRaises(ValueError): self.store.approve(self.body | {'image':'arbitrary'})
        with self.assertRaises(RuntimeError): self.store.approve(self.body, now=self.offer['expires_at'])
        self.assertEqual(self.store.read()['stage'], 'available')

    def test_mutable_or_unstable_images_rejected(self):
        for image in ('latest', IMAGE.replace('2026.10.0@','2026.10.1@')):
            with self.assertRaises(ValueError): self.store.offer('2026.9.1','2026.10.0',image,'old','url')
        with self.assertRaises(ValueError): self.store.offer('2026.9.1','2026.10.0b1',IMAGE,'old','url')

    def test_failed_or_active_attempt_not_rearmed_by_same_release(self):
        for stage in ('failed','preparing','backup','deploying','recovery_required'):
            self.store.update(stage=stage)
            self.store.offer('2026.9.1','2026.10.0',IMAGE,'old-image','url')
            self.assertEqual(self.store.read()['stage'],stage)

    def failed(self):
        self.store.approve(self.body)
        self.store.update(stage='failed', completed_at='2026-10-09T03:42:00+00:00',
                          last_error='Capacity guard failed', private_extra='not for summary')
        return self.body | {'action': 'reoffer'}

    def test_failed_offer_never_silently_rearms_after_expiry_or_changed_release(self):
        self.failed();self.store.update(expires_at=0)
        for target, image, base in [('2026.10.0', IMAGE, 'changed-base'),
                                    ('2026.10.1', IMAGE.replace('2026.10.0', '2026.10.1'), 'old-image')]:
            self.store.offer('2026.9.1', target, image, base, 'url')
            self.assertEqual(self.store.read()['stage'], 'failed')

    def test_reoffer_requires_new_approval_and_preserves_only_previous_summary(self):
        body = self.failed();verified = self.store.reoffer_snapshot(body)
        fresh = self.store.reoffer(body, verified)
        self.assertEqual(fresh['stage'], 'available')
        self.assertNotEqual(fresh['candidate_id'], body['candidate_id'])
        self.assertIsNone(fresh['approved_at'])
        self.assertEqual(fresh['previous_attempt']['candidate_id'], body['candidate_id'])
        self.assertNotIn('private_extra', fresh['previous_attempt'])
        with self.assertRaises(RuntimeError):self.store.approve(self.body)
        with self.assertRaises(RuntimeError):self.store.reoffer(body, verified)
        self.store.approve(self.body | {'candidate_id': fresh['candidate_id']})
        self.assertEqual(Store(self.store.path).read()['stage'], 'preparing')

    def test_concurrent_reoffers_accept_only_one_verified_snapshot(self):
        body = self.failed();verified = self.store.reoffer_snapshot(body)
        def attempt(_):
            try:self.store.reoffer(body, verified);return True
            except RuntimeError:return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 1)

    def test_reoffer_rejects_changed_or_unsafe_state(self):
        body = self.failed();verified = self.store.reoffer_snapshot(body)
        self.store.update(check_error='changed')
        with self.assertRaises(RuntimeError):self.store.reoffer(body, verified)
        for change in ({'source_mutation_intent': {'base_commit': 'x'}}, {'source_push_attempted': True},
                       {'helm_upgrade_attempted': True}, {'backup_schedule_was_suspended': False},
                       {'backup_create_attempted': True}, {'stage': 'recovery_required'}):
            self.store.data = dict(verified, **change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):self.store.reoffer_snapshot(body)

from aiohttp.test_utils import TestClient, TestServer
from server import create_app, recover_interrupted

class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'status.json')
        d=self.store.offer('2026.9.1','2026.10.0',IMAGE,'old','url')
        self.body={'action':'install','target_version':'2026.10.0','candidate_id':d['candidate_id']}
        self.calls=[]
        self.runtime=Mock()
        self.client=TestClient(TestServer(create_app(self.store,self.runtime,{},'a'*48,worker=lambda *args:self.calls.append(True))))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()
    async def test_restart_catches_unfinished_schedule_even_after_terminal_status(self):
        for stage in ('failed', 'succeeded', 'backup'):
            self.store.update(stage=stage, backup_schedule_was_suspended=False,
                              backup_schedule_restored=False)
            recover_interrupted(self.store)
            self.assertEqual(self.store.read()['stage'], 'recovery_required')

    async def test_authentication_and_payload_rejections_start_nothing(self):
        self.assertEqual((await self.client.post('/approve',json=self.body)).status,403)
        headers={'Authorization':'Bearer '+'a'*48}
        self.assertEqual((await self.client.post('/approve',json={},headers=headers)).status,422)
        self.assertEqual((await self.client.post('/approve',json=self.body|{'candidate_id':'0'*32},headers=headers)).status,409)
        self.assertEqual(self.calls,[])
    async def test_exact_approval_returns_202_and_second_returns_409(self):
        headers={'Authorization':'Bearer '+'a'*48}
        self.assertEqual((await self.client.post('/approve',json=self.body,headers=headers)).status,202)
        self.assertEqual((await self.client.post('/approve',json=self.body,headers=headers)).status,409)
        await asyncio.sleep(.05)
        self.assertEqual(self.calls,[True])
        self.assertEqual(Store(self.store.path).read()['stage'],'preparing')

    async def test_operator_reoffer_does_not_start_worker_or_replay_approval(self):
        self.store.approve(self.body);self.store.update(stage='failed')
        body=self.body|{'action':'reoffer'};headers={'Authorization':'Bearer '+'a'*48}
        self.assertEqual((await self.client.post('/reoffer',json=body)).status,403)
        self.assertEqual((await self.client.post('/reoffer',json=body,headers={'Authorization':'Bearer '+'b'*48})).status,403)
        self.assertEqual((await self.client.post('/reoffer',json=body|{'extra':1},headers=headers)).status,422)
        response=await self.client.post('/reoffer',json=body,headers=headers)
        self.assertEqual(response.status,200);fresh=await response.json()
        self.assertNotEqual(fresh['candidate_id'],body['candidate_id'])
        self.assertEqual(self.calls,[]);self.runtime.validate_reoffer.assert_called_once()
        self.assertEqual((await self.client.post('/approve',json=self.body,headers=headers)).status,409)
        self.assertEqual((await self.client.post('/reoffer',json=body,headers=headers)).status,409)

    async def test_failed_live_reoffer_check_is_sanitized_and_preserves_failure(self):
        self.store.approve(self.body);self.store.update(stage='failed')
        self.runtime.validate_reoffer.side_effect=RuntimeError('sensitive private exception')
        response=await self.client.post('/reoffer',json=self.body|{'action':'reoffer'},headers={'Authorization':'Bearer '+'a'*48})
        self.assertEqual(response.status,409)
        self.assertNotIn('sensitive',await response.text())
        self.assertEqual(self.store.read()['stage'],'failed');self.assertEqual(self.calls,[])

if __name__=='__main__': unittest.main()
