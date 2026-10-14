"""Bounded OpenAI HTTP relay. Client cancellation closes only its upstream socket."""
import http.client
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from .http_transport import InferenceHTTPServer
from .request_validation import validate_request


class AdmissionError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def disconnected(sock):
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready) and not sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except (OSError, ValueError):
        return True


class Router:
    def __init__(self, targets, *, running, queued, queue_timeout, request_timeout, engine_capacity, frozen=None):
        self.targets = targets
        self.frozen = frozen
        self.running, self.queued = running, queued
        self.queue_timeout, self.request_timeout = queue_timeout, request_timeout
        self.engine_capacity = engine_capacity
        self.condition = threading.Condition()
        self.active = [0] * len(targets)
        self.waiting = 0
        self.draining = False
        self.cursor = 0

    def acquire(self, sock):
        deadline = time.monotonic() + self.queue_timeout
        with self.condition:
            pending = False
            try:
                while True:
                    if self.draining:
                        raise AdmissionError(503, "deployment_draining")
                    if disconnected(sock):
                        raise AdmissionError(499, "request_cancelled")
                    eligible = [i for i, n in enumerate(self.active) if n < self.engine_capacity]
                    if sum(self.active) < self.running and eligible:
                        ordered = sorted(eligible, key=lambda i: (self.active[i], (i - self.cursor) % len(self.targets)))
                        index = ordered[0]
                        self.cursor = (index + 1) % len(self.targets)
                        self.active[index] += 1
                        return index
                    if not pending:
                        if self.waiting >= self.queued:
                            raise AdmissionError(429, "inference_queue_full")
                        self.waiting += 1
                        pending = True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdmissionError(504, "inference_queue_timeout")
                    self.condition.wait(min(0.05, remaining))
            finally:
                if pending:
                    self.waiting -= 1

    def release(self, index):
        with self.condition:
            self.active[index] -= 1
            self.condition.notify_all()

    def drain(self):
        with self.condition:
            self.draining = True
            self.condition.notify_all()

    def relay(self, handler, route, body=None, *, admin=False):
        admission = route in ("/v1/chat/completions", "/v1/responses")
        if body is not None and self.frozen:
            expected_route = "/v1/chat/completions" if self.frozen["protocol"]["api"] == "chat-completions" else "/v1/responses"
            if route != expected_route:
                raise AdmissionError(400, "inference_protocol_conflict")
            try:
                body = json.dumps(validate_request(json.loads(body), self.frozen), separators=(",", ":")).encode()
            except ValueError as error:
                raise AdmissionError(400, getattr(error, "code", "invalid_request")) from error
        index = self.acquire(handler.connection) if admission else 0
        target = self.targets[index]
        connection = None
        finished = threading.Event()
        timer = None
        started = False
        try:
            url = urlsplit(target["baseUrl"])
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=self.request_timeout)
            token = target["adminToken"] if admin else target["engineToken"]
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Connection": "close"}
            if body is not None:
                payload = json.loads(body)
                payload["model"] = target["wireModel"]
                body = json.dumps(payload, separators=(",", ":")).encode()
            connection.request(handler.command, route, body=body, headers=headers)
            upstream_socket = connection.sock

            def watch():
                deadline = time.monotonic() + self.request_timeout
                while not finished.wait(0.05):
                    if disconnected(handler.connection) or time.monotonic() >= deadline:
                        if upstream_socket:
                            try:
                                upstream_socket.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        connection.close()
                        return

            timer = threading.Thread(target=watch, daemon=True)
            timer.start()
            response = connection.getresponse()
            handler.send_response(response.status)
            for key in ("Content-Type", "Content-Length", "Cache-Control"):
                if response.getheader(key) is not None:
                    handler.send_header(key, response.getheader(key))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            started = True
            while True:
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError):
            if not started and not disconnected(handler.connection):
                handler.error(502, "inference_upstream_unavailable")
        finally:
            finished.set()
            if connection:
                connection.close()
            if timer:
                timer.join(timeout=0.2)
            if admission:
                self.release(index)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def error(self, status, code):
        # Rejecting a POST before consuming its bounded body must not poison a
        # pooled HTTP/1.1 connection with leftover request bytes.
        self.close_connection = True
        self.reply(status, {"error": {"type": "inference_error", "code": code, "message": code}})

    def reply(self, status, value):
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self, token):
        import hmac
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

    def body(self):
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= 16 * 1024 * 1024:
            raise AdmissionError(413, "invalid_request_size")
        self.connection.settimeout(10)
        body = self.rfile.read(int(length))
        if len(body) != int(length):
            raise AdmissionError(400, "incomplete_request")
        return body

    def do_GET(self):
        if self.path == "/flush_cache":
            if not self.authorized(self.server.access["adminToken"]):
                self.error(401, "authentication_required")
                return
            try:
                self.flush()
            except AdmissionError as error:
                self.error(error.status, error.code)
            except (OSError, http.client.HTTPException):
                self.error(502, "replica_flush_failed")
        elif self.path == "/health":
            self.reply(503 if self.server.router.draining else 200, {"status": "draining" if self.server.router.draining else "ready"})
        elif not self.authorized(self.server.access["engineToken"]):
            self.error(401, "authentication_required")
        elif self.path == "/v1/models":
            self.reply(200, {"object": "list", "data": [{"id": self.server.access["wireModel"], "object": "model", "owned_by": "hitch-inference"}]})
        elif self.path == "/server_info":
            self.server.router.relay(self, self.path)
        else:
            self.error(404, "route_not_found")

    def do_POST(self):
        admin = self.path == "/flush_cache"
        token = self.server.access["adminToken" if admin else "engineToken"]
        if not self.authorized(token):
            self.error(401, "authentication_required")
            return
        try:
            if admin:
                self.flush()
            elif self.path in ("/v1/chat/completions", "/v1/responses"):
                body = self.body()
                value = json.loads(body)
                if not isinstance(value, dict) or value.get("model") != self.server.access["wireModel"]:
                    raise AdmissionError(404, "model_not_found")
                self.server.router.relay(self, self.path, body)
            else:
                self.error(404, "route_not_found")
        except AdmissionError as error:
            if error.status != 499:
                self.error(error.status, error.code)
        except (ValueError, TypeError):
            self.error(400, "invalid_json_request")

    def flush(self):
        self.close_connection = True
        # The pinned SGLang endpoint supports both GET and POST. Every replica
        # must acknowledge the same method before warmup evidence is admitted.
        for target in self.server.router.targets:
            url = urlsplit(target["baseUrl"])
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
            try:
                connection.request(self.command, "/flush_cache", headers={"Authorization": "Bearer " + target["adminToken"]})
                response = connection.getresponse()
                if response.status != 200:
                    raise AdmissionError(502, "replica_flush_failed")
                response.read()
            finally:
                connection.close()
        self.reply(200, {"flushed": True})


class GatewayServer(InferenceHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address, router, access, handler=Handler):
        self.router, self.access = router, access
        super().__init__(address, handler)
