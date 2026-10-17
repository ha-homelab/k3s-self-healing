"""Real administrative WebSockets stay on the selected endpoint."""
import json
from pathlib import Path
import sys
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from disaster_recovery.kubernetes import Kubernetes

sys.path.insert(0, str(Path(__file__).parents[1] / 'core_updates'))
try:
    from runtime import Runtime
finally:
    sys.path.pop(0)


class WebSocketRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redirect_status = None
        self.destination_requests = 0
        self.auth_messages = []

        async def destination(request):
            self.destination_requests += 1
            return await self.socket(request)

        app = web.Application()
        app.router.add_get('/{tail:.*}', destination)
        self.destination = TestServer(app)
        await self.destination.start_server()
        self.addAsyncCleanup(self.destination.close)

        async def origin(request):
            if self.redirect_status:
                return web.Response(status=self.redirect_status, headers={
                    'Location': str(self.destination.make_url(request.path))})
            return await self.socket(request)

        app = web.Application()
        app.router.add_get('/{tail:.*}', origin)
        self.origin = TestServer(app)
        await self.origin.start_server()
        self.addAsyncCleanup(self.origin.close)

    async def socket(self, request):
        socket = web.WebSocketResponse(protocols=['v4.channel.k8s.io'])
        await socket.prepare(request)
        if request.path == '/api/websocket':
            await socket.send_json({'type': 'auth_required'})
            self.auth_messages.append(await socket.receive_json())
            await socket.send_json({'type': 'auth_ok'})
            query = await socket.receive_json()
            await socket.send_json({'id': query['id'], 'success': True, 'result': []})
        else:
            await socket.send_bytes(b'\x03' + json.dumps({'status': 'Success'}).encode())
        await socket.close()
        return socket

    def clients(self):
        ha = Runtime.__new__(Runtime)
        ha.ha_url = str(self.origin.make_url('')).rstrip('/')
        ha.ha_token = 'synthetic-ha-token'
        kube = Kubernetes.__new__(Kubernetes)
        kube.base = str(self.origin.make_url('')).rstrip('/')
        kube.context = True
        kube.headers = lambda: {'Authorization': 'Bearer synthetic-kube-token'}
        return ha, kube

    async def test_direct_endpoints_keep_working(self):
        ha, kube = self.clients()
        self.assertEqual(await ha._ha_entries(), [])
        self.assertEqual(await kube._exec('homeassistant', 'broker', 'mosquitto', ['true']), '')
        self.assertEqual(self.destination_requests, 0)
        self.assertEqual(self.auth_messages, [{'type': 'auth', 'access_token': 'synthetic-ha-token'}])

    async def test_all_redirect_statuses_fail_before_contacting_destination(self):
        ha, kube = self.clients()
        for status in (301, 302, 303, 307, 308):
            self.redirect_status = status
            for name, call in [('HA', ha._ha_entries), ('Kubernetes', lambda: kube._exec(
                    'homeassistant', 'broker', 'mosquitto', ['true']))]:
                with self.subTest(status=status, client=name):
                    with self.assertRaisesRegex(RuntimeError, 'WebSocket redirects'):
                        await call()
        self.assertEqual(self.destination_requests, 0)
        self.assertEqual(self.auth_messages, [])
