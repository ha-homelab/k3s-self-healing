"""Small authenticated Kubernetes reader; the only write operation is MQTT checkpointing."""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener)


class NoRedirect(HTTPRedirectHandler):
    """Never forward administrative credentials to a redirect target."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


async def reject_websocket_redirect(_session, _context, params):
    """Stop the upgrade before contacting a redirected administrative endpoint."""
    params.response.close()
    raise RuntimeError('Administrative WebSocket redirects are not allowed')


def urlopen(request, *, context=None, timeout=30):
    """Open the selected endpoint directly, retaining normal TLS verification."""
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()  # Only status/headers are used; do not retain private error bodies.
        raise


class Kubernetes:
    def __init__(self):
        service_account = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.token_path = service_account / 'token'
        self.context = ssl.create_default_context(cafile=str(service_account / 'ca.crt'))
        self.base = 'https://' + os.environ['KUBERNETES_SERVICE_HOST'] + ':' + os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')

    def headers(self):
        return {'Authorization': 'Bearer ' + self.token_path.read_text().strip()}

    def get(self, path):
        # A newly started Pod may briefly precede its network-policy rules. Only
        # retry transient transport failures of this read-only GET, never exec.
        transient_errnos = {
            errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNREFUSED,
            errno.ECONNRESET, errno.ETIMEDOUT,
        }
        for attempt in range(5):
            request = Request(self.base + path, headers=self.headers())
            try:
                with urlopen(request, context=self.context, timeout=60) as response:
                    return json.load(response)
            except OSError as error:
                reason = error.reason if isinstance(error, URLError) else error
                if isinstance(error, HTTPError) or isinstance(reason, ssl.SSLError):
                    raise
                transient = isinstance(reason, TimeoutError) or (
                    isinstance(reason, OSError) and reason.errno in transient_errnos
                )
                if not transient or attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    def list(self, path):
        items = []
        cursor = ''
        while True:
            query = {'limit': '500'}
            if cursor:
                query['continue'] = cursor
            result = self.get(path + '?' + urlencode(query))
            items.extend(result['items'])
            cursor = result.get('metadata', {}).get('continue', '')
            if not cursor:
                return {'apiVersion': result['apiVersion'], 'kind': result['kind'], 'items': items}

    async def _exec(self, namespace, pod, container, command):
        # aiohttp ships with the pinned Home Assistant runtime image.
        import aiohttp
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_websocket_redirect)
        query = [('container', container), ('stdout', 'true'), ('stderr', 'true')]
        query += [('command', value) for value in command]
        path = '/api/v1/namespaces/' + quote(namespace, safe='') + '/pods/' + quote(pod, safe='') + '/exec?'
        url = self.base.replace('https://', 'wss://', 1) + path + urlencode(query)
        status = None
        output = bytearray()
        # ClientTimeout bounds the HTTP handshake, but an upgraded WebSocket can
        # otherwise receive forever. Bound the complete command, including EOF.
        async with asyncio.timeout(30):
            async with aiohttp.ClientSession(headers=self.headers(), timeout=aiohttp.ClientTimeout(total=30),
                                             trace_configs=[trace], trust_env=False) as session:
                async with session.ws_connect(url, ssl=self.context, protocols=['v4.channel.k8s.io']) as socket:
                    async for message in socket:
                        if message.type == aiohttp.WSMsgType.BINARY and message.data:
                            channel, data = message.data[0], message.data[1:]
                            if channel == 3:
                                status = json.loads(data)
                            elif channel in (1, 2):
                                output.extend(data)
                                if len(output) > 4096:
                                    raise RuntimeError('Unexpected checkpoint command output')
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError('Checkpoint exec websocket failed')
        if not status or status.get('status') != 'Success':
            raise RuntimeError('MQTT checkpoint command failed; private output is not logged')
        return output.decode('utf-8', errors='replace').strip()

    def checkpoint_mosquitto(self, pod):
        # Signal only a verified PID 1 broker. SIGUSR1 writes persistence; it does
        # not restart the broker, reload its configuration or publish a command.
        namespace = pod['metadata']['namespace']
        name = pod['metadata']['name']
        output = asyncio.run(self._exec(namespace, name, 'mosquitto',
            ['sh', '-c', 'test "$(cat /proc/1/comm)" = mosquitto && kill -USR1 1']))
        if output:
            raise RuntimeError('Unexpected MQTT checkpoint output')
        return {'namespace': namespace, 'pod': name, 'uid': pod['metadata']['uid'], 'signal': 'SIGUSR1'}
