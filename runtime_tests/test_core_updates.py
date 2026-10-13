"""Exercise the shipped approval YAML in isolated HA with all I/O stubbed.

An optional JSON package override supports stdin-only execution inside the
installed HA image. No live configuration, credentials, MQTT, HTTP, or device
services are loaded. REQUIRE_HA_RUNTIME=1 makes a missing runtime a hard failure.
"""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest

import yaml

try:
    from homeassistant.core import Context, HomeAssistant, SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
except ImportError:
    HA_AVAILABLE = False
else:
    HA_AVAILABLE = True

if os.environ.get('REQUIRE_HA_RUNTIME') == '1' and not HA_AVAILABLE:
    raise RuntimeError('Home Assistant runtime is required for approval tests')

PACKAGE = Path(__file__).resolve().parents[1] / 'homeassistant/packages/core_updates.yaml'
ENTITY = 'update.home_assistant_core'
CANDIDATE = 'a' * 32
NEW_CANDIDATE = 'b' * 32
VERSION = '2026.10.0'


class FixtureLoader(yaml.SafeLoader):
    pass


FixtureLoader.add_constructor('!secret', lambda loader, node: {
    'ha_core_updater_authorization': 'Bearer TEST_ONLY',
    'ha_core_updater_approve_url': 'http://ha-core-updater.homeassistant.svc.cluster.local:8099/approve',
    'optional_phone_notify_service': 'notify.mobile_app_example_phone',
}[node.value])


def load_package():
    override = os.environ.get('CORE_UPDATES_TEST_PACKAGE_JSON')
    return json.loads(override) if override else yaml.load(PACKAGE.read_text(), Loader=FixtureLoader)


class CoreUpdateBoundaryTest(unittest.TestCase):
    def test_approval_uses_protected_rest_not_mqtt(self):
        package = load_package()
        transport = package['rest_command']['core_update_approve']
        self.assertEqual(transport['method'], 'POST')
        self.assertEqual(transport['url'], 'http://ha-core-updater.homeassistant.svc.cluster.local:8099/approve')
        self.assertEqual(transport['content_type'], 'application/json')
        self.assertIn('authorization', transport['headers'])
        self.assertNotIn('mqtt.publish', json.dumps(package))
        self.assertNotIn('update.install', json.dumps(package))
        self.assertNotIn('homeassistant.restart', json.dumps(package))

    def test_only_explicit_action_event_reaches_approval_handler(self):
        package = load_package()
        handler = next(item for item in package['automation'] if item['id'].endswith('mobile_action'))
        self.assertEqual(handler['triggers'], [{'trigger': 'event', 'event_type': 'mobile_app_notification_action'}])
        self.assertNotIn('initial', package['input_text']['core_update_notified_candidate'])
        self.assertNotIn('initial', package['input_text']['core_update_notified_result'])


