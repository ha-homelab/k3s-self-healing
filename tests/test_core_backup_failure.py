"""Only correlated numeric failure reports and safe terminal runs are trusted."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core_updates'))
import runtime

RUN = 'a' * 32
GIB = 1024**3
START = datetime.now(timezone.utc) - timedelta(minutes=10)


def report():
    measured = 1000
    allowance = (3 * measured + 1) // 2 + 128 * 1024**2
    required = 4 * GIB + 3 * GIB + allowance
    return {'schema_version': 1, 'code': 'native_backup_capacity', 'update_run_id': RUN,
            'observed_at': (START + timedelta(minutes=5)).isoformat(), 'sql_export_retained': True,
            'free_bytes': required - 123, 'reserve_bytes': 4 * GIB, 'required_bytes': required,
            'total_bytes': 54 * GIB, 'sql_bytes': 3 * GIB, 'archive_allowance_bytes': allowance,
            'measured_config_bytes': measured, 'reviewed_ceiling_bytes': 5 * GIB, 'shortfall_bytes': 123}


class FailureReportTest(unittest.TestCase):
    def setUp(self):
        self.rt = runtime.Runtime.__new__(runtime.Runtime);self.rt.namespace = 'homeassistant'
        self.job = {'metadata': {'uid': 'job-uid'}, 'status': {'conditions': [{'type': 'Failed', 'status': 'True'}]}}
        self.pod = {'metadata': {'ownerReferences': [{'kind': 'Job', 'name': 'backup-job', 'uid': 'job-uid', 'controller': True}]},
                    'status': {'containerStatuses': [{'name': 'native-backup', 'state': {'terminated': {
                        'exitCode': 1, 'message': json.dumps(report())}}}]}}
        self.rt.get = Mock(side_effect=lambda *args: deepcopy(self.job))
        self.rt.request = Mock(side_effect=lambda *args: {'items': [deepcopy(self.pod)]})

    def diagnostic(self):
        return self.rt.backup_failure('backup-job', 'job-uid', RUN, START)

    def test_exact_failed_job_report_is_preserved_without_logs(self):
        self.assertEqual(self.diagnostic(), report())
        self.assertIn('labelSelector=job-name%3Dbackup-job', self.rt.request.call_args.args[0])
        self.assertNotIn('log', self.rt.request.call_args.args[0])

    def test_replaced_job_wrong_owner_wrong_run_and_success_are_rejected(self):
        cases = [lambda: self.job['metadata'].update(uid='replacement'),
                 lambda: self.job['status'].update(active=1),
                 lambda: self.pod['metadata']['ownerReferences'][0].update(uid='wrong'),
                 lambda: self.pod['status']['containerStatuses'][0].update(name='other'),
                 lambda: self.pod['status']['containerStatuses'][0]['state']['terminated'].update(exitCode=0),
                 lambda: self.pod['status']['containerStatuses'][0]['state']['terminated'].update(message=json.dumps(report() | {'update_run_id': 'b' * 32}))]
        for change in cases:
            self.setUp();change()
            with self.subTest(change=change):self.assertIsNone(self.diagnostic())

    def test_raw_extra_invalid_and_uncorrelated_reports_never_escape(self):
        invalid = ['secret raw error', 'x' * 4097, json.dumps(report() | {'secret': 'private'}),
                   json.dumps(report() | {'shortfall_bytes': True}), json.dumps(report() | {'free_bytes': -1}),
                   json.dumps(report() | {'required_bytes': 2**64}), json.dumps(report() | {'shortfall_bytes': 124}),
                   json.dumps(report() | {'observed_at': (START - timedelta(seconds=1)).isoformat()}),
                   json.dumps(report() | {'observed_at': '2026-10-09T01:00:00'}),
                   json.dumps(report() | {'observed_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})]
        for message in invalid:
            with self.subTest(message=message[:30]):self.assertIsNone(runtime.capacity_diagnostic(message, RUN, START))

    def test_missing_or_ambiguous_evidence_falls_back_to_original_failure(self):
        self.rt.request.side_effect = RuntimeError('private API message')
        self.assertIsNone(self.diagnostic())
        self.rt.request.side_effect = lambda *args: {'items': [self.pod, self.pod]}
        self.assertIsNone(self.diagnostic())

    def test_optional_job_uid_must_match_the_actual_job(self):
        uid = '11111111-2222-3333-4444-555555555555'
        self.job['metadata']['uid'] = uid
        self.pod['metadata']['ownerReferences'][0]['uid'] = uid
        terminated = self.pod['status']['containerStatuses'][0]['state']['terminated']
        terminated['message'] = json.dumps(report() | {'update_job_uid': uid})
        self.assertEqual(self.rt.backup_failure('backup-job', uid, RUN, START), report())
        terminated['message'] = json.dumps(report() | {'update_job_uid': '00000000-2222-3333-4444-555555555555'})
        self.assertIsNone(self.rt.backup_failure('backup-job', uid, RUN, START))


class ReofferSafetyTest(unittest.TestCase):
    def setUp(self):
        self.rt = runtime.Runtime.__new__(runtime.Runtime)
        self.failed = {'stage': 'failed', 'candidate_id': RUN, 'approved_at': START.isoformat(),
            'installed_version': '2026.9.1', 'target_version': '2026.10.0', 'base_image': 'old-image',
            'image': 'ghcr.io/home-assistant/home-assistant:2026.10.0@sha256:' + 'f' * 64,
            'backup_schedule_was_suspended': False, 'backup_schedule_restored': True,
            'backup_create_attempted': True, 'backup_job': 'backup-job', 'backup_job_uid': 'job-uid',
            'backup_job_terminal': 'failed'}
        self.job = {'metadata': {'uid': 'job-uid'}, 'status': {'conditions': [{'type': 'Failed', 'status': 'True'}]}}
        self.cron = {'spec': {'suspend': False}}
        self.rt.healthy = Mock(return_value='2026.9.1');self.rt.image = Mock(return_value='old-image')
        self.rt.maintenance_idle = Mock()
        self.rt.get = Mock(side_effect=lambda kind, name: self.cron if kind == 'cronjobs' else self.job)

    def test_historical_failed_job_needs_no_new_diagnostic(self):
        self.rt.validate_reoffer(self.failed)
        self.rt.maintenance_idle.assert_called_once()
        self.rt.healthy.assert_called_once_with('2026.9.1')

    def test_live_change_missing_job_or_active_operation_rejects_reoffer(self):
        cases = [lambda: self.job['metadata'].update(uid='replacement'),
                 lambda: self.job['status'].update(active=1),
                 lambda: self.cron['spec'].update(suspend=True),
                 lambda: setattr(self.rt.image, 'return_value', 'new-image'),
                 lambda: setattr(self.rt.healthy, 'return_value', '2026.10.0'),
                 lambda: setattr(self.rt.get, 'side_effect', RuntimeError('missing job')),
                 lambda: setattr(self.rt.maintenance_idle, 'side_effect', RuntimeError('busy'))]
        for change in cases:
            self.setUp();change()
            with self.subTest(change=change), self.assertRaises(RuntimeError):self.rt.validate_reoffer(self.failed)

    def test_failure_before_any_mutation_can_be_reoffered(self):
        for key in list(self.failed):
            if key.startswith('backup_'):del self.failed[key]
        self.rt.validate_reoffer(self.failed)
        self.rt.get.assert_not_called()

    def test_active_preflight_rejects_reoffer(self):
        self.failed['preflight_job'] = 'preflight-job'
        original = self.rt.get.side_effect
        self.rt.get.side_effect = lambda kind, name: {'status': {'active': 1}} if name == 'preflight-job' else original(kind, name)
        with self.assertRaisesRegex(RuntimeError, 'preflight'):self.rt.validate_reoffer(self.failed)


if __name__ == '__main__':
    unittest.main()
