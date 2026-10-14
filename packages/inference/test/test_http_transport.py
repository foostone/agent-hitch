import http.client
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hitch_inference.gateway import GatewayServer


class NumericBindingTests(unittest.TestCase):
    def test_gateway_listens_and_serves_when_reverse_dns_is_unavailable(self):
        with patch("socket.getfqdn", side_effect=AssertionError("reverse DNS must not gate startup")), \
                patch("socket.gethostbyaddr", side_effect=AssertionError("reverse DNS is unavailable")):
            server = GatewayServer(("127.0.0.1", 0), SimpleNamespace(draining=False), {})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"status": "ready"})
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
