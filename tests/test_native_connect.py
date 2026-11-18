"""Initial connection recovery must not repeat an authenticated backup session."""
import asyncio
import errno
import importlib.util
from pathlib import Path
import socket
import ssl
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
spec = importlib.util.spec_from_file_location('connect_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ConnectorError(Exception):
    def __init__(self, os_error):
        self.os_error = os_error


class ClientSSLError(ConnectorError):
    pass


class NativeConnectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fake = types.SimpleNamespace(ClientConnectorError=ConnectorError, ClientSSLError=ClientSSLError)
        self.modules = patch.dict(sys.modules, {'aiohttp': fake})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_error_classification_is_narrow(self):
        for code in (socket.EAI_AGAIN, socket.EAI_FAIL):
            self.assertTrue(runner.transient_connect_error(ConnectorError(socket.gaierror(code, 'DNS'))))
        for code in (errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
                     errno.EHOSTUNREACH, errno.ENETUNREACH):
            self.assertTrue(runner.transient_connect_error(ConnectorError(OSError(code, 'connect'))))
        for error in (ConnectorError(socket.gaierror(socket.EAI_NONAME, 'unknown host')),
                      ConnectorError(OSError(errno.EACCES, 'permission')),
                      ClientSSLError(OSError(errno.ECONNRESET, 'TLS')),
                      ssl.SSLCertVerificationError('certificate'),
                      RuntimeError('redirect'), ValueError('HTTP rejection')):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(runner.transient_connect_error(error))

    async def test_nonretryable_http_redirect_and_tls_failures_stop_first_attempt(self):
        for failure in (RuntimeError('redirect'), ValueError('HTTP 401'),
                        ClientSSLError(OSError(errno.ECONNRESET, 'TLS'))):
            calls = []

            class Session:
                def ws_connect(self, url):
                    calls.append(url)
                    raise failure

            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                async with runner.initial_native_socket(Session(), 'ws://offline.invalid'):
                    self.fail('Handshake failed')
            self.assertEqual(len(calls), 1)

    async def test_total_budget_bounds_a_stalled_handshake(self):
        calls = []

        class Handshake:
            async def __aenter__(self):
                await asyncio.Event().wait()

            async def __aexit__(self, *args):
                pass

        class Session:
            def ws_connect(self, url):
                calls.append(url)
                return Handshake()

        with patch.multiple(runner, CONNECT_BUDGET_SECONDS=0.025,
                            CONNECT_TIMEOUT_SECONDS=10, CONNECT_RETRY_DELAYS=(2, 5)):
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(0.5):
                    async with runner.initial_native_socket(Session(), 'ws://offline.invalid'):
                        self.fail('No connection')
        self.assertEqual(len(calls), 1)

    async def test_cancellation_propagates_without_another_attempt(self):
        entered = asyncio.Event()
        calls = []

        class Handshake:
            async def __aenter__(self):
                entered.set()
                await asyncio.Event().wait()

            async def __aexit__(self, *args):
                pass

        class Session:
            def ws_connect(self, url):
                calls.append(url)
                return Handshake()

        async def connect():
            async with runner.initial_native_socket(Session(), 'ws://offline.invalid'):
                self.fail('No connection')

        task = asyncio.create_task(connect())
        await asyncio.wait_for(entered.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 1)

    async def test_socket_closes_and_session_body_failure_is_not_retried(self):
        calls, closed = [], []

        class Handshake:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                closed.append(args[0])

        class Session:
            def ws_connect(self, url):
                calls.append(url)
                return Handshake()

        with self.assertRaises(TimeoutError):
            async with runner.initial_native_socket(Session(), 'ws://offline.invalid'):
                raise TimeoutError('backup acknowledgement unknown')
        self.assertEqual(len(calls), 1)
        self.assertEqual(closed, [TimeoutError])
