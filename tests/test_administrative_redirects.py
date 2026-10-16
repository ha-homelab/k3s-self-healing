"""Real HTTP administrative clients reject redirects before sending credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from disaster_recovery import kubernetes

sys.path.insert(0, str(Path(__file__).parents[1] / 'core_updates'))
try:
    import runtime
finally:
    sys.path.pop(0)


class AdministrativeRedirectTests(unittest.TestCase):
    def test_normal_endpoint_works_but_redirect_never_receives_token(self):
        destination_requests = []

        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                destination_requests.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *_):
                pass

        destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
        target = f'http://127.0.0.1:{destination.server_port}/credential-target'

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 200 if self.path == '/ok' else int(self.path[1:])
                self.send_response(status)
                if status != 200:
                    self.send_header('Location', target)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *_):
                pass

        origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (origin, destination)]
        for thread in threads:
            thread.start()
        try:
            for module in (runtime, kubernetes):
                with module.urlopen(f'http://127.0.0.1:{origin.server_port}/ok', timeout=2) as response:
                    self.assertEqual(response.read(), b'{}')
                for code in (301, 302, 303, 307, 308):
                    request = Request(f'http://127.0.0.1:{origin.server_port}/{code}', headers={'Authorization': 'Bearer synthetic-token'})
                    with self.subTest(module=module.__name__, code=code), self.assertRaises(HTTPError) as error:
                        module.urlopen(request, timeout=2)
                    self.assertEqual(error.exception.code, code)
            self.assertEqual(destination_requests, [])
        finally:
            for server in (origin, destination):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)
