"""Execute the shipped maintenance YAML in an isolated Home Assistant runtime.

The Home Assistant dependency is optional for ordinary repository tests. To run
these tests, use the same Home Assistant Python environment as the deployment.
All service handlers and inventory lookups are fixture boundaries; no production
API, configuration directory, integration, network connection, or restart is used.
Only wait and delay durations are shortened in the copied package.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import homeassistant
except ImportError:
    if os.environ.get('REQUIRE_HA_RUNTIME') == '1':
        raise
    HA_AVAILABLE = False
else:
    import yaml
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.template.extensions.config_entries import ConfigEntryExtension
    from homeassistant.helpers.template.extensions.devices import DeviceExtension
    HA_AVAILABLE = True


PACKAGE = Path(__file__).resolve().parents[1] / 'homeassistant/packages/hacs_maintenance.yaml'
BACKUP_SENSOR = 'sensor.backup_last_successful_automatic_backup'
BACKUP_MANAGER = 'sensor.backup_backup_manager_state'
LATCH = 'input_boolean.hacs_maintenance_restart_pending'
INSTALL_SUCCEEDED = 'input_boolean.hacs_maintenance_install_succeeded'
UPDATE_AUTOMATION = 'automation.hacs_maintenance_update'
CORE_UPDATE = 'update.home_assistant_core'
CORE_MARKER = 'input_text.core_update_notified_candidate'


def shorten_waits(value):
    """Preserve all actions and templates; replace only wait/delay durations."""
    if isinstance(value, dict):
        if 'wait_template' in value:
            value['timeout'] = {'milliseconds': 200}
        if 'delay' in value:
            value['delay'] = {'milliseconds': 1}
        for item in value.values():
            shorten_waits(item)
    elif isinstance(value, list):
        for item in value:
            shorten_waits(item)


@unittest.skipUnless(HA_AVAILABLE, 'Home Assistant Python runtime is not installed')
class HacsMaintenanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A JSON override supports stdin-only execution in an existing HA image.
        # It contains the package under test, never live HA configuration.
        package_json = os.environ.get('HACS_MAINTENANCE_TEST_PACKAGE_JSON')
        self.package = json.loads(package_json) if package_json else yaml.safe_load(PACKAGE.read_text())
        self.package = deepcopy(self.package)
        shorten_waits(self.package)
        self.temporary = tempfile.TemporaryDirectory(prefix='hacs-maintenance-test-')
        self.hass = HomeAssistant(self.temporary.name)
        self.calls = []
        self.inventory = []
        self.models = {}
        self.worker_tasks = []
        self.worker_errors = []
        self.backup_result = 'success'
        self.install_failures = set()
        self.install_hangs = set()
        self.install_version_overrides = {}
        self.install_post_write_failures = set()
        self.core_after_backup = None
        self.core_after_install = None
        self.core_before_worker = {}
        self.backup_gate = asyncio.Event()
        self.install_gate = asyncio.Event()
        self.cancelled_services = []
        self.logger = logging.getLogger('test.hacs_maintenance')
        self.logger.disabled = True
        self.now = datetime(2026, 9, 27, 13, tzinfo=timezone.utc)

        inventory = self.inventory
        models = self.models
        self.patches = [
            patch('homeassistant.util.dt.now', side_effect=lambda *args, **kwargs: self.now),
            patch.object(ConfigEntryExtension, 'integration_entities',
                         lambda extension, domain: list(inventory) if domain == 'hacs' else []),
            patch.object(DeviceExtension, 'device_attr',
                         lambda extension, entity, attribute: models.get(entity) if attribute == 'model' else None),
        ]
        for patcher in self.patches:
            patcher.start()

        self.hass.states.async_set(BACKUP_MANAGER, 'idle')
        self.hass.states.async_set(BACKUP_SENSOR, '2000-01-01T00:00:00+00:00')
        self.hass.states.async_set(LATCH, 'off')
        self.hass.states.async_set(INSTALL_SUCCEEDED, 'off')
        self.hass.states.async_set(UPDATE_AUTOMATION, 'on', {'current': 1})

        self.workers = {}
        for name, config in self.package['script'].items():
            entity = f'script.{name}'
            self.hass.states.async_set(entity, 'off', {'current': 0})
            self.workers[entity] = self.make_script(config['sequence'], name, 'script')

        self.hass.services.async_register('script', 'turn_on', self.start_worker)
        self.hass.services.async_register('backup', 'create_automatic', self.backup)
        self.hass.services.async_register('update', 'install', self.install)
        self.hass.services.async_register('input_boolean', 'turn_on', self.set_latch)
        self.hass.services.async_register('input_boolean', 'turn_off', self.set_latch)
        self.hass.services.async_register('persistent_notification', 'create', self.record)
        self.hass.services.async_register('homeassistant', 'restart', self.record)

    async def asyncTearDown(self):
        # Release simulated blocked I/O only after each test has inspected it.
        self.backup_gate.set()
        self.install_gate.set()
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def make_script(self, sequence, name, domain, variables=None):
        return Script(
            self.hass, cv.SCRIPT_SCHEMA(sequence), name, domain,
            script_mode='single', logger=self.logger,
            variables=cv.SCRIPT_VARIABLES_SCHEMA(variables) if variables else None,
        )

    def add_update(self, name, model='plugin', *, available=True, integration='hacs'):
        entity = f'update.{name}'
        if integration == 'hacs':
            self.inventory.append(entity)
        self.models[entity] = model
        self.hass.states.async_set(entity, 'on' if available else 'off', {
            'installed_version': '1.0.0', 'latest_version': '2.0.0' if available else '1.0.0',
            'in_progress': False, 'release_summary': None,
        })
        return entity

    def set_core(self, stage='available', *, state='on', in_progress=False):
        self.hass.states.async_set(CORE_MARKER, 'fixture-candidate')
        self.hass.states.async_set(CORE_UPDATE, state, {
            'stage': stage, 'in_progress': in_progress,
        })

    async def record(self, call):
        self.calls.append((f'{call.domain}.{call.service}', dict(call.data)))

    async def set_latch(self, call):
        await self.record(call)
        entities = call.data['entity_id']
        if isinstance(entities, str):
            entities = [entities]
        for entity in entities:
            self.hass.states.async_set(entity, 'on' if call.service == 'turn_on' else 'off')

    async def backup(self, call):
        await self.record(call)
        self.hass.states.async_set(BACKUP_MANAGER, 'create_backup')
        if self.backup_result == 'hang':
            try:
                await self.backup_gate.wait()
            except asyncio.CancelledError:
                self.cancelled_services.append('backup')
                raise
        if self.backup_result == 'raise':
            self.hass.states.async_set(BACKUP_MANAGER, 'idle')
            raise HomeAssistantError('Fixture backup creation failed')
        if self.backup_result in ('success', 'hang'):
            self.hass.states.async_set(BACKUP_SENSOR, self.now.isoformat())
        # Upload failure can return normally without advancing the success sensor.
        self.hass.states.async_set(BACKUP_MANAGER, 'idle')
        if self.core_after_backup:
            self.set_core(**self.core_after_backup)

    async def install(self, call):
        await self.record(call)
        entity = call.data['entity_id']
        if isinstance(entity, list):
            self.assertEqual(len(entity), 1)
            entity = entity[0]
        if entity in self.install_hangs:
            old = self.hass.states.get(entity)
            self.hass.states.async_set(entity, old.state, dict(old.attributes) | {'in_progress': True})
            try:
                await self.install_gate.wait()
            except asyncio.CancelledError:
                self.cancelled_services.append('install')
                raise
        if entity in self.install_failures:
            raise HomeAssistantError('Fixture installation failed')
        old = self.hass.states.get(entity)
        installed_version = self.install_version_overrides.get(entity, old.attributes['latest_version'])
        attributes = dict(old.attributes) | {
            'installed_version': installed_version, 'latest_version': installed_version,
            'in_progress': False,
        }
        if self.models[entity] == 'integration':
            attributes['release_summary'] = 'Restart of Home Assistant required'
        self.hass.states.async_set(entity, 'off', attributes)
        if self.core_after_install:
            self.set_core(**self.core_after_install)
        if entity in self.install_post_write_failures:
            raise HomeAssistantError('Fixture post-install processing failed after version write')

    async def start_worker(self, call):
        await self.record(call)
        entity = call.data['entity_id']
        if isinstance(entity, list):
            self.assertEqual(len(entity), 1)
            entity = entity[0]
        self.assertEqual(self.hass.states.get(entity).state, 'off')
        self.hass.states.async_set(entity, 'on', {'current': 1})

        async def run():
            try:
                if entity in self.core_before_worker:
                    self.set_core(**self.core_before_worker[entity])
                await self.workers[entity].async_run(call.data.get('variables', {}), Context())
            except HomeAssistantError as error:
                self.worker_errors.append(str(error))
            finally:
                self.hass.states.async_set(entity, 'off', {'current': 0})

        self.worker_tasks.append(asyncio.create_task(run()))

    async def run_automation(self, suffix='update'):
        config = next(item for item in self.package['automation'] if item['id'].endswith(suffix))
        # Prepend the actual automation conditions so Script evaluates the same
        # condition actions after rendering the actual automation variables.
        script = self.make_script(
            config.get('conditions', []) + config['actions'],
            config['id'], 'automation', config.get('variables'),
        )
        try:
            await script.async_run({'this': {'entity_id': UPDATE_AUTOMATION}}, Context())
        except HomeAssistantError:
            # Explicit stop(error=true) is the package's intended failure result.
            pass

    def count(self, service):
        return sum(name == service for name, _ in self.calls)

    async def test_recent_successful_backup_is_reused(self):
        entity = self.add_update('recent_backup_card')
        self.hass.states.async_set(BACKUP_SENSOR, (self.now - timedelta(hours=2)).isoformat())
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 0)
        self.assertEqual(self.installed(), [entity])

    async def test_old_backup_is_replaced_before_install(self):
        entity = self.add_update('old_backup_card')
        self.hass.states.async_set(BACKUP_SENSOR, (self.now - timedelta(hours=5)).isoformat())
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assertEqual(self.installed(), [entity])

    async def test_future_backup_timestamp_is_not_reused(self):
        entity = self.add_update('future_backup_card')
        self.hass.states.async_set(BACKUP_SENSOR, (self.now + timedelta(hours=1)).isoformat())
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assertEqual(self.installed(), [entity])

    async def test_weekday_does_not_start_backup_or_updates(self):
        self.add_update('weekday_card')
        self.now += timedelta(days=1)
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 0)
        self.assertEqual(self.installed(), [])

    def installed(self):
        result = []
        for service, data in self.calls:
            if service == 'update.install':
                entity = data['entity_id']
                result.append(entity[0] if isinstance(entity, list) else entity)
        return result

    def assert_stopped(self):
        self.assertEqual(self.installed(), [])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertTrue(any(data.get('title') == 'HACS maintenance stopped'
                            for name, data in self.calls if name == 'persistent_notification.create'))

    async def test_no_updates_does_not_back_up_or_restart(self):
        self.add_update('current', available=False)
        self.add_update('ha_core', integration='homeassistant')
        await self.run_automation()
        self.assertEqual(self.calls, [])

    async def test_plugins_back_up_once_install_in_order_and_do_not_restart(self):
        second = self.add_update('z_card')
        first = self.add_update('a_card')
        self.add_update('device_firmware', integration='other')
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assertEqual(self.installed(), [first, second])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get(LATCH).state, 'off')

    async def test_integrations_and_hacs_install_hacs_last_restart_once(self):
        hacs = self.add_update('hacs_update', 'integration')
        integration = self.add_update('z_integration', 'integration')
        plugin = self.add_update('a_card')
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assertEqual(self.installed(), [plugin, integration, hacs])
        self.assertEqual(self.count('homeassistant.restart'), 1)
        restart_index = next(i for i, call in enumerate(self.calls) if call[0] == 'homeassistant.restart')
        self.assertTrue(all(i < restart_index for i, call in enumerate(self.calls) if call[0] == 'update.install'))
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')

    async def test_numeric_version_strings_preserve_exact_release_identity(self):
        entity = self.add_update('numeric_release')
        old = self.hass.states.get(entity)
        self.hass.states.async_set(entity, 'on', dict(old.attributes) | {
            'installed_version': '1.00', 'latest_version': '2.00',
        })
        await self.run_automation()
        self.assertEqual(self.installed(), [entity])
        self.assertFalse(any(data.get('title') == 'HACS maintenance stopped'
                             for name, data in self.calls if name == 'persistent_notification.create'))

    async def test_backup_exception_stops_before_any_install(self):
        self.add_update('integration', 'integration')
        self.backup_result = 'raise'
        await self.run_automation()
        self.assert_stopped()
        self.assertTrue(self.worker_errors)

    async def test_upload_failure_with_stale_success_timestamp_stops(self):
        self.add_update('integration', 'integration')
        self.backup_result = 'upload_failed'
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assert_stopped()

    async def test_failed_install_stops_remaining_updates_and_restart(self):
        failed = self.add_update('a_failed', 'integration')
        self.add_update('z_later', 'integration')
        self.install_failures.add(failed)
        await self.run_automation()
        self.assertEqual(self.installed(), [failed])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertTrue(self.worker_errors)

    async def test_post_install_exception_cannot_look_like_success(self):
        entity = self.add_update('a_integration', 'integration')
        self.add_update('z_later')
        self.install_post_write_failures.add(entity)
        await self.run_automation()
        self.assertEqual(self.installed(), [entity])
        self.assertEqual(self.hass.states.get(entity).attributes['installed_version'], '2.0.0')
        self.assertIs(self.hass.states.get(entity).attributes['in_progress'], False)
        self.assertEqual(self.hass.states.get(INSTALL_SUCCEEDED).state, 'off')
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertTrue(self.worker_errors)

    async def test_backup_timeout_leaves_worker_running_without_cancellation(self):
        self.add_update('integration', 'integration')
        self.backup_result = 'hang'
        await self.run_automation()
        self.assert_stopped()
        self.assertEqual(self.hass.states.get('script.hacs_maintenance_backup').state, 'on')
        self.assertEqual(self.cancelled_services, [])
        self.assertFalse(self.worker_tasks[0].done())

    async def test_installer_timeout_leaves_worker_running_without_restart(self):
        entity = self.add_update('integration', 'integration')
        self.install_hangs.add(entity)
        await self.run_automation()
        self.assertEqual(self.installed(), [entity])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get('script.hacs_maintenance_install').state, 'on')
        self.assertEqual(self.cancelled_services, [])
        self.assertFalse(self.worker_tasks[-1].done())

    async def test_late_integration_install_preserves_restart_request(self):
        entity = self.add_update('integration', 'integration')
        self.install_hangs.add(entity)
        await self.run_automation()
        self.assertEqual(self.hass.states.get(LATCH).state, 'off')
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.install_gate.set()
        await asyncio.gather(*self.worker_tasks)
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.cancelled_services, [])

    async def test_changed_version_stops_batch_but_retains_integration_restart(self):
        entity = self.add_update('a_integration', 'integration')
        self.add_update('z_later')
        self.install_version_overrides[entity] = '3.0.0'
        await self.run_automation()
        self.assertEqual(self.installed(), [entity])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')

    async def test_busy_automation_defers_restart_and_retains_latch(self):
        self.add_update('integration', 'integration')
        self.hass.states.async_set('automation.critical_device_action', 'on', {'current': 1})
        await self.run_automation()
        self.assertEqual(len(self.installed()), 1)
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')
        self.assertTrue(any(data.get('title') == 'HACS restart deferred'
                            for name, data in self.calls if name == 'persistent_notification.create'))

    async def test_pending_restart_retries_without_another_backup(self):
        self.hass.states.async_set(LATCH, 'on')
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 0)
        self.assertEqual(self.installed(), [])
        self.assertEqual(self.count('homeassistant.restart'), 1)

    async def test_active_or_uncertain_core_stops_before_backup_and_install(self):
        self.add_update('core_guard')
        cases = [
            {'stage': stage} for stage in (
                'preparing', 'backup', 'preflight', 'deploying', 'verifying',
                'recovery_required', 'new_unknown_stage',
            )
        ] + [
            {'stage': 'available', 'in_progress': True},
            {'stage': 'available', 'in_progress': 42},
            {'stage': 'available', 'state': 'unknown'},
            {'stage': 'available', 'state': 'unavailable'},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.calls.clear()
                self.set_core(**case)
                await self.run_automation()
                self.assertEqual(self.count('backup.create_automatic'), 0)
                self.assert_stopped()

    async def test_configured_core_missing_entity_fails_closed(self):
        self.add_update('no_core_status')
        self.hass.states.async_set(CORE_MARKER, '')
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 0)
        self.assert_stopped()

    async def test_idle_available_and_terminal_safe_core_states_allow_hacs(self):
        for stage in ('idle', 'available', 'succeeded', 'failed'):
            with self.subTest(stage=stage):
                self.calls.clear()
                entity = self.add_update('core_safe_' + stage)
                self.set_core(stage)
                await self.run_automation()
                self.assertEqual(self.installed(), [entity])

    async def test_direct_backup_and_install_workers_block_active_core(self):
        entity = self.add_update('direct_guard')
        self.set_core('backup')
        await self.workers['script.hacs_maintenance_backup'].async_run({}, Context())
        await self.workers['script.hacs_maintenance_install'].async_run({'update_entity': entity}, Context())
        self.assertEqual(self.count('backup.create_automatic'), 0)
        self.assertEqual(self.installed(), [])
        self.assertEqual(self.hass.states.get(INSTALL_SUCCEEDED).state, 'off')

    async def test_core_transition_after_backup_stops_before_install(self):
        self.add_update('after_backup')
        self.core_after_backup = {'stage': 'preparing'}
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assert_stopped()

    async def test_install_worker_rechecks_after_coordinator_guard(self):
        self.add_update('worker_race')
        self.core_before_worker['script.hacs_maintenance_install'] = {'stage': 'backup'}
        await self.run_automation()
        self.assertEqual(self.count('backup.create_automatic'), 1)
        self.assert_stopped()

    async def test_core_transition_between_items_defers_remaining_and_restart(self):
        first = self.add_update('a_first', 'integration')
        self.add_update('z_remaining', 'integration')
        self.core_after_install = {'stage': 'preparing'}
        await self.run_automation()
        self.assertEqual(self.installed(), [first])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')
        self.assertEqual(self.cancelled_services, [])

    async def test_core_recovery_with_false_progress_prevents_final_restart(self):
        entity = self.add_update('final_restart', 'integration')
        self.core_after_install = {'stage': 'recovery_required', 'in_progress': False}
        await self.run_automation()
        self.assertEqual(self.installed(), [entity])
        self.assertEqual(self.count('homeassistant.restart'), 0)
        self.assertEqual(self.hass.states.get(LATCH).state, 'on')
        self.assertTrue(any(data.get('title') == 'HACS restart deferred'
                            for name, data in self.calls if name == 'persistent_notification.create'))

    async def test_startup_clears_pending_restart_latch(self):
        self.hass.states.async_set(LATCH, 'on')
        await self.run_automation('startup')
        self.assertEqual(self.hass.states.get(LATCH).state, 'off')
        self.assertEqual(self.count('input_boolean.turn_off'), 1)
        self.assertEqual(self.count('homeassistant.restart'), 0)


if __name__ == '__main__':
    unittest.main()
