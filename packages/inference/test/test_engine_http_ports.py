"""Real detached startup at high HTTP ports with the pinned gRPC validation boundary."""
import os
import socket
import unittest
from unittest.mock import patch
import test_runtime as runtime
from hitch_inference.content import digest_json
from hitch_inference.process_service import ProcessService
from hitch_inference.state import load


class HTTPPortTest(unittest.TestCase):
    setUp = runtime.RuntimeTest.setUp
    tearDown = runtime.RuntimeTest.tearDown
    payload = runtime.RuntimeTest.payload
    owner = staticmethod(runtime.RuntimeTest.owner)
    request = runtime.RuntimeTest.request

    def high_port(self, preferred):
        for port in (preferred, *range(65000, 60000, -1)):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        self.fail("no free high HTTP port for startup regression")

    def test_dynamic_high_http_port_does_not_use_default_grpc_offset(self):
        self.start_at_high_port(dynamic=True, inherit_grpc=False)

    def test_fixed_high_http_port_disables_inherited_grpc_configuration(self):
        self.start_at_high_port(dynamic=False, inherit_grpc=True)

    def start_at_high_port(self, *, dynamic, inherit_grpc):
        port = self.high_port(60643 if dynamic else 65535)
        payload = self.payload(tp=1, replicas=1)
        if not dynamic:
            # Historical fixed-port v2 endpoints still connect directly to SGLang.
            payload["lock"]["schema_version"] = "2"
            payload["lock"].pop("topology")
            payload["lock"]["execution"]["platform"].pop("device_constraints")
            payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
        config = {**self.config, "dynamicInferencePorts": dynamic, "inferencePort": port}
        service = ProcessService(config, self.node.identity)
        environment = {key: value for key, value in os.environ.items() if key not in ("SGLANG_ENABLE_GRPC", "SGLANG_GRPC_PORT")}
        if inherit_grpc:
            environment.update(SGLANG_ENABLE_GRPC="true", SGLANG_GRPC_PORT="70000")
        try:
            with patch.dict(os.environ, environment, clear=True), patch("hitch_inference.topology.available_port", return_value=port):
                service.start(payload)
                def ready():
                    status = service.inspect(self.owner(payload))
                    self.assertNotIn(status["state"], ("failed", "stopped"), runtime.startup_diagnostics(self.root))
                    return status if status["state"] == "ready" else None
                status = runtime.wait_until(ready)
            self.assertGreater(status["access"]["port"], 55535)
            self.assertEqual(status["access"]["port"], port)
            self.assertEqual(self.request(status["access"])[0], 200)
            engines = [load(path) for path in self.gpu.glob("*.json")]
            self.assertEqual(len(engines), 1)
            self.assertEqual(engines[0]["grpc"], {"enabled": False, "port": port})
        finally:
            self.assertTrue(service.stop(self.owner(payload))["resourcesReleased"])
