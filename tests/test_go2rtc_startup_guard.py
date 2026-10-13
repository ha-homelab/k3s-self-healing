"""Exercise readiness failures, cleanup and safe application of the startup guard."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("go2rtc_guard", ROOT / "helm/go2rtc-startup-guard/patch.py")
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
SCHEMES = {"exec", "ffmpeg", "http", "onvif", "rtsp", "webrtc"}
SOURCE = (GUARD.IMPORT + '_SUCCESSFUL_BOOT_MESSAGE = "INF [api] listen addr="\n'
          '_APP_MODULES = (\n    "ffmpeg",\n    "onvif",\n    "webrtc",\n)\n'
          'class Server:\n    async def _start(self):\n'
          '        await self._startup_complete.wait()\n' + GUARD.BEFORE + '\n')


class ClientError(Exception):
    pass


class StartError(Exception):
    pass


def http_error(status):
    error = ClientError()
    error.__cause__ = Exception()
    error.__cause__.status = status
    return error


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def make(self, results, timeout=1):
        client = SimpleNamespace(validate_server_version=AsyncMock(),
                                 schemes=SimpleNamespace(list=AsyncMock(side_effect=results)),
                                 streams=SimpleNamespace(list=AsyncMock(return_value={})),
                                 preload=SimpleNamespace(list=AsyncMock(return_value={})))
        namespace = {'asyncio': asyncio, '_SETUP_TIMEOUT': timeout,
                     'Go2RtcRestClient': lambda *_: client, 'HA_MANAGED_URL': 'http://local/',
                     'Go2RtcClientError': ClientError, 'Go2RTCServerStartError': StartError}
        exec('async def check(self):\n' + textwrap.indent(textwrap.dedent(GUARD.AFTER), '    '), namespace)
        server = SimpleNamespace(_session=None, _stop=AsyncMock())
        return client, server, namespace['check']

    async def test_ready_server_is_not_delayed_or_stopped(self):
        client, server, check = self.make([SCHEMES])
        await check(server)
        client.streams.list.assert_awaited_once()
        client.preload.list.assert_awaited_once()
        server._stop.assert_not_awaited()

    async def test_early_404_503_and_incomplete_schemes_wait_for_all_apis(self):
        client, server, check = self.make([http_error(404), http_error(503), {'rtsp'}, SCHEMES])
        await check(server)
        self.assertEqual(client.schemes.list.await_count, 4)
        client.preload.list.assert_awaited_once()
        server._stop.assert_not_awaited()

    async def test_missing_stream_route_is_retried(self):
        client, server, check = self.make([SCHEMES, SCHEMES])
        client.streams.list.side_effect = [http_error(404), {}]
        await check(server)
        self.assertEqual(client.streams.list.await_count, 2)

    async def test_auth_failure_fails_immediately_and_stops_child(self):
        client, server, check = self.make([http_error(401)])
        with self.assertRaises(StartError):
            await check(server)
        self.assertEqual(client.schemes.list.await_count, 1)
        server._stop.assert_awaited_once()

    async def test_incomplete_server_times_out_and_stops_child(self):
        client, server, check = self.make(None, timeout=0.02)
        client.schemes.list.return_value = {'rtsp'}
        with self.assertRaises(StartError):
            await check(server)
        server._stop.assert_awaited_once()

    async def test_cancellation_stops_child_and_propagates(self):
        client, server, check = self.make([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await check(server)
        server._stop.assert_awaited_once()


class PatchingTests(unittest.TestCase):
    def test_guard_is_idempotent_and_preserves_other_methods(self):
        extra = '\n    async def unrelated(self):\n        return 123\n'
        patched, status = GUARD.patched_source(SOURCE + extra)
        self.assertEqual(status, 'patched')
        self.assertTrue(patched.endswith(extra))
        self.assertEqual(GUARD.patched_source(patched), (patched, 'already-patched'))

    def test_changed_upstream_is_not_replaced(self):
        for source in (SOURCE.replace('class Server:', 'class Different:'),
                       SOURCE.replace('validate_server_version()', 'check_version()'),
                       SOURCE.replace('    "ffmpeg",', '    "different",')):
            self.assertEqual(GUARD.patched_source(source), (source, 'unsupported'))

    def test_atomic_application_preserves_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'server.py'
            p.write_text(SOURCE)
            p.chmod(0o640)
            self.assertEqual(GUARD.patch_file(p), 'patched')
            self.assertEqual(GUARD.patch_file(p), 'already-patched')
            self.assertEqual(p.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(Path(d).iterdir()), [p])
