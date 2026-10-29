"""Synthetic kubectl boundaries verify all preflight gates precede mutations."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_install', ROOT / 'scripts/install-native-backup.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTest(unittest.TestCase):
    def setUp(self):
        cron = next(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
        self.image = cron['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image']
        self.ha = {
            'metadata': {'uid': 'synthetic-pod'},
            'spec': {
                'nodeName': 'example-node',
                'containers': [
                    {'name': 'sidecar', 'image': 'fixture'},
                    {'name': 'ha', 'image': self.image, 'volumeMounts': [
                        {'name': 'config', 'mountPath': '/config'}]},
                ],
                'volumes': [{'name': 'config', 'persistentVolumeClaim': {
                    'claimName': 'home-assistant-home-assistant-0'}}],
            },
            'status': {'phase': 'Running', 'containerStatuses': [
                {'name': 'ha', 'ready': True, 'state': {'running': {}}}]},
        }
        self.current = None
        self.node = {'status': {
            'addresses': [{'type': 'InternalIP', 'address': '192.0.2.10'}],
            'nodeInfo': {'architecture': 'arm64'},
        }}
        self.storage = {'device': '8:17', 'separate': False, 'mounted': False}
        self.jobs = []
        self.late_jobs = None
        self.allowed = True
        self.dry_run_error = False
        self.calls = []
        self.pod_reads = self.job_reads = 0
        self.argv = ['installer', '--kubeconfig', '/synthetic/kubeconfig',
                     '--node', 'example-node', '--database-cidr', '192.0.2.30/32']

    def check_output(self, args, **kwargs):
        self.assertEqual(kwargs.get('timeout'), 60)
        if 'node' in args:
            result = self.node
        elif 'pod' in args:
            self.pod_reads += 1
            result = self.current if self.pod_reads > 1 and self.current is not None else self.ha
        elif 'jobs' in args:
            self.job_reads += 1
            jobs = self.late_jobs if self.job_reads > 1 and self.late_jobs is not None else self.jobs
            result = {'items': jobs}
        elif 'exec' in args:
            self.assertEqual(args[args.index('-c') + 1], 'ha')
            result = self.storage
        else:
            self.fail('Unexpected read command')
        return json.dumps(result).encode()

    def kubectl_run(self, args, **kwargs):
        self.assertEqual(kwargs.get('timeout'), 60)
        if 'apply' in args:
            objects = list(yaml.safe_load_all(kwargs['input']))
            action = 'dry-run' if '--dry-run=server' in args else 'apply'
            self.calls.append((action, objects))
            if action == 'dry-run' and self.dry_run_error:
                raise subprocess.CalledProcessError(1, ['synthetic-admission-rejection'])
        elif 'exec' in args:
            self.assertEqual(args[args.index('-c') + 1], 'ha')
            if '-i' in args:
                self.calls.append(('contract', None))
                return subprocess.CompletedProcess(args, 0, json.dumps({'allowed': self.allowed}))
            self.calls.append(('mkdir', None))
        else:
            self.assertIn('secret', args)
            self.calls.append(('secret-exists', None))
        return subprocess.CompletedProcess(args, 0)

    def install(self):
        with (patch.object(sys, 'argv', self.argv),
              patch.object(installer.subprocess, 'check_output', side_effect=self.check_output),
              patch.object(installer.subprocess, 'run', side_effect=self.kubectl_run)):
            installer.main()

    def assert_no_mutation(self):
        self.assertFalse(any(action in ('apply', 'mkdir') for action, _ in self.calls))

    def job(self, terminal=None, projected=False):
        volume = {'configMap': {'name': installer.CODE_CONFIG_MAP}}
        if projected:
            volume = {'projected': {'sources': [volume]}}
        return {'spec': {'template': {'spec': {'volumes': [volume]}}},
                'status': {'conditions': [] if terminal is None else [
                    {'type': terminal, 'status': 'True'}]}}

    def test_all_objects_dry_run_before_writes_and_correct_container_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('synthetic-only')
            token.chmod(0o600)
            self.argv += ['--token-file', str(token)]
            self.install()
        self.assertEqual([action for action, _ in self.calls],
                         ['contract', 'dry-run', 'mkdir', 'apply'])
        dry = next(objects for action, objects in self.calls if action == 'dry-run')
        applied = next(objects for action, objects in self.calls if action == 'apply')
        self.assertEqual(dry, applied)
        self.assertEqual([obj['kind'] for obj in applied],
                         ['Secret', 'ConfigMap', 'CronJob', 'NetworkPolicy'])
        self.assertTrue(all(obj['metadata']['namespace'] == 'homeassistant' for obj in applied))
        pod = applied[2]['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod['initContainers'][0]['image'], self.image)
        self.assertEqual(pod['containers'][0]['image'], self.image)
        self.assertEqual(pod['initContainers'][1]['image'],
                         'mysql@sha256:8ab2115abe8ca3d2745962f4e75f2f86fb8182ae19908773cd4cce811f3c106e')

    def test_amd64_mysql_client_preserves_ha_workers_and_export_command(self):
        self.node['status']['nodeInfo']['architecture'] = 'amd64'
        self.install()
        applied = next(objects for action, objects in self.calls if action == 'apply')
        cron = next(obj for obj in applied if obj['kind'] == 'CronJob')
        pod = cron['spec']['jobTemplate']['spec']['template']['spec']
        mysql = next(c for c in pod['initContainers'] if c['name'] == 'mysql-dump')
        self.assertEqual(mysql['image'],
                         'mysql@sha256:5cf1e1293ea42e077e6a22114e6773b9aae3bb8f9933a2acd5a9721518ff8f5b')
        template = next(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
        template_pod = template['spec']['jobTemplate']['spec']['template']['spec']
        template_mysql = next(c for c in template_pod['initContainers'] if c['name'] == 'mysql-dump')
        self.assertEqual(mysql['command'], template_mysql['command'])
        self.assertEqual(mysql['securityContext'], template_mysql['securityContext'])
        self.assertEqual(pod['initContainers'][0]['image'], self.image)
        self.assertEqual(pod['containers'][0]['image'], self.image)

    def test_unsupported_mysql_architecture_fails_before_any_mutation(self):
        self.node['status']['nodeInfo']['architecture'] = 'unsupported'
        with self.assertRaises(KeyError):
            self.install()
        self.assert_no_mutation()

    def test_admission_rejection_does_not_initialize_directory_or_replace_code(self):
        self.argv += ['--timezone', 'Invalid/Zone']
        self.dry_run_error = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assert_no_mutation()

    def test_mismatched_or_mutable_image_rejected_before_writes(self):
        for image in ('fixture:latest', 'fixture@sha256:' + 'f' * 64):
            with self.subTest(image=image):
                self.ha['spec']['containers'][1]['image'] = image
                with self.assertRaisesRegex(ValueError, 'image|digest'):
                    self.install()
                self.assert_no_mutation()

    def test_ambiguous_configuration_container_rejected(self):
        self.ha['spec']['containers'][0]['volumeMounts'] = [
            {'name': 'config', 'mountPath': '/config'}]
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.install()
        self.assert_no_mutation()

    def test_unreviewed_runtime_rejected_before_writes(self):
        self.allowed = False
        with self.assertRaisesRegex(ValueError, 'contract'):
            self.install()
        self.assert_no_mutation()

    def test_unrecognized_storage_rejected_before_writes(self):
        self.storage['separate'] = True
        with self.assertRaisesRegex(ValueError, 'mount'):
            self.install()
        self.assert_no_mutation()

    def test_pending_and_projected_code_consumers_block_install(self):
        for projected in (False, True):
            with self.subTest(projected=projected):
                self.jobs = [self.job(projected=projected)]
                with self.assertRaisesRegex(ValueError, 'unfinished Job'):
                    self.install()
                self.assert_no_mutation()

    def test_job_starting_during_admission_still_blocks_mutation(self):
        self.late_jobs = [self.job()]
        with self.assertRaisesRegex(ValueError, 'unfinished Job'):
            self.install()
        self.assert_no_mutation()

    def test_terminal_jobs_do_not_block_install(self):
        self.jobs = [self.job('Complete'), self.job('Failed')]
        self.install()
        self.assertTrue(any(action == 'apply' for action, _ in self.calls))

    def test_rollout_during_admission_blocks_mutation(self):
        self.current = deepcopy(self.ha)
        self.current['metadata']['uid'] = 'replacement-pod'
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.install()
        self.assert_no_mutation()

    def test_invalid_token_fails_before_any_kubectl(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('synthetic-only')
            token.chmod(0o644)
            self.argv += ['--token-file', str(token)]
            with self.assertRaises(SystemExit):
                self.install()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pod_reads, 0)