@unittest.skipUnless(HA_AVAILABLE, 'Home Assistant Python runtime is not installed')
class CoreUpdateRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.package = deepcopy(load_package())
        self.temporary = tempfile.TemporaryDirectory(prefix='core-updates-test-')
        self.hass = HomeAssistant(self.temporary.name)
        self.calls = []
        self.status = 202
        self.transport_error = False
        self.change_candidate_before_script = False
        self.logger = logging.getLogger('test.core_updates')
        self.logger.disabled = True
        self.scripts = {
            name: self.make_script(config['sequence'], name)
            for name, config in self.package['script'].items()
        }
        self.hass.services.async_register('rest_command', 'core_update_approve', self.approve,
                                          supports_response=SupportsResponse.ONLY)
        self.hass.services.async_register('script', 'core_update_approve', self.run_approval)
        self.hass.services.async_register('script', 'notify_optional', self.record)
        self.hass.services.async_register('persistent_notification', 'create', self.record)
        self.hass.services.async_register('persistent_notification', 'dismiss', self.record)
        self.hass.services.async_register('input_text', 'set_value', self.set_text)
        for name in self.package['input_text']:
            self.hass.states.async_set('input_text.' + name, '')
        self.set_update()

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temporary.cleanup()

    def make_script(self, sequence, name):
        return Script(self.hass, cv.SCRIPT_SCHEMA(sequence), name, 'script',
                      script_mode='single', logger=self.logger)

    def set_update(self, state='on', **attributes):
        self.hass.states.async_set(ENTITY, state, {
            'candidate_id': CANDIDATE, 'target_version': VERSION,
            'latest_version': VERSION, 'installed_version': '2026.9.1',
            'stage': 'available', 'in_progress': False, 'last_error': None,
        } | attributes)

    async def record(self, call):
        self.calls.append((f'{call.domain}.{call.service}', dict(call.data)))

    async def approve(self, call):
        await self.record(call)
        if self.transport_error:
            raise HomeAssistantError('Fixture request timed out')
        return {'status': self.status, 'content': {}, 'headers': {}}

    async def run_approval(self, call):
        await self.record(call)
        if self.change_candidate_before_script:
            self.set_update(candidate_id=NEW_CANDIDATE)
        await self.scripts['core_update_approve'].async_run(dict(call.data), Context())

    async def set_text(self, call):
        await self.record(call)
        entity_id = call.data['entity_id']
        if isinstance(entity_id, list):
            self.assertEqual(len(entity_id), 1)
            entity_id = entity_id[0]
        self.hass.states.async_set(entity_id, call.data['value'])

    async def run_automation(self, suffix, event_data=None):
        config = next(item for item in self.package['automation'] if item['id'].endswith(suffix))
        script = self.make_script(config.get('conditions', []) + config['actions'], config['id'])
        variables = {'trigger': {'event': {'data': event_data or {}}}}
        await script.async_run(variables, Context())

    async def action(self, kind='INSTALL', candidate=CANDIDATE, version=VERSION):
        await self.run_automation('mobile_action', {'action': f'HA_CORE:{kind}:{candidate}:{version}'})

    def selected(self, name):
        return [data for service, data in self.calls if service == name]

    async def test_available_notifies_once_without_installing(self):
        await self.run_automation('available')
        await self.run_automation('available')
        self.assertEqual(len(self.selected('persistent_notification.create')), 1)
        self.assertEqual(len(self.selected('script.notify_optional')), 1)
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])
        notification = self.selected('script.notify_optional')[0]
        self.assertEqual(notification['notify_service'], 'notify.mobile_app_example_phone')
        action = notification['notify_data']['data']['actions'][0]
        self.assertEqual(action['action'], f'HA_CORE:INSTALL:{CANDIDATE}:{VERSION}')
        self.assertTrue(action['authenticationRequired'])

    async def test_new_candidate_same_version_gets_new_notification(self):
        await self.run_automation('available')
        self.set_update(candidate_id=NEW_CANDIDATE)
        await self.run_automation('available')
        self.assertEqual(len(self.selected('script.notify_optional')), 2)

    async def test_approval_preserves_exact_candidate_and_version(self):
        await self.action()
        self.assertEqual(self.selected('rest_command.core_update_approve'), [
            {'candidate_id': CANDIDATE, 'target_version': VERSION}])
        self.assertEqual(self.selected('persistent_notification.create')[-1]['title'],
                         'Home Assistant update approved')

    async def test_older_version_notification_cannot_approve_new_version(self):
        self.set_update(candidate_id=NEW_CANDIDATE, target_version='2026.10.1', latest_version='2026.10.1')
        await self.action()
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])

    async def test_replaced_candidate_same_version_is_stale(self):
        self.set_update(candidate_id=NEW_CANDIDATE)
        await self.action()
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])

    async def test_second_validation_catches_candidate_change_before_script(self):
        self.change_candidate_before_script = True
        await self.action()
        self.assertEqual(len(self.selected('script.core_update_approve')), 1)
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])

    async def test_later_only_dismisses_notification(self):
        await self.action('LATER')
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])
        self.assertEqual(self.selected('script.core_update_approve'), [])
        self.assertEqual(self.selected('persistent_notification.dismiss'), [
            {'notification_id': 'core_update_available'}])
        self.assertEqual(self.selected('script.notify_optional')[0]['notify_data']['message'],
                         'clear_notification')

    async def test_stale_later_does_not_dismiss_current_offer(self):
        self.set_update(candidate_id=NEW_CANDIDATE)
        await self.action('LATER')
        self.assertEqual(self.calls, [])

    async def test_empty_or_unrelated_event_cannot_approve(self):
        for event in ({}, {'action': 'OTHER'}, {'action': 'HA_CORE:INSTALL'},
                      {'action': None}, {'action': []},
                      {'action': f'HA_CORE:DISMISS:{CANDIDATE}:{VERSION}'}):
            await self.run_automation('mobile_action', event)
        self.assertEqual(self.calls, [])

    async def test_invalid_or_missing_script_arguments_cannot_approve(self):
        for arguments in ({}, {'candidate_id': CANDIDATE},
                          {'candidate_id': [], 'target_version': VERSION},
                          {'candidate_id': CANDIDATE, 'target_version': '2026.10.1'}):
            await self.scripts['core_update_approve'].async_run(arguments, Context())
        self.assertEqual(self.calls, [])

    async def test_unavailable_busy_or_inconsistent_status_cannot_approve(self):
        for state, attributes in (
            ('unavailable', {}), ('unknown', {}), ('off', {}),
            ('on', {'stage': 'deploying'}), ('on', {'stage': 'failed'}),
            ('on', {'in_progress': True}), ('on', {'latest_version': '2026.10.1'}),
        ):
            self.set_update(state, **attributes)
            await self.action()
        self.assertEqual(self.calls, [])

    async def test_rejected_approval_does_not_claim_accepted_or_dismiss_offer(self):
        for status in (403, 409, 422, 503):
            self.calls.clear()
            self.status = status
            await self.action()
            self.assertEqual(self.selected('persistent_notification.dismiss'), [])
            note = self.selected('persistent_notification.create')[0]
            self.assertEqual(note['title'], 'Home Assistant update was not accepted')
            self.assertIn(str(status), note['message'])

    async def test_running_or_unavailable_hacs_blocks_core_approval(self):
        cases = [
            ('automation.renamed_hacs_coordinator', 'on', {'id': 'ha_homelab_hacs_maintenance_update', 'current': 1}),
            ('automation.renamed_hacs_coordinator', 'unavailable', {'id': 'ha_homelab_hacs_maintenance_update', 'current': 0}),
            ('script.hacs_maintenance_backup', 'on', {'current': 1}),
            ('script.hacs_maintenance_install', 'off', {'current': 1}),
            ('script.hacs_maintenance_install', 'unavailable', {}),
        ]
        for entity, state, attributes in cases:
            with self.subTest(entity=entity, state=state):
                self.calls.clear()
                self.hass.states.async_set(entity, state, attributes)
                await self.action()
                self.assertEqual(self.selected('rest_command.core_update_approve'), [])
                self.hass.states.async_remove(entity)

    async def test_idle_hacs_coordinator_and_workers_allow_core_approval(self):
        self.hass.states.async_set('automation.renamed_hacs_coordinator', 'on', {
            'id': 'ha_homelab_hacs_maintenance_update', 'current': 0,
        })
        for entity in ('script.hacs_maintenance_backup', 'script.hacs_maintenance_install'):
            self.hass.states.async_set(entity, 'off', {'current': 0})
        await self.action()
        self.assertEqual(len(self.selected('rest_command.core_update_approve')), 1)

    async def test_lost_response_does_not_retry_or_claim_failure(self):
        self.transport_error = True
        await self.action()
        self.assertEqual(len(self.selected('rest_command.core_update_approve')), 1)
        self.assertEqual(self.selected('persistent_notification.dismiss'), [])
        note = self.selected('persistent_notification.create')[0]
        self.assertIn('not confirmed', note['title'])
        self.assertIn('may already have accepted', note['message'])

    async def test_success_after_restart_notifies_once(self):
        self.set_update('off', stage='succeeded', installed_version=VERSION)
        await self.run_automation('result')
        await self.run_automation('result')
        self.assertEqual(len(self.selected('script.notify_optional')), 1)
        self.assertEqual(self.selected('rest_command.core_update_approve'), [])
        self.assertIn('passed', self.selected('script.notify_optional')[0]['notify_data']['message'])

    async def test_failure_and_recovery_show_distinct_non_success_results(self):
        self.set_update(stage='failed', last_error='Preflight configuration check failed')
        await self.run_automation('result')
        self.set_update(stage='recovery_required', last_error='Database migration cannot be reversed automatically')
        await self.run_automation('result')
        notes = self.selected('script.notify_optional')
        self.assertEqual(len(notes), 2)
        self.assertIn('failed', notes[0]['notify_data']['title'])
        self.assertIn('recovery', notes[1]['notify_data']['title'])
        self.assertIn('Manual recovery', notes[1]['notify_data']['message'])

    async def test_progress_never_generates_success_notification(self):
        for stage in ('idle', 'preparing', 'backup', 'preflight', 'deploying', 'verifying'):
            self.set_update(stage=stage, in_progress=True)
            await self.run_automation('available')
            await self.run_automation('result')
        self.assertEqual(self.calls, [])

    async def test_unavailable_retained_success_is_not_reported(self):
        self.set_update('unavailable', stage='succeeded')
        await self.run_automation('result')
        self.assertEqual(self.calls, [])

    async def test_component_schemas_accept_package(self):
        from homeassistant.components.input_text import CONFIG_SCHEMA as input_text_schema
        from homeassistant.components.rest_command import CONFIG_SCHEMA as rest_schema
        from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA as script_schema
        input_text_schema({'input_text': self.package['input_text']})
        rest_schema({'rest_command': self.package['rest_command']})
        for config in self.package['script'].values():
            script_schema(config)

    async def test_transport_serializes_exact_three_field_json(self):
        from homeassistant.helpers.template import Template
        template = Template(self.package['rest_command']['core_update_approve']['payload'], self.hass)
        body = template.async_render({'candidate_id': CANDIDATE, 'target_version': VERSION}, parse_result=False)
        self.assertEqual(json.loads(body), {
            'action': 'install', 'candidate_id': CANDIDATE, 'target_version': VERSION})


if __name__ == '__main__':
    unittest.main()
