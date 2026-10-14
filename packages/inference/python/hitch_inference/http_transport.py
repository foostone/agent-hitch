"""HTTP binding without reverse-DNS dependencies in local service startup."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class InferenceHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer resolves a display name with getfqdn(), which can block
        # indefinitely even for 127.0.0.1. Node/model identity comes from the
        # frozen lock; a reverse lookup has no role in admitting this listener.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
