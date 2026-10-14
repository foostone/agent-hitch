"""Actual ordinary API -> pool -> fake SGLang token-unit contract."""
import copy
import http.client
import json
import threading
import unittest
import test_runtime as runtime
from hitch_inference.content import digest_json
from hitch_inference.gateway import GatewayServer
from hitch_inference.http_server import DeploymentManager, ServiceHandler


class ResponsesHTTPTest(unittest.TestCase):
    setUp = runtime.RuntimeTest.setUp
    tearDown = runtime.RuntimeTest.tearDown
    payload = runtime.RuntimeTest.payload
    owner = staticmethod(runtime.RuntimeTest.owner)

    def test_responses_budget_has_one_terminal_conversion_and_invalid_deploy_does_not_pollute_registry(self):
        manager = DeploymentManager(self.config)
        server = GatewayServer(("127.0.0.1", 0), None, {}, ServiceHandler)
        server.manager, server.api_token, server.management_token = manager, "inference-token", "management-token"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = self.payload(tp=1, replicas=2)
            payload["ownerId"] = manager.owner
            payload["lock"]["protocol"]["api"] = "responses"
            payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
            public = {k: v for k, v in payload.items() if k != "ownerId"}
            def request(route, body, token="inference-token", port=None):
                connection = http.client.HTTPConnection("127.0.0.1", port or server.server_address[1], timeout=10)
                connection.request("POST", route, body if isinstance(body, str) else json.dumps(body), {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse(); value = response.read(); connection.close()
                return response.status, value
            self.assertEqual(request("/deployments", public, "management-token")[0], 202)
            runtime.wait_until(lambda: manager.inspect(payload["serviceId"])["state"] == "ready")
            for budget in (32, 1):
                self.assertEqual(request("/v1/responses", {"model": payload["lock"]["inference_id"], "input": "hello", "max_output_tokens": budget})[0], 200)
                engine = json.loads((self.gpu / "requests.jsonl").read_text().splitlines()[-1])["body"]
                self.assertEqual(engine["max_output_tokens"], budget + 2)
                self.assertEqual(engine["top_k"], -1)
            count = len((self.gpu / "requests.jsonl").read_text().splitlines())
            for invalid in ({"max_output_tokens": 33}, {"background": True}, {"previous_response_id": "old-response"}, {"truncation": "auto"}, {"top_k": -1}):
                self.assertEqual(request("/v1/responses", {"model": payload["lock"]["inference_id"], "input": "hello", **invalid})[0], 400)
            self.assertEqual(len((self.gpu / "requests.jsonl").read_text().splitlines()), count)
            self.assertEqual(request("/v1/responses", {"model": "unbound-model", "input": "hello"})[0], 404)
            access = manager.inspect(payload["serviceId"])["access"]
            self.assertEqual(request("/v1/responses", {"model": "unbound-model", "input": "hello"}, access["engineToken"], access["port"])[0], 404)
            for number in ("1e309", "-1e309"):
                raw = '{"model":' + json.dumps(access["wireModel"]) + ',"input":"hello","max_output_tokens":' + number + '}'
                self.assertEqual(request("/v1/responses", raw, access["engineToken"], access["port"])[0], 400)
            self.assertEqual(len((self.gpu / "requests.jsonl").read_text().splitlines()), count)
            broken = copy.deepcopy(public); broken["serviceId"] = "inference_" + "f" * 32
            broken["lock"]["model_id"] = "sha256:" + "1" * 64
            self.assertEqual(request("/deployments", broken, "management-token")[0], 400)
            self.assertEqual(list(manager.deployments()), [payload["serviceId"]])
            self.assertIsNotNone(manager.route(payload["lock"]["inference_id"]))
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", "/v1/models", headers={"Authorization": "Bearer inference-token"})
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read())["data"][0]["id"], payload["lock"]["inference_id"])
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
