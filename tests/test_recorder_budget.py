"""Backpressure preserves a durable stop and never replays uncertain cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core_updates'))
from recorder_budget import BudgetGuard, DAY, PAUSE, TABLES, measured_bytes, physical_usage, recorder_url


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'guard.json'
        self.now = time.time()
        self.snapshot = {'measured_at': self.now, 'data_index_bytes': 1_000_000_000,
                         'physical_bytes': None, 'physical_status': 'unknown',
                         'oldest': dict.fromkeys(TABLES, self.now - 2 * DAY),
                         'latest_state_age_seconds': 2}
        self.rt = Mock(ha_url='http://ha.invalid', ha_token='test-only')
        self.rt.ha.return_value = {'state': 'RUNNING'}
        self.store = Mock()
        self.store.lock = threading.RLock()
        self.store.read.return_value = {'stage': 'idle'}
        self.settings = {'schema': 'homeassistant', 'purge_qualified': True}
        self.guard = self.new_guard()

    def new_guard(self):
        guard = BudgetGuard(self.settings, self.rt, self.store, self.path,
                            collector=lambda _: deepcopy(self.snapshot))
        guard.service = Mock()
        return guard

    def state(self):
        return json.loads(self.path.read_text())

    def services(self, guard=None):
        return [(c.args[0], c.args[1]) for c in (guard or self.guard).service.call_args_list]

    def test_unknown_dispatch_survives_restart_without_replay(self):
        def uncertain(domain, name, payload):
            if name == 'purge':
                self.assertEqual(self.state()['pending']['phase'], 'dispatching')
                raise TimeoutError('sensitive URL must not be serialized')
        self.guard.service.side_effect = uncertain
        self.guard.run_once()
        self.assertEqual(self.state()['pending']['phase'], 'outcome_unknown')
        restarted = self.new_guard()
        restarted.run_once()
        self.assertNotIn(('recorder', 'purge'), self.services(restarted))
        self.assertNotIn('sensitive', self.path.read_text())

    def test_pause_intent_is_durable_before_disable_and_no_automatic_enable(self):
        self.snapshot['data_index_bytes'] = PAUSE
        def check(domain, name, payload):
            if name == 'disable':
                self.assertEqual(self.state()['pause']['phase'], 'dispatching')
        self.guard.service.side_effect = check
        self.guard.run_once()
        self.assertEqual(self.state()['pause']['phase'], 'accepted')
        self.assertNotIn(('recorder', 'purge'), self.services())
        self.snapshot['data_index_bytes'] = 1_000_000_000
        restarted = self.new_guard()
        restarted.run_once()
        self.assertIn(('recorder', 'disable'), self.services(restarted))
        self.assertNotIn(('recorder', 'enable'), self.services(restarted))

    def test_pause_is_reapplied_even_when_database_measurement_fails(self):
        self.snapshot['data_index_bytes'] = PAUSE
        self.guard.run_once()
        restarted = self.new_guard()
        restarted.collector = Mock(side_effect=ConnectionError('secret'))
        restarted.run_once()
        self.assertIn(('recorder', 'disable'), self.services(restarted))
        self.assertIn('pause', self.state())
        self.assertNotIn('secret', self.path.read_text())

    def test_physical_allocation_can_trigger_pause_and_free_space_is_not_subtracted(self):
        self.snapshot['physical_bytes'] = PAUSE + 100
        self.snapshot['data_free_row_sum_bytes'] = 5_000_000_000
        self.assertEqual(measured_bytes(self.snapshot), PAUSE + 100)
        self.guard.run_once()
        self.assertIn(('recorder', 'disable'), self.services())

    def test_fixed_cutoff_completion_does_not_require_catching_a_moving_day(self):
        self.guard.run_once()
        cutoff = self.state()['pending']['cutoff']
        self.snapshot['oldest'] = dict.fromkeys(TABLES, cutoff + 1)
        self.guard.run_once()
        self.assertNotIn('pending', self.state())
        self.assertEqual(self.state()['last_completed']['cutoff'], cutoff)

    def test_unqualified_purge_or_maintenance_never_queues_cleanup(self):
        self.settings['purge_qualified'] = False
        self.guard.run_once()
        self.assertNotIn(('recorder', 'purge'), self.services())
        self.settings['purge_qualified'] = True
        self.store.read.return_value = {'stage': 'backup'}
        self.guard.run_once()
        self.assertNotIn(('recorder', 'purge'), self.services())
        self.store.read.return_value = {'stage': 'idle'}
        self.rt.maintenance_idle.side_effect = RuntimeError('busy')
        self.guard.run_once()
        self.assertNotIn(('recorder', 'purge'), self.services())

    def test_stale_measurement_never_starts_purge(self):
        self.snapshot['measured_at'] = self.now - 61
        self.guard.run_once()
        self.assertIn('measurement_error', self.state())
        self.assertNotIn(('recorder', 'purge'), self.services())

    def test_purge_does_not_request_repack_or_filter_and_has_private_receipt(self):
        self.guard.run_once()
        calls = [c for c in self.guard.service.call_args_list if c.args[1] == 'purge']
        self.assertEqual(calls[0].args[2], {'keep_days': 1, 'repack': False, 'apply_filter': False})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.state()['hard_quota_enforced'])

    def test_foreign_target_state_is_not_reused(self):
        self.guard.run_once()
        self.settings['schema'] = 'other'
        foreign = self.new_guard()
        previous = self.path.read_bytes()
        foreign.run_once()
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.services(foreign), [('persistent_notification', 'create')])

    def test_corrupt_receipt_is_preserved_and_reported_in_ha(self):
        self.path.write_bytes(b'not JSON')
        self.guard.run_once()
        self.assertEqual(self.path.read_bytes(), b'not JSON')
        self.assertEqual(self.services(), [('persistent_notification', 'create')])
        with self.assertRaises(RuntimeError):
            self.guard.assert_update_idle()

    def test_update_admission_refuses_unknown_purge_until_completion(self):
        self.guard.run_once()
        with self.store.lock, self.assertRaises(RuntimeError):
            self.guard.assert_update_idle()
        self.snapshot['oldest'] = dict.fromkeys(TABLES, self.now)
        self.guard.run_once()
        with self.store.lock:
            self.guard.assert_update_idle()

    def test_update_started_during_preflight_prevents_purge_admission(self):
        self.rt.maintenance_idle.side_effect = lambda: self.store.read.configure_mock(return_value={'stage': 'preparing'})
        self.guard.run_once()
        self.assertNotIn(('recorder', 'purge'), self.services())
        self.assertNotIn('pending', self.state())

    def test_pending_stall_notifies_and_never_enqueues_another_purge(self):
        self.guard.run_once()
        state = self.state()
        state['pending']['last_progress_at'] = self.now - 1900
        self.path.write_text(json.dumps(state))
        self.guard.service.reset_mock()
        self.guard.run_once()
        self.assertIn(('persistent_notification', 'create'), self.services())
        self.assertNotIn(('recorder', 'purge'), self.services())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'integrations').mkdir()
        (self.root / 'configuration.yaml').write_text('recorder: !include integrations/recorder.yaml\n')
        (self.root / 'integrations/recorder.yaml').write_text('db_url: !secret recorder_db_url\n')
        (self.root / 'secrets.yaml').write_text('recorder_db_url: mysql://test.invalid/homeassistant\n')

    def test_only_reviewed_include_and_secret_are_resolved(self):
        self.assertEqual(recorder_url(self.root), 'mysql://test.invalid/homeassistant')
        (self.root / 'integrations/recorder.yaml').write_text('db_url: !secret different\n')
        with self.assertRaises(ValueError):
            recorder_url(self.root)

    def test_duplicate_field_and_escaping_include_are_rejected(self):
        (self.root / 'secrets.yaml').write_text('recorder_db_url: first\nrecorder_db_url: second\n')
        with self.assertRaises(ValueError):
            recorder_url(self.root)
        (self.root / 'configuration.yaml').write_text('recorder: !include ../outside.yaml\n')
        with self.assertRaises(ValueError):
            recorder_url(self.root)


class StorageProbeTests(unittest.TestCase):
    def test_all_call_results_are_drained_before_connection_reuse(self):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.description = [('schema_name',), ('allocated_bytes',), ('file_bytes',)]
        cursor.fetchall.side_effect = [[('homeassistant', 200, 180)], [], []]
        cursor.nextset.side_effect = [True, True, None]
        result = physical_usage(connection, 'homeassistant')
        self.assertEqual(result['physical_bytes'], 200)
        self.assertEqual(result['file_bytes'], 180)
        self.assertEqual(cursor.fetchall.call_count, 3)
        cursor.execute.assert_called_once_with('CALL `homeassistant`.recorder_storage_probe()')
        cursor.close.assert_called_once()

    def test_wrong_schema_or_unavailable_probe_cannot_claim_valid_physical_usage(self):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.description = [('schema_name',), ('allocated_bytes',), ('file_bytes',)]
        cursor.fetchall.return_value = [('another_schema', 200, 180)]
        cursor.nextset.return_value = None
        with self.assertRaises(ValueError):
            physical_usage(connection, 'homeassistant')
        cursor.close.assert_called_once()
        cursor.reset_mock()
        cursor.execute.side_effect = PermissionError('private details')
        with self.assertRaises(PermissionError):
            physical_usage(connection, 'homeassistant')
        cursor.close.assert_called_once()



if __name__ == '__main__':
    unittest.main()
