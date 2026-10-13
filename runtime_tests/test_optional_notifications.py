"""Exercise the actual optional-notification helper, including late providers."""
import logging
import os
import shutil
from pathlib import Path
import tempfile
import unittest
import yaml

try:
    from homeassistant import loader
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant, Context
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.setup import async_setup_component
    AVAILABLE = True
except ImportError:
    if os.environ.get('REQUIRE_HA_RUNTIME') == '1':
        raise
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'Home Assistant runtime required')
class OptionalNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.config.skip_pip = True
        self.assertTrue(await async_load_base_functionality(self.hass))
        scripts = Path(self.temp.name) / 'python_scripts'
        scripts.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] /
                        'homeassistant/python_scripts/notification_service_available.py',
                        scripts / 'notification_service_available.py')
        self.assertTrue(await async_setup_component(self.hass, 'python_script', {'python_script': {}}))
        package = yaml.safe_load((Path(__file__).resolve().parents[1] /
                                  'homeassistant/packages/optional_notifications.yaml').read_text())
        self.worker = Script(self.hass, cv.SCRIPT_SCHEMA(package['script']['notify_optional']['sequence']),
                             'optional notify', 'script', logger=logging.getLogger('test.optional'))
        self.calls = []

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def invoke(self, service='notify.example_phone'):
        await self.worker.async_run({'notify_service': service,
                                     'notify_data': {'message': 'fixture'}}, Context())

    async def record(self, call):
        self.calls.append(dict(call.data))

    async def test_missing_provider_is_skipped_and_late_provider_is_used(self):
        await self.invoke()
        self.assertEqual(self.calls, [])
        self.hass.services.async_register('notify', 'example_phone', self.record)
        await self.invoke()
        self.assertEqual(self.calls, [{'message': 'fixture'}])

    async def test_removed_provider_is_not_cached(self):
        self.hass.services.async_register('notify', 'example_phone', self.record)
        await self.invoke()
        self.hass.services.async_remove('notify', 'example_phone')
        await self.invoke()
        self.assertEqual(len(self.calls), 1)

    async def test_other_service_domains_are_not_called(self):
        self.hass.services.async_register('homeassistant', 'restart', self.record)
        await self.invoke('homeassistant.restart')
        self.assertEqual(self.calls, [])

    async def test_delivery_error_is_isolated(self):
        async def fail(call):
            raise HomeAssistantError('Synthetic provider outage')
        self.hass.services.async_register('notify', 'example_phone', fail)
        await self.invoke()
