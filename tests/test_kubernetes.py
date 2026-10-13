"""Offline Kubernetes read/retry and checkpoint transport contracts."""

import asyncio
import errno
import io
import json
import ssl
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from disaster_recovery import kubernetes
from disaster_recovery.kubernetes import Kubernetes


class FakeSocket:
    def __init__(self, messages=(), *, stall=None):
        self.messages = iter(messages)
        self.stall = stall
        self.closed = False

    async def __aenter__(self):
        if self.stall == "handshake":
            await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            if self.stall == "receive":
                await asyncio.Event().wait()
            raise StopAsyncIteration


class FakeSession:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def ws_connect(self, url, **options):
        self.url = url
        self.options = options
        return self.socket


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Kubernetes.__new__(Kubernetes)
        self.client.base = "https://offline.invalid:443"
        self.client.context = object()
        self.client.headers = lambda: {"Authorization": "Bearer offline-fixture"}

    async def call(self, socket, *, shorten_deadline=False):
        session = FakeSession(socket)
        fake_aiohttp = SimpleNamespace(
            ClientSession=lambda **_: session,
            ClientTimeout=lambda **values: values,
            WSMsgType=SimpleNamespace(BINARY=2, ERROR=258),
        )
        real_timeout = asyncio.timeout
        requested_deadlines = []

        def deadline(seconds):
            requested_deadlines.append(seconds)
            return real_timeout(0.01 if shorten_deadline else seconds)

        try:
            with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}), patch.object(
                asyncio, "timeout", side_effect=deadline
            ):
                return await self.client._exec("homeassistant", "broker", "mosquitto", ["fixture"])
        finally:
            self.assertEqual(requested_deadlines, [30])
            self.assertTrue(session.closed)
            self.assertIs(session.options["ssl"], self.client.context)
            self.assertEqual(session.options["protocols"], ["v4.channel.k8s.io"])

    async def test_silent_handshake_or_upgraded_stream_times_out_and_closes(self):
        for stall in ("handshake", "receive"):
            with self.subTest(stall=stall):
                socket = FakeSocket(stall=stall)
                with self.assertRaises(TimeoutError):
                    await self.call(socket, shorten_deadline=True)
                if stall == "receive":
                    self.assertTrue(socket.closed)

    async def test_success_status_without_eof_still_has_bounded_wait(self):
        status = SimpleNamespace(type=2, data=b"\x03" + json.dumps({"status": "Success"}).encode())
        socket = FakeSocket([status], stall="receive")
        with self.assertRaises(TimeoutError):
            await self.call(socket, shorten_deadline=True)
        self.assertTrue(socket.closed)

    async def test_completed_success_returns_and_missing_status_fails_closed(self):
        status = SimpleNamespace(type=2, data=b"\x03" + json.dumps({"status": "Success"}).encode())
        socket = FakeSocket([status])
        self.assertEqual(await self.call(socket), "")
        self.assertTrue(socket.closed)
        with self.assertRaisesRegex(RuntimeError, "checkpoint command failed"):
            await self.call(FakeSocket())


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = Kubernetes.__new__(Kubernetes)
        self.client.base = "https://offline.invalid:443"
        self.client.context = object()
        self.client.headers = lambda: {"Authorization": "Bearer offline-fixture"}

    def test_allowed_direct_and_wrapped_network_errors_retry_with_tls_intact(self):
        reasons = [OSError(code, "offline transient fixture") for code in (
            errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNREFUSED,
            errno.ECONNRESET, errno.ETIMEDOUT,
        )] + [TimeoutError("offline timeout fixture")]
        for reason in reasons:
            for error in (reason, URLError(reason)):
                with self.subTest(reason=type(reason).__name__, wrapped=isinstance(error, URLError)):
                    with patch.object(kubernetes, "urlopen", side_effect=[
                        error, io.BytesIO(b'{"kind":"Node"}'),
                    ]) as opened, patch.object(kubernetes.time, "sleep") as sleep:
                        self.assertEqual(self.client.get("/api/v1/nodes/example-ha-node"), {"kind": "Node"})
                    self.assertEqual(opened.call_count, 2)
                    sleep.assert_called_once_with(1)
                    for call in opened.call_args_list:
                        self.assertEqual(call.args[0].get_method(), "GET")
                        self.assertIs(call.kwargs["context"], self.client.context)
                        self.assertEqual(call.kwargs["timeout"], 60)

    def test_persistent_transport_failure_stops_after_five_attempts(self):
        error = URLError(OSError(errno.EHOSTUNREACH, "offline fixture"))
        with patch.object(kubernetes, "urlopen", side_effect=error) as opened, patch.object(
            kubernetes.time, "sleep"
        ) as sleep:
            with self.assertRaises(URLError) as caught:
                self.client.get("/api/v1/nodes/example-ha-node")
        self.assertIs(caught.exception, error)
        self.assertEqual(opened.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8])

    def test_http_certificate_and_other_os_errors_do_not_retry(self):
        # Even a certificate error with a retryable numeric errno must fail closed.
        certificate = ssl.SSLCertVerificationError(errno.EHOSTUNREACH, "offline certificate fixture")
        errors = [certificate, URLError(certificate), OSError(errno.EACCES, "offline denied fixture")]
        errors += [HTTPError(self.client.base, code, "offline HTTP fixture", {}, None)
                   for code in (401, 403, 500)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(kubernetes, "urlopen", side_effect=error) as opened, patch.object(
                    kubernetes.time, "sleep"
                ) as sleep:
                    with self.assertRaises(type(error)) as caught:
                        self.client.get("/api/v1/nodes/example-ha-node")
                self.assertIs(caught.exception, error)
                self.assertEqual(opened.call_count, 1)
                sleep.assert_not_called()

    def test_invalid_json_fails_without_retry_and_closes_response(self):
        response = io.BytesIO(b'not-json')
        with patch.object(kubernetes, "urlopen", return_value=response) as opened, patch.object(
            kubernetes.time, "sleep"
        ) as sleep:
            with self.assertRaises(json.JSONDecodeError):
                self.client.get("/api/v1/nodes/example-ha-node")
        self.assertTrue(response.closed)
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_reset_while_reading_body_closes_response_and_retries_get(self):
        class ResetResponse(io.BytesIO):
            def read(self, *args):
                raise ConnectionResetError(errno.ECONNRESET, "offline body reset")

        interrupted = ResetResponse()
        with patch.object(kubernetes, "urlopen", side_effect=[
            interrupted, io.BytesIO(b'{"items":[]}'),
        ]) as opened, patch.object(kubernetes.time, "sleep") as sleep:
            self.assertEqual(self.client.get("/api/v1/pods"), {"items": []})
        self.assertTrue(interrupted.closed)
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
