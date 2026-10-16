"""Approval updates journal ambiguous writes and never downgrade migrated HA."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core_updates'))
try:
    import runtime
    import worker
finally:
    sys.path.pop(0)

RUN_ID = 'a' * 32
BASE = 'ghcr.io/home-assistant/home-assistant:2026.9.1@sha256:' + 'b' * 64
TARGET = 'ghcr.io/home-assistant/home-assistant:2026.10.0@sha256:' + 'c' * 64
COMMIT = 'd' * 40


class FakeStore:
    def __init__(self, events):
        self.events = events
        self.data = {'stage': 'preparing', 'candidate_id': RUN_ID, 'installed_version': '2026.9.1',
                     'target_version': '2026.10.0', 'base_image': BASE, 'image': TARGET,
                     'approved_at': datetime.now(timezone.utc).isoformat()}

    def read(self):
        return deepcopy(self.data)

    def update(self, **changes):
        self.events.append(('persist', deepcopy(changes)))
        self.data.update(changes)
        return self.read()


class FakeRuntime:
    namespace = 'homeassistant'

    def __init__(self, store, events):
        self.store, self.events = store, events
        self.current_image = BASE
        self.cron = {'metadata': {'resourceVersion': '1'}, 'spec': {'suspend': False}}
        self.job_record = None
        self.receipt = {}
        self.create_error = False
        self.wait_error = None
        self.preflight_error = False
        self.backup_image_error = False
        self.bad_receipt = False
        self.idle_calls = 0
        self.idle_failure = None
        self.guard_error = False
        self.restore_error = False
        self.patch_calls = 0
        self.failure_diagnostic = None

    def wait_update_visible(self, candidate_id):
        self.events.append(('visible_guard', candidate_id))
        if self.guard_error:
            raise RuntimeError('Active guard was not visible in HA')

    def healthy(self, expected):
        self.events.append(('healthy', expected))
        return expected

    def image(self):
        return self.current_image

    def maintenance_idle(self):
        self.idle_calls += 1
        if self.idle_calls == self.idle_failure:
            raise RuntimeError('A different backup started')

    def get(self, kind, name):
        if kind == 'cronjobs':
            return deepcopy(self.cron)
        if kind == 'jobs' and self.job_record is not None:
            return deepcopy(self.job_record)
        raise RuntimeError('Cannot determine Job status')

    def path(self, kind, name):
        return kind + '/' + name

    def request(self, path, method, payload):
        self.patch_calls += 1
        assert self.store.data['stage'] in runtime.ACTIVE
        if payload['spec']['suspend']:
            assert self.store.data['backup_job'] == 'ha-recorder-core-' + RUN_ID[:12]
            assert 'backup_schedule_was_suspended' in self.store.data
        self.events.append(('suspend', payload['spec']['suspend']))
        if self.patch_calls > 1 and self.restore_error:
            raise RuntimeError('Restore schedule response unknown')
        self.cron['spec']['suspend'] = payload['spec']['suspend']
        self.cron['metadata']['resourceVersion'] = str(int(self.cron['metadata']['resourceVersion']) + 1)

    def backup_spec(self, run_id):
        self.events.append(('backup_spec', run_id))
        return {'candidate': run_id}

    def job(self, name, spec):
        assert self.store.data['backup_create_attempted'] is True
        self.events.append(('backup_create', name))
        self.job_record = {'metadata': {'uid': 'job-uid'}, 'status': {'active': 1}}
        if self.create_error:
            raise RuntimeError('Connection lost after create')
        return deepcopy(self.job_record)

    def wait_job(self, name, seconds):
        self.events.append(('backup_wait', name))
        if self.wait_error == 'active':
            raise RuntimeError('Backup deadline expired')
        if self.wait_error == 'failed':
            self.job_record['status'] = {'conditions': [{'type': 'Failed', 'status': 'True'}]}
            raise RuntimeError('Backup failed')
        if self.wait_error == 'replaced':
            self.job_record['metadata']['uid'] = 'another-job-uid'
        self.job_record['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        self.receipt = {'update_run_id': 'e' * 32 if self.bad_receipt else RUN_ID,
                        'date': datetime.now(timezone.utc).isoformat(),
                        'backup_id': 'exact-native-backup', 'agents': ['synology']}
        return deepcopy(self.job_record)

    def backup_failure(self, name, uid, run_id, started_at):
        return self.failure_diagnostic

    def preflight(self, name, image):
        self.events.append(('preflight', name))
        assert self.store.data['backup_id'] == 'exact-native-backup'
        if self.preflight_error:
            raise RuntimeError('Target configuration rejected')

    def update_backup_image(self, image):
        self.events.append(('backup_image', image))
        if self.backup_image_error:
            raise RuntimeError('Backup image patch unconfirmed')


class WorkerFlowTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.store = FakeStore(self.events)
        self.rt = FakeRuntime(self.store, self.events)
        self.push_error = False
        self.helm_error = False
        self.source = Mock()
        self.source.prepare.side_effect = self.prepare
        self.source.pin.side_effect = self.pin
        self.source.push.side_effect = self.push
        self.commands = []

    def prepare(self):
        self.events.append(('source_prepare', None))
        return 'f' * 40

    def pin(self, image, approval):
        assert self.store.data['source_mutation_intent']['candidate_id'] == RUN_ID
        self.events.append(('source_pin', image))
        return COMMIT

    def push(self, commit):
        assert self.store.data['source_commit'] == commit
        assert self.store.data['source_push_attempted'] is True
        self.events.append(('source_push', commit))
        if self.push_error:
            raise RuntimeError('Remote result unknown')

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[1] == 'history':
            return json.dumps([{'status': 'deployed', 'chart': 'home-assistant-0.3.80', 'revision': 4}])
        self.assertEqual(args[1], 'upgrade')
        self.assertTrue(self.store.data['helm_upgrade_attempted'])
        self.events.append(('helm_upgrade', None))
        if self.helm_error:
            raise RuntimeError('Helm timed out after starting target')
        self.rt.current_image = TARGET
        return ''

    def run_worker(self):
        receipt_path = Mock()
        receipt_path.read_text.side_effect = lambda: json.dumps(self.rt.receipt)
        with patch.object(worker, 'Source', return_value=self.source), \
             patch.object(worker, 'chart_file', return_value='/test/chart.tgz'), \
             patch.object(worker, 'helm_env', return_value={}), \
             patch.object(worker, 'command', side_effect=self.command), \
             patch.object(worker, 'Path', return_value=receipt_path), \
             patch.object(worker.time, 'sleep'):
            worker.run(self.store, self.rt, {'chart_version': '0.3.80', 'health_timeout_seconds': 5})

    def assert_no_deploy(self):
        self.assertFalse(any(args[1] in {'upgrade', 'rollback'} for args in self.commands))

    def test_success_journals_before_writes_and_preflights_after_fresh_backup(self):
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'succeeded')
        names = [event[0] for event in self.events]
        for before, after in [('backup_wait', 'preflight'), ('preflight', 'source_pin'),
                              ('source_pin', 'source_push'), ('source_push', 'helm_upgrade')]:
            self.assertLess(names.index(before), names.index(after))
        self.assertIn(('backup_spec', RUN_ID), self.events)
        self.assertTrue(self.store.data['backup_schedule_restored'])
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assertEqual(self.store.data['source_push_status'], 'confirmed')
        self.assertEqual(self.events[0], ('visible_guard', RUN_ID))
        terminal = [index for index, (event, data) in enumerate(self.events)
                    if event == 'persist' and data.get('stage') == 'succeeded']
        restore = max(index for index, (event, _) in enumerate(self.events) if event == 'suspend')
        self.assertEqual(len(terminal), 1)
        self.assertGreater(terminal[0], restore)

    def test_guard_not_published_in_ha_prevents_all_mutations(self):
        self.rt.guard_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.source.prepare.assert_not_called()
        self.assertEqual(self.rt.patch_calls, 0)
        self.assert_no_deploy()

    def test_schedule_restore_failure_never_announces_success(self):
        self.rt.restore_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertFalse(any(event == 'persist' and data.get('stage') == 'succeeded'
                             for event, data in self.events))
        self.assertFalse(self.store.data['backup_schedule_restored'])

    def test_ambiguous_backup_create_stays_suspended_and_requires_recovery(self):
        self.rt.create_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertTrue(self.rt.cron['spec']['suspend'])
        self.assertFalse(self.store.data['backup_schedule_restored'])
        self.assert_no_deploy()

    def test_active_backup_timeout_stays_suspended(self):
        self.rt.wait_error = 'active'
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertTrue(self.rt.cron['spec']['suspend'])
        self.assert_no_deploy()

    def test_replaced_job_with_same_name_does_not_prove_original_is_terminal(self):
        self.rt.wait_error = 'replaced'
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertTrue(self.rt.cron['spec']['suspend'])
        self.assert_no_deploy()

    def test_proven_failed_backup_can_restore_schedule(self):
        self.rt.wait_error = 'failed'
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assertTrue(self.store.data['backup_schedule_restored'])
        self.assert_no_deploy()

    def test_capacity_failure_is_structured_and_does_not_deploy(self):
        self.rt.wait_error = 'failed'
        self.rt.failure_diagnostic = {'required_bytes': 10 * 1024**3, 'free_bytes': 9 * 1024**3,
                                      'shortfall_bytes': 1024**3}
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.assertEqual(self.store.data['backup_failure'], self.rt.failure_diagnostic)
        self.assertIn('short 1.00 GiB', self.store.data['last_error'])
        self.assertTrue(self.store.data['backup_schedule_restored'])
        self.assert_no_deploy()

    def test_no_job_created_can_restore_original_schedule(self):
        self.rt.idle_failure = 2  # after suspension, before constructing/creating the job
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assertNotIn('backup_create', [event[0] for event in self.events])
        self.assert_no_deploy()

    def test_new_but_different_run_receipt_cannot_authorize_upgrade(self):
        self.rt.bad_receipt = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.assertIn('different update run', self.store.data['last_error'])
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assert_no_deploy()

    def test_preflight_failure_happens_after_backup_without_source_mutation(self):
        self.rt.preflight_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'failed')
        self.source.pin.assert_not_called()
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assert_no_deploy()
        terminal = next(index for index, (event, data) in enumerate(self.events)
                        if event == 'persist' and data.get('stage') == 'failed')
        restore = max(index for index, (event, _) in enumerate(self.events) if event == 'suspend')
        self.assertGreater(terminal, restore)

    def test_ambiguous_push_keeps_commit_and_never_deploys_or_reverts(self):
        self.push_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertEqual(self.store.data['source_commit'], COMMIT)
        self.assertEqual(self.store.data['source_push_status'], 'pending')
        self.source.restore_pin.assert_not_called()
        self.assertFalse(self.rt.cron['spec']['suspend'])
        self.assert_no_deploy()

    def test_helm_timeout_never_downgrades_or_resumes_old_backup_image(self):
        self.helm_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertTrue(self.rt.cron['spec']['suspend'])
        self.source.restore_pin.assert_not_called()
        self.assertFalse(any(args[1] == 'rollback' for args in self.commands))

    def test_unverified_new_backup_image_keeps_schedule_suspended(self):
        self.rt.backup_image_error = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'recovery_required')
        self.assertTrue(self.rt.cron['spec']['suspend'])

    def test_initially_suspended_schedule_is_preserved_on_success(self):
        self.rt.cron['spec']['suspend'] = True
        self.run_worker()
        self.assertEqual(self.store.data['stage'], 'succeeded')
        self.assertTrue(self.rt.cron['spec']['suspend'])


class SourcePinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        (self.path / 'helm').mkdir()
        (self.path / 'native_backup').mkdir()
        self.values = ROOT.joinpath('helm/values-pajikos.yaml').read_text()
        self.backup = ROOT.joinpath('native_backup/cronjob.yaml').read_text()
        (self.path / 'helm/values-pajikos.yaml').write_text(self.values)
        (self.path / 'native_backup/cronjob.yaml').write_text(self.backup)
        self.source = worker.Source({})
        self.source.path = self.path
        self.source.approval_path = self.path.parent / (self.path.name + '-receipts')
        self.addCleanup(shutil.rmtree, self.source.approval_path, True)
        self.source.git = Mock(return_value=COMMIT)
        self.approval = {'candidate_id': RUN_ID, 'target_version': '2026.10.0',
                         'image': TARGET, 'base_image': BASE, 'approved_at': 'approved',
                         'backup_id': 'backup-id'}

    def test_local_pin_changes_ha_and_two_backup_images_but_never_mysql_or_push(self):
        self.assertEqual(self.source.pin(TARGET, self.approval), COMMIT)
        values = yaml.safe_load((self.path / 'helm/values-pajikos.yaml').read_text())
        self.assertEqual(values['image']['tag'], TARGET.split('home-assistant:', 1)[1])
        original = next(item for item in yaml.safe_load_all(self.backup) if item['kind'] == 'CronJob')
        actual_text = (self.path / 'native_backup/cronjob.yaml').read_text()
        actual = next(item for item in yaml.safe_load_all(actual_text) if item['kind'] == 'CronJob')
        before = original['spec']['jobTemplate']['spec']['template']['spec']
        after = actual['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(after['initContainers'][0]['image'], TARGET)
        self.assertEqual(after['containers'][0]['image'], TARGET)
        self.assertEqual(after['initContainers'][1], before['initContainers'][1])
        self.assertIn('# Official MySQL', actual_text)
        self.assertIn('&security', actual_text)
        self.assertFalse(any(call.args[0] == 'push' for call in self.source.git.call_args_list))
        self.source.git.assert_any_call('add', 'helm/values-pajikos.yaml', 'native_backup/cronjob.yaml')
        receipt = self.source.approval_path / (RUN_ID + '.json')
        self.assertEqual(json.loads(receipt.read_text()), self.approval)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.approval_path.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.path / 'core_updates/approvals').exists())

    def test_existing_receipt_is_not_replaced_or_committed(self):
        self.source.pin(TARGET, self.approval)
        receipt = self.source.approval_path / (RUN_ID + '.json')
        original = receipt.read_bytes()
        self.source.git.reset_mock()
        with self.assertRaises(FileExistsError):
            self.source.pin(TARGET, {**self.approval, 'backup_id': 'different-private-id'})
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(list(self.source.approval_path.iterdir()), [receipt])
        self.source.git.assert_not_called()

    def test_real_git_commit_contains_only_image_pin_files(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.path,
                                           env=self.source.env, text=True).strip()
        git('init', '--quiet')
        git('config', 'commit.gpgsign', 'false')
        git('config', 'core.hooksPath', '/dev/null')
        git('add', 'helm/values-pajikos.yaml', 'native_backup/cronjob.yaml')
        git('commit', '--quiet', '-m', 'Synthetic baseline')
        self.source.git = git
        commit = self.source.pin(TARGET, self.approval)
        self.assertEqual(set(git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines()),
                         {'helm/values-pajikos.yaml', 'native_backup/cronjob.yaml'})
        self.assertEqual(git('status', '--porcelain'), '')
        self.assertNotIn('approvals/', git('ls-tree', '-r', '--name-only', commit))
        self.assertEqual(json.loads((self.source.approval_path / (RUN_ID + '.json')).read_text()),
                         self.approval)

    def test_public_or_checkout_receipt_directory_blocks_source_mutation(self):
        self.source.approval_path.mkdir(mode=0o755)
        self.source.approval_path.chmod(0o755)
        for directory in (self.source.approval_path, self.path / 'receipts'):
            with self.subTest(directory=directory):
                self.source.approval_path = directory
                with self.assertRaises(RuntimeError):
                    self.source.pin(TARGET, self.approval)
                self.assertEqual((self.path / 'helm/values-pajikos.yaml').read_text(), self.values)
                self.assertEqual((self.path / 'native_backup/cronjob.yaml').read_text(), self.backup)
                self.source.git.assert_not_called()

    def test_symlinked_receipt_directory_is_rejected(self):
        target = self.path / 'private-target'
        target.mkdir(mode=0o700)
        self.source.approval_path.symlink_to(target, target_is_directory=True)
        self.addCleanup(self.source.approval_path.unlink, missing_ok=True)
        with self.assertRaises((RuntimeError, OSError)):
            self.source.pin(TARGET, self.approval)
        self.assertEqual(list(target.iterdir()), [])
        self.source.git.assert_not_called()

    def test_ambiguous_backup_container_refuses_all_source_edits(self):
        broken = self.backup.replace('name: prepare', 'name: unexpected')
        (self.path / 'native_backup/cronjob.yaml').write_text(broken)
        with self.assertRaises(RuntimeError):
            self.source.pin(TARGET, self.approval)
        self.assertEqual((self.path / 'helm/values-pajikos.yaml').read_text(), self.values)
        self.source.git.assert_not_called()


class RuntimeJobTests(unittest.TestCase):
    def test_active_pods_are_not_proven_terminal(self):
        self.assertIsNone(runtime.terminal_job_state({'status': {'active': 1,
            'conditions': [{'type': 'Failed', 'status': 'True'}]}}))
        self.assertIsNone(runtime.terminal_job_state({'status': {'succeeded': 1}}))

    def test_backup_spec_correlates_exact_run_without_changing_live_template(self):
        cron = next(item for item in yaml.safe_load_all(ROOT.joinpath('native_backup/cronjob.yaml').read_text()) if item['kind'] == 'CronJob')
        before = deepcopy(cron)
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt.get = Mock(return_value=cron)
        spec = rt.backup_spec(RUN_ID)
        pod = spec['template']['spec']
        self.assertIn({'name': 'HA_UPDATE_RUN_ID', 'value': RUN_ID}, pod['containers'][0]['env'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(cron, before)
        self.assertEqual(pod['initContainers'][1], before['spec']['jobTemplate']['spec']['template']['spec']['initContainers'][1])

    def test_preflight_mounts_config_subpath_readonly_and_without_hostnetwork(self):
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt.settings = {'config_pvc': 'real-pvc', 'config_subpath': 'config'}
        rt.get = Mock(return_value={'spec': {'template': {'spec': {
            'nodeSelector': {'kubernetes.io/hostname': 'example-ha-node'},
            'containers': [{'name': 'home-assistant', 'volumeMounts': []}],
        }}}})
        rt.job = Mock()
        rt.wait_job = Mock()
        rt.preflight('preflight', TARGET)
        pod = rt.job.call_args.args[1]['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod.get('hostNetwork', False))
        self.assertEqual(pod['volumes'][0]['persistentVolumeClaim'], {'claimName': 'real-pvc', 'readOnly': True})
        self.assertEqual(pod['containers'][0]['volumeMounts'][0]['subPath'], 'config')
        self.assertTrue(pod['containers'][0]['volumeMounts'][0]['readOnly'])

    def test_preflight_placeholders_require_an_actual_external_mount(self):
        rt = runtime.Runtime.__new__(runtime.Runtime)
        rt.settings = {'preflight_empty_dirs': ['/share/ssd/Music']}
        live = {'containers': [{'name': 'home-assistant', 'volumeMounts': [{'name': 'shared', 'mountPath': '/share'}]}]}
        rt.get = Mock(return_value={'spec': {'template': {'spec': live}}})
        rt.job = Mock();rt.wait_job = Mock()
        rt.preflight('preflight', TARGET)
        pod = rt.job.call_args.args[1]['template']['spec']
        env = {item['name']: item['value'] for item in pod['containers'][0]['env']}
        self.assertEqual(json.loads(env['HA_PREFLIGHT_PLACEHOLDERS']), [{'path': '/share/ssd/Music', 'mount': '/share'}])
        self.assertNotIn('shared', [item['name'] for item in pod['volumes']])
        live['containers'][0]['volumeMounts'] = []
        rt.job.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'external mount'):
            rt.preflight('preflight', TARGET)
        rt.job.assert_not_called()


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.rt = runtime.Runtime.__new__(runtime.Runtime)
        self.rt.namespace = 'homeassistant'
        self.states = [
            {'entity_id': 'sensor.backup_backup_manager_state', 'state': 'idle'},
            {'entity_id': 'script.hacs_maintenance_backup', 'state': 'off', 'attributes': {'current': 0}},
            {'entity_id': 'script.hacs_maintenance_install', 'state': 'off', 'attributes': {'current': 0}},
            {'entity_id': 'automation.renamed_weekly_task', 'state': 'on',
             'attributes': {'id': 'ha_homelab_hacs_maintenance_update', 'current': 0}},
        ]
        self.rt.ha = Mock(side_effect=lambda *args, **kwargs: self.states)
        self.rt.request = Mock(return_value={'items': []})

    def test_missing_or_unavailable_backup_sensor_fails_closed(self):
        self.states.pop(0)
        with self.assertRaisesRegex(RuntimeError, 'backup manager'):
            self.rt.maintenance_idle()
        self.states.insert(0, {'entity_id': 'sensor.backup_backup_manager_state', 'state': 'unavailable'})
        with self.assertRaisesRegex(RuntimeError, 'backup manager'):
            self.rt.maintenance_idle()

    def test_numeric_zero_update_progress_is_busy_but_false_is_idle(self):
        update = {'entity_id': 'update.other_firmware', 'state': 'on', 'attributes': {'in_progress': 0}}
        self.states.append(update)
        with self.assertRaisesRegex(RuntimeError, 'Another update'):
            self.rt.maintenance_idle()
        update['attributes']['in_progress'] = False
        self.rt.maintenance_idle()

    def test_renamed_running_hacs_coordinator_is_identified_by_stable_id(self):
        self.states[-1]['attributes']['current'] = 1
        with self.assertRaisesRegex(RuntimeError, 'HACS maintenance'):
            self.rt.maintenance_idle()

    def test_missing_or_unknown_hacs_workers_and_coordinator_fail_closed(self):
        for index in (1, 2, 3):
            with self.subTest(index=index):
                saved = self.states.pop(index)
                with self.assertRaises(RuntimeError):
                    self.rt.maintenance_idle()
                self.states.insert(index, saved)
        self.states[2]['attributes'].pop('current')
        with self.assertRaises(RuntimeError):
            self.rt.maintenance_idle()

    def test_pending_recorder_job_blocks_but_old_terminal_and_unrelated_jobs_do_not(self):
        jobs = [
            {'metadata': {'name': 'ha-recorder-old'}, 'status': {'conditions': [{'type': 'Failed', 'status': 'True'}]}},
            {'metadata': {'name': 'unrelated-pending'}, 'status': {}},
        ]
        self.rt.request.return_value = {'items': jobs}
        self.rt.maintenance_idle()
        jobs.append({'metadata': {'name': 'ha-recorder-pending'}, 'status': {}})
        with self.assertRaisesRegex(RuntimeError, 'pending or running'):
            self.rt.maintenance_idle()

    def test_publish_barrier_requires_this_candidate_active_stage_and_boolean_progress(self):
        ready = {'state': 'on', 'attributes': {'candidate_id': RUN_ID, 'stage': 'preparing', 'in_progress': True}}
        stale = deepcopy(ready)
        stale['attributes']['candidate_id'] = 'f' * 32
        self.rt.ha = Mock(side_effect=[stale, ready])
        with patch.object(runtime.time, 'sleep'):
            self.rt.wait_update_visible(RUN_ID)
        self.assertEqual(self.rt.ha.call_count, 2)
        self.assertEqual(self.rt.ha.call_args.args[0], '/api/states/update.home_assistant_core')

    def test_publish_barrier_expires_without_enabling_mutations(self):
        self.rt.ha = Mock(return_value={'state': 'on', 'attributes': {
            'candidate_id': RUN_ID, 'stage': 'available', 'in_progress': False}})
        clock = iter(range(100))
        with patch.object(runtime.time, 'monotonic', side_effect=lambda: next(clock)), patch.object(runtime.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'active maintenance guard'):
                self.rt.wait_update_visible(RUN_ID, seconds=3)


if __name__ == '__main__':
    unittest.main()
