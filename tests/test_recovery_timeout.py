"""Bound component budgets without extending retries or contacting a cluster."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from disaster_recovery import runner


INVALID_TIMEOUTS = (None, True, False, 0, -1, 3601, 900.0, '900', [], {})


class RecoveryTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_helper_forwards_default_and_bounded_overrides(self):
        for supplied, expected in (({}, 900), ({'archive_timeout_seconds': 1}, 1),
                                   ({'archive_timeout_seconds': 3600}, 3600)):
            with self.subTest(supplied=supplied), \
                 patch.object(runner, 'create_archive', return_value={'bytes': 42}) as archive:
                result = runner.archive_component(self.base / 'source', self.base / 'work',
                    'application', {'type': 'fixture'}, ['*.log'], ['images'], **supplied)
                archive.assert_called_once_with(self.base / 'source', self.base / 'work/application.tar.gz',
                    excludes=['*.log'], generated_image_caches=['images'], timeout_seconds=expected)
                self.assertEqual(result, {'name': 'application', 'source': {'type': 'fixture'}, 'bytes': 42})

    def test_helper_rejects_invalid_budgets_before_archiving(self):
        for value in INVALID_TIMEOUTS:
            with self.subTest(value=value), patch.object(runner, 'create_archive') as archive:
                with self.assertRaisesRegex(ValueError, 'archive_timeout_seconds'):
                    runner.archive_component(self.base, self.base / 'work', 'application', {},
                                             archive_timeout_seconds=value)
                archive.assert_not_called()
                self.assertFalse((self.base / 'work').exists())

    def test_every_entry_is_validated_before_filesystem_or_cluster_side_effects(self):
        for section in ('claims', 'host_sources'):
            for value in INVALID_TIMEOUTS:
                config = {'claims': [{'name': 'first-claim'}],
                          'host_sources': [{'name': 'first-host-source'}]}
                config[section].append({'name': 'last-entry', 'optional': True,
                                        'archive_timeout_seconds': value})
                original = copy.deepcopy(config)
                root = self.base / 'backups'
                with self.subTest(section=section, value=value), \
                     patch.object(runner, 'Kubernetes') as kubernetes, \
                     patch.object(runner.os, 'umask') as umask, \
                     patch.object(runner, 'archive_component') as archive:
                    with self.assertRaisesRegex(ValueError, 'archive_timeout_seconds'):
                        runner.run(config, root)
                    kubernetes.assert_not_called()
                    kubernetes.return_value.checkpoint_mosquitto.assert_not_called()
                    archive.assert_not_called()
                    umask.assert_not_called()
                    self.assertFalse(root.exists())
                    self.assertEqual(config, original)

    def test_run_forwards_each_entry_budget_and_preserves_internal_defaults(self):
        host = self.base / 'host'
        (host / 'etc').mkdir(parents=True)
        (host / 'etc/machine-id').write_text('fixture-machine')
        claims = [{'name': 'application', 'archive_timeout_seconds': 1800}, {'name': 'mosquitto'}]
        host_sources = [{'name': 'host-state', 'path': '/state', 'archive_timeout_seconds': 3600},
                        {'name': 'host-settings', 'path': '/settings'}]
        sources = {}
        for item in claims + host_sources:
            source = host / item.get('path', item['name']).lstrip('/')
            source.mkdir()
            (source / 'settings.json').write_text('{}')
            sources[item['name']] = source
        mqtt = sources['mosquitto'] / 'mosquitto.db'
        mqtt.write_bytes(b'fixture persistence')

        def checkpoint(_pod):
            previous = mqtt.stat()
            os.utime(mqtt, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
            return {'status': 'fixture-checkpoint'}

        pod = {'metadata': {'namespace': 'homeassistant', 'name': 'fixture-broker', 'uid': 'fixture-uid'},
               'spec': {'containers': [{'name': 'mosquitto'}]}, 'status': {'phase': 'Running'}}
        api = Mock(spec=['get', 'list', 'checkpoint_mosquitto'])
        api.get.side_effect = {
            '/api/v1/nodes/fixture-node': {'status': {'nodeInfo': {'machineID': 'fixture-machine'}}},
            '/version': {'gitVersion': 'fixture'},
        }.__getitem__
        api.list.side_effect = {
            '/apis/apiextensions.k8s.io/v1/customresourcedefinitions': {'items': []},
            '/api/v1/pods': {'items': [pod]},
        }.__getitem__
        api.checkpoint_mosquitto.side_effect = checkpoint
        config = {'node': 'fixture-node', 'minimum_free_bytes': 0, 'retain_completed': 2,
                  'namespaces': ['homeassistant'], 'api_collections': [],
                  'claims': claims, 'host_sources': host_sources}

        def resolve(_api, item, _config, _host, _nfs):
            return sources[item['name']], {'type': 'fixture', 'claim': item['name']}

        previous_umask = os.umask(0o077)
        try:
            with patch.dict(os.environ, {'NODE_NAME': 'fixture-node'}), \
                 patch.object(runner, 'resolve_claim', side_effect=resolve), \
                 patch.object(runner, 'create_archive', wraps=runner.create_archive) as archive:
                snapshot = runner.run(config, self.base / 'backups', host=host, api=api)
        finally:
            os.umask(previous_umask)

        self.assertEqual(archive.call_count, 6)
        self.assertEqual({call.args[1].name: call.kwargs['timeout_seconds'] for call in archive.call_args_list}, {
            'application.tar.gz': 1800, 'mosquitto.tar.gz': 900,
            'host-state.tar.gz': 3600, 'host-settings.tar.gz': 900,
            'kubernetes.tar.gz': 900, 'recovery-tools.tar.gz': 900,
        })
        api.checkpoint_mosquitto.assert_called_once_with(pod)
        self.assertTrue(json.loads((snapshot / 'manifest.json').read_text())['completed'])


if __name__ == '__main__':
    unittest.main()
