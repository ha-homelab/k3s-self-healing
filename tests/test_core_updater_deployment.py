"""Installation cannot reveal credentials or accidentally modify the HA release."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_core_updater', ROOT / 'scripts/install-core-updater.py')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'private.json'
        self.values = {
            'approval_token': 'a' * 64, 'mqtt_username': 'updater',
            'mqtt_password': 'example-test-only',
            'ssh-privatekey': '-----BEGIN OPENSSH PRIVATE KEY-----\nexample-test-only\n-----END OPENSSH PRIVATE KEY-----\n',
            'known_hosts': 'github.com ssh-ed25519 example-test-only',
        }
        self.path.write_text(json.dumps(self.values))
        self.path.chmod(0o600)

    def test_only_private_complete_files_are_accepted(self):
        secret = INSTALL.credentials_secret(self.path, 'lab')
        self.assertEqual(secret['metadata']['namespace'], 'lab')
        self.assertEqual(set(secret['data']), INSTALL.CREDENTIAL_KEYS)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'mode 0600 or 0400'):
            INSTALL.credentials_secret(self.path, 'lab')

    def test_symlinks_are_rejected(self):
        link = self.path.with_name('link.json')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            INSTALL.private_json(link)

    def test_missing_keys_and_weak_token_are_rejected(self):
        self.values['approval_token'] = 'short'
        self.path.write_text(json.dumps(self.values))
        with self.assertRaisesRegex(ValueError, '32 characters'):
            INSTALL.credentials_secret(self.path, 'lab')
        del self.values['known_hosts']
        self.path.write_text(json.dumps(self.values))
        with self.assertRaisesRegex(ValueError, 'exactly'):
            INSTALL.credentials_secret(self.path, 'lab')

    def test_nested_inline_secrets_cannot_enter_configmap(self):
        with self.assertRaisesRegex(ValueError, 'inline secrets'):
            INSTALL.reject_inline_secrets({'nested': [{'mqtt_password': 'not-safe'}]})
        INSTALL.reject_inline_secrets({'approval_token_file': '/credentials/approval_token'})

    def test_manifest_error_does_not_echo_secret_or_code(self):
        kube = INSTALL.Kubectl('/test/kubeconfig', 'lab')
        result = argparse.Namespace(returncode=1, stdout='', stderr='SECRET-MATERIAL')
        with patch.object(INSTALL.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError) as error:
                kube.apply([{'kind': 'Secret'}], dry_run=True)
        self.assertNotIn('SECRET-MATERIAL', str(error.exception))

    def test_secret_values_travel_only_over_stdin(self):
        kube = INSTALL.Kubectl('/test/kubeconfig', 'lab')
        result = argparse.Namespace(returncode=0, stdout='secret/example\n', stderr='')
        with patch.object(INSTALL.subprocess, 'run', return_value=result) as run, patch('builtins.print'):
            kube.apply([{'kind': 'Secret', 'data': {'password': 'TEST-SENSITIVE'}}], dry_run=True)
        command = run.call_args.args[0]
        self.assertIn('--server-side', command)
        self.assertIn('--dry-run=server', command)
        self.assertNotIn('TEST-SENSITIVE', ' '.join(command))
        self.assertIn('TEST-SENSITIVE', run.call_args.kwargs['input'])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(namespace='lab', ha_statefulset='home-assistant',
                                       ha_service='ha-api', mqtt_service='broker', storage_class='local-path')
        self.cluster = {
            'node': 'test-node', 'pvc': 'existing-ha-data', 'subpath': 'config',
            'ha_address': '192.0.2.5/32', 'ha_selector': {'app': 'custom-ha'},
            'mqtt_selector': {'app': 'custom-broker'}, 'mqtt_ip': '192.0.2.21',
            'api_addresses': ['10.43.0.1/32', '192.0.2.10/32'], 'api_ports': [443, 6443],
        }
        self.config = {'apiVersion': 'v1', 'kind': 'ConfigMap',
                       'metadata': {'name': 'ha-core-updater-code', 'namespace': 'lab'},
                       'data': {'settings.json': '{}', 'server.py': '# Test\n'}}
        self.objects = INSTALL.render(self.args, self.cluster, self.config)

    def one(self, kind, name=None):
        return next(item for item in self.objects if item['kind'] == kind and (name is None or item['metadata']['name'] == name))

    def test_only_updater_resources_are_created(self):
        self.assertTrue(all(item['metadata']['namespace'] == 'lab' for item in self.objects))
        self.assertFalse(any(item['kind'] in {'StatefulSet', 'CronJob', 'ClusterRole', 'ClusterRoleBinding'} for item in self.objects))
        self.assertEqual(self.one('PersistentVolumeClaim')['metadata']['name'], 'ha-core-updater-state')
        self.assertEqual(self.one('RoleBinding')['subjects'][0]['namespace'], 'lab')

    def test_readonly_existing_config_and_no_host_privileges(self):
        deployment = self.one('Deployment')
        self.assertEqual(deployment['spec']['strategy']['type'], 'Recreate')
        self.assertEqual(deployment['spec']['replicas'], 1)
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod.get('hostNetwork', False))
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'], 'test-node')
        self.assertFalse(any('hostPath' in volume for volume in pod['volumes']))
        config_volume = next(volume for volume in pod['volumes'] if volume['name'] == 'config')
        self.assertEqual(config_volume['persistentVolumeClaim'], {'claimName': 'existing-ha-data', 'readOnly': True})
        container = pod['containers'][0]
        config_mount = next(mount for mount in container['volumeMounts'] if mount['name'] == 'config')
        self.assertTrue(config_mount['readOnly'])
        self.assertEqual(config_mount['subPath'], 'config')
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(container['securityContext']['capabilities'], {'drop': ['ALL']})
        self.assertFalse(any(mount['name'] == 'kube-api' for mount in pod['initContainers'][0]['volumeMounts']))

    def test_config_checksum_rolls_only_updater(self):
        initial = self.one('Deployment')['spec']['template']['metadata']['annotations']['checksum/code-settings']
        self.config['data']['settings.json'] = '{"changed": true}'
        changed = INSTALL.render(self.args, self.cluster, self.config)
        deployment = next(item for item in changed if item['kind'] == 'Deployment')
        self.assertNotEqual(initial, deployment['spec']['template']['metadata']['annotations']['checksum/code-settings'])

    def test_credential_rotation_is_reflected_in_pod_template(self):
        rotated = INSTALL.render(self.args, self.cluster, self.config, credential_checksum='rotation-checksum')
        deployment = next(item for item in rotated if item['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['template']['metadata']['annotations']['checksum/credentials'], 'rotation-checksum')

    def test_worker_settings_cannot_target_a_different_config_volume(self):
        with self.assertRaisesRegex(ValueError, 'config_pvc conflicts'):
            INSTALL.cluster_settings({'config_pvc': 'some-other-config'}, self.args, self.cluster)
        normalized = INSTALL.cluster_settings({}, self.args, self.cluster)
        self.assertEqual(normalized['namespace'], 'lab')
        self.assertEqual(normalized['config_pvc'], 'existing-ha-data')
        self.assertEqual(normalized['config_subpath'], 'config')

    def test_runtime_endpoints_follow_selected_services_and_namespace(self):
        settings = INSTALL.cluster_settings({}, self.args, self.cluster)
        configmap = INSTALL.code_configmap(settings, self.args.namespace)
        configured = json.loads(configmap['data']['settings.json'])
        self.assertEqual(configured['ha_url'], 'http://ha-api.lab.svc.cluster.local:8123')
        self.assertEqual(configured['mqtt_host'], 'broker.lab.svc.cluster.local')
        self.assertEqual(INSTALL.cluster_settings(dict(settings), self.args, self.cluster), settings)
        container = self.one('Deployment')['spec']['template']['spec']['containers'][0]
        self.assertFalse({'HA_URL', 'MQTT_HOST'} & {item['name'] for item in container['env']})

    def test_conflicting_runtime_endpoints_are_rejected(self):
        for key, value in (
                ('ha_url', 'http://home-assistant.homeassistant.svc.cluster.local:8123'),
                ('mqtt_host', 'mosquitto.homeassistant.svc.cluster.local')):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key + ' conflicts'):
                INSTALL.cluster_settings({key: value}, self.args, self.cluster)

    def test_unsupported_statefulset_is_rejected_before_cluster_access(self):
        with patch('sys.argv', ['install', '--kubeconfig', '/unused',
                               '--settings-file', '/unused', '--ha-statefulset', 'custom-ha']), \
                patch.object(INSTALL, 'Kubectl') as kube, \
                patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                INSTALL.main()
        self.assertEqual(error.exception.code, 2)
        kube.assert_not_called()

    def test_api_and_ingress_policy_use_discovered_addresses(self):
        policy = self.one('NetworkPolicy', 'ha-core-updater')['spec']
        self.assertEqual(policy['ingress'][0]['from'][0]['ipBlock']['cidr'], '192.0.2.5/32')
        self.assertEqual(policy['egress'][1]['to'], [{'ipBlock': {'cidr': item}} for item in self.cluster['api_addresses']])
        preflight = self.one('NetworkPolicy', 'ha-core-preflight')['spec']
        self.assertEqual(preflight['ingress'], [])
        self.assertEqual(len(preflight['egress']), 2)
        self.assertEqual(preflight['egress'][0], policy['egress'][0])
        self.assertEqual(preflight['egress'][1]['to'], policy['egress'][-1]['to'])
        self.assertEqual(preflight['egress'][1]['ports'], [{'protocol': 'TCP', 'port': 443}])

    def test_only_named_statefulset_and_backup_cronjob_can_be_patched(self):
        role = self.one('Role')
        writes = [rule for rule in role['rules'] if {'patch', 'update'} & set(rule['verbs'])]
        self.assertEqual({tuple(rule['resources']) for rule in writes}, {('statefulsets',), ('cronjobs',), ('secrets',)})
        statefulset = next(rule for rule in writes if rule['resources'] == ['statefulsets'])
        self.assertEqual(statefulset['resourceNames'], ['home-assistant'])
        self.assertFalse(any('pods/exec' in rule['resources'] for rule in role['rules']))
        self.assertFalse(any(rule['resources'] == ['pods'] and 'delete' in rule['verbs'] for rule in role['rules']))


if __name__ == '__main__':
    unittest.main()
