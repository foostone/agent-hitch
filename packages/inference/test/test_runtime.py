"""Real detached fake engines and HTTP sockets. These tests do not certify GPU hardware."""
import concurrent.futures
import copy
import http.client
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostics import startup_diagnostics

from hitch_inference.content import ContractError, atomic_json, digest_json
from hitch_inference.gateway import GatewayServer, Router
from hitch_inference.node import NodeService
from hitch_inference.plan import plan
from hitch_inference.pool import InferenceService, pool_worker
from hitch_inference.process_service import ProcessService
from hitch_inference.state import load, lock
from hitch_inference.topology import service_port
from hitch_inference.device_lease import NodeDeviceLedger
from hitch_inference.node_generation import record_generation


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition did not become true before deadline")


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inference-runtime-")
        self.root = Path(self.temp.name)
        self.gpu = self.root / "gpu"
        self.gpu.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        nvidia = self.bin / "nvidia-smi"
        nvidia.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fixtures/nvidia-smi.py").read_text())
        nvidia.chmod(0o755)
        self.environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HITCH_INFERENCE_FAKE_GPU_STATE": str(self.gpu), "HITCH_INFERENCE_API_TOKEN": "inference-token", "HITCH_INFERENCE_MANAGEMENT_TOKEN": "management-token", "HITCH_INFERENCE_TEST_TRACEBACK": "1"})
        self.environment.start()
        self.config = {"schemaVersion": 2, "nodeId": "fixture-node", "nodeRoot": str(self.root / "node"), "storeRoot": str(self.root / "cas"), "port": 0}
        self.config_file = self.root / "config.json"
        atomic_json(self.config_file, self.config)
        self.node = NodeService(self.config)
        self.service = InferenceService(self.config, self.node.identity)
        model = self.root / "model"
        model.mkdir()
        for name, value in {"config.json": {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "torch_dtype": "float32", "max_position_embeddings": 128},
                            "tokenizer_config.json": {"chat_template": "{{ messages }}"}, "tokenizer.json": {"version": "fixture"}}.items():
            (model / name).write_text(json.dumps(value))
        header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
        (model / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + struct.pack("<f", 1))
        self.model_directory = model
        self.payloads = []
        self.processes = []

    def tearDown(self):
        for payload in self.payloads:
            try:
                self.service.stop(self.owner(payload))
            except Exception:
                pass
        for process in self.processes:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        self.environment.stop()
        self.temp.cleanup()

    def payload(self, *, tp=2, replicas=2, queued=1, queue_timeout=500, request_timeout=5000):
        groups = [["GPU-fixture-" + str(i * tp + j) for j in range(tp)] for i in range(replicas)]
        payload = plan(self.config, self.model_directory, groups, running=replicas, queued=queued)
        payload["ownerId"] = "fixture-owner"
        payload["lock"]["execution"].update(startup_timeout_ms=15000, queue_timeout_ms=queue_timeout, request_timeout_ms=request_timeout)
        payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
        self.payloads.append(payload)
        return payload

    @staticmethod
    def owner(payload):
        return {"serviceId": payload["serviceId"], "ownerId": payload["ownerId"], "inferenceId": payload["lock"]["inference_id"]}

    def ready(self, payload):
        self.service.start(payload)
        def observed():
            value = self.service.inspect(self.owner(payload))
            if value["state"] == "failed":
                self.fail("inference service failed during startup")
            return value if value["state"] == "ready" else None
        try:
            return wait_until(observed)
        except AssertionError as error:
            self.fail(str(error) + "\n" + startup_diagnostics(self.root))

    def request(self, access, text="hello", *, stream=False, tools=False):
        connection = http.client.HTTPConnection("127.0.0.1", access["port"], timeout=10)
        body = {"model": access["wireModel"], "messages": [{"role": "user", "content": text}], "stream": stream}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object"}}}]
        connection.request("POST", "/v1/chat/completions", json.dumps(body), {"Authorization": "Bearer " + access["engineToken"], "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw if stream else json.loads(raw)

    def count_requests(self):
        file = self.gpu / "requests.jsonl"
        return len(file.read_text().splitlines()) if file.exists() else 0

    def test_real_tp_and_disjoint_replicas_concurrent_http_tools_and_usage(self):
        payload = self.payload()
        status = self.ready(payload)
        self.assertEqual(status["gpuUuids"], ["GPU-fixture-" + str(i) for i in range(4)])
        self.assertEqual(len(status["replicaObservations"]), 2)
        for child in self.service.children(payload):
            self.assertEqual(child["lock"]["resources"]["gpu_count"], child["lock"]["execution"]["tensor_parallel_size"])
        with concurrent.futures.ThreadPoolExecutor() as executor:
            replies = list(executor.map(lambda _: self.request(status["access"], "delay:0.4", tools=True), range(2)))
        self.assertEqual({tuple(body["replica"]) for code, body in replies}, {("GPU-fixture-0", "GPU-fixture-1"), ("GPU-fixture-2", "GPU-fixture-3")})
        for code, body in replies:
            self.assertEqual(code, 200)
            self.assertEqual(body["usage"]["total_tokens"], 5)
            self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        files = list(self.gpu.glob("*.json"))
        self.assertEqual(len(files), 2)
        for file in files:
            argv = load(file)["arguments"]
            self.assertEqual((argv["tp_size"], argv["dp_size"], argv["pp_size"]), ("2", "1", "1"))
        connection = http.client.HTTPConnection("127.0.0.1", status["access"]["port"])
        connection.request("POST", "/flush_cache", headers={"Authorization": "Bearer " + status["access"]["adminToken"]})
        self.assertEqual(connection.getresponse().status, 200); connection.close()
        self.assertEqual(len((self.gpu / "flush.jsonl").read_text().splitlines()), 2)

    def test_bounded_queue_timeout_abort_and_shared_service_lifetime(self):
        payload = self.payload(tp=1, replicas=1, queued=1, queue_timeout=150)
        access = self.ready(payload)["access"]
        with concurrent.futures.ThreadPoolExecutor() as executor:
            first = executor.submit(self.request, access, "delay:0.8")
            wait_until(lambda: self.count_requests() == 1)
            second = executor.submit(self.request, access, "queued")
            time.sleep(0.05)
            third = self.request(access, "overflow")
            self.assertEqual(third[0], 429)
            self.assertEqual(second.result()[1]["error"]["code"], "inference_queue_timeout")
            self.assertEqual(first.result()[0], 200)
        sock = socket.create_connection(("127.0.0.1", access["port"]))
        body = json.dumps({"model": access["wireModel"], "messages": [{"role": "user", "content": "delay:3"}], "stream": True}).encode()
        sock.sendall(("POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + access["engineToken"] + "\r\nContent-Length: " + str(len(body)) + "\r\nContent-Type: application/json\r\n\r\n").encode() + body)
        wait_until(lambda: self.count_requests() == 2)
        sock.recv(4096); sock.close()
        # Abort closes one upstream request; another caller can use the same pool.
        wait_until(lambda: self.request(access, "still alive")[0] == 200, timeout=5)
        self.assertEqual(self.service.inspect(self.owner(payload))["state"], "ready")

    def test_streaming_auth_crash_and_confirmed_all_replica_release(self):
        payload = self.payload()
        status = self.ready(payload)
        access = status["access"]
        code, raw = self.request(access, stream=True)
        self.assertEqual(code, 200); self.assertIn(b"data: [DONE]", raw); self.assertIn(b'"total_tokens": 5', raw)
        connection = http.client.HTTPConnection("127.0.0.1", access["port"])
        connection.request("GET", "/v1/models")
        self.assertEqual(connection.getresponse().status, 401); connection.close()
        os.kill(status["handle"]["process"]["pid"], signal.SIGKILL)
        wait_until(lambda: self.service.inspect(self.owner(payload))["state"] == "failed")
        self.assertFalse(self.service.inspect(self.owner(payload))["resourcesReleased"])
        stopped = self.service.stop(self.owner(payload))
        self.assertTrue(stopped["resourcesReleased"])
        self.assertGreater(stopped["gpuSeconds"], 0)
        self.assertTrue(all(replica["resourcesReleased"] for replica in stopped["replicas"]))

    def test_delayed_start_duplicate_worker_parent_digest_and_ports(self):
        payload = self.payload(tp=1, replicas=1)
        stopped = self.service.stop(self.owner(payload))
        self.assertTrue(stopped["resourcesReleased"])
        with patch.object(self.service, "prepare", side_effect=AssertionError("fenced start cannot prepare")):
            self.assertEqual(self.service.start(payload)["state"], "stopped")
        changed = copy.deepcopy(payload); changed["lock"]["topology"]["max_queued_requests"] += 1
        with self.assertRaises(ContractError): self.service.prepare(changed)
        active = self.payload(tp=1, replicas=1)
        current = self.ready(active)
        directory = self.service.directory(active)
        with patch.object(ProcessService, "stop") as stopper:
            pool_worker(directory)  # existing elected worker owns worker.lock
            self.assertFalse(stopper.called)
        self.assertEqual(self.service.inspect(self.owner(active))["handle"], current["handle"])
        other = directory.parent.parent / "inference" / "inference_port_fixture"
        other.mkdir(parents=True)
        with self.assertRaises(ContractError): service_port({"inferencePort": current["access"]["port"]}, other)

    def test_legacy_lease_schema_and_release_fence(self):
        ledger = NodeDeviceLedger(self.config["nodeRoot"], self.node.identity)
        with patch("hitch_inference.device_lease.gpu_processes", return_value=[]):
            legacy = ledger.acquire("training/legacy/incarnation", ["GPU-fixture-0"])
            with self.assertRaises(ContractError): ledger.acquire("inference/other", ["GPU-fixture-0"])
            ledger.acquire("inference/disjoint", ["GPU-fixture-1", "GPU-fixture-2"])
            self.assertEqual(legacy["node"], self.node.identity)
            with self.assertRaises(ContractError): ledger.release("training/legacy/incarnation")
            ledger.fence("training/legacy/incarnation")
            self.assertTrue(ledger.release("training/legacy/incarnation"))
            ledger.acquire("training/new", ["GPU-fixture-0"])
        self.assertEqual(load(Path(self.config["nodeRoot"]) / "device-leases.json")["schemaVersion"], 2)

    def test_each_replica_observation_and_all_tp_devices_are_required(self):
        payload = self.payload()
        payload["lock"]["execution"]["startup_timeout_ms"] = 1500
        payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
        with patch.dict(os.environ, {"HITCH_INFERENCE_FAKE_BAD_REPLICA": "GPU-fixture-2"}):
            self.service.start(payload)
            status = wait_until(lambda: (value if value["state"] == "failed" else None)
                if (value := self.service.inspect(self.owner(payload))) else None, timeout=10)
        self.assertNotEqual(status["state"], "ready")
        self.assertTrue(self.service.stop(self.owner(payload))["resourcesReleased"])
        missing = self.payload(tp=2, replicas=1)
        missing["lock"]["execution"]["startup_timeout_ms"] = 1000
        missing["lock"]["inference_id"] = digest_json({k: v for k, v in missing["lock"].items() if k != "inference_id"})
        with patch.dict(os.environ, {"HITCH_INFERENCE_FAKE_MISSING_GPU": "GPU-fixture-1"}):
            self.service.start(missing)
            wait_until(lambda: self.service.inspect(self.owner(missing))["state"] == "failed", timeout=10)
        self.assertTrue(self.service.stop(self.owner(missing))["resourcesReleased"])

    def test_archived_pool_recovery_requires_boot_proof_and_every_gpu_empty(self):
        payload = self.payload()
        previous = {"nodeId": self.node.identity["nodeId"], "generation": "archived-generation"}
        root = Path(self.config["nodeRoot"])
        current_identity = load(root / "identity.json")
        archived = {**previous, "bootIdentity": "prior-os-boot"}
        record_generation(root, archived)
        atomic_json(root / "identity.json", archived)
        old_ledger = NodeDeviceLedger(root, previous)
        directory = self.service.directory(payload)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "identity.json", {"node": previous, "ownerId": payload["ownerId"], "inferenceId": payload["lock"]["inference_id"], "inputDigest": digest_json(payload)})
        atomic_json(directory / "request.json", payload)
        atomic_json(directory / "status.json", {"schemaVersion": 2, "state": "starting", "resourcesReleased": False})
        with patch("hitch_inference.device_lease.gpu_processes", return_value=[]):
            for child in self.service.children(payload):
                target = self.service.legacy.directory(child)
                target.mkdir(parents=True, exist_ok=True)
                atomic_json(target / "identity.json", {"node": previous, "ownerId": child["ownerId"], "inferenceId": child["lock"]["inference_id"], "inputDigest": digest_json(child)})
                atomic_json(target / "status.json", {"schemaVersion": 2, "state": "starting", "resourcesReleased": False})
                old_ledger.acquire(self.service.legacy.owner(child["serviceId"]), child["lock"]["execution"]["platform"]["device_constraints"])
        atomic_json(root / "identity.json", current_identity)
        intent = {**self.owner(payload), "previousNode": previous, "expectedHandle": None}
        with patch("hitch_inference.device_lease.gpu_processes", return_value=[{"device": "GPU-fixture-0", "pid": 999999}]):
            with self.assertRaises(ContractError): self.service.recover(intent)
        with patch("hitch_inference.device_lease.gpu_processes", return_value=[]), patch("hitch_inference.pool.stop_owned") as stop:
            recovered = self.service.recover(intent)
            self.assertTrue(recovered["resourcesReleased"])
            self.assertEqual(set(recovered["devices"]), {"GPU-fixture-" + str(i) for i in range(4)})
            self.assertFalse(stop.called)
        self.assertEqual(recovered["sourceIdentityDigest"], digest_json(load(directory / "identity.json")))
        self.assertEqual(len(load(directory / "replica-generation-releases.json")), 2)
        # No boot change is insufficient proof even if numeric PIDs are absent.
        wrong = {**previous, "generation": "same-boot-generation"}
        record_generation(root, {**wrong, "bootIdentity": current_identity["bootIdentity"]})
        with self.assertRaises(ContractError):
            from hitch_inference.node_generation import previous_boot_proof
            previous_boot_proof(root, self.node.identity, wrong)

    def test_startup_drain_fences_delayed_workers_and_converges_release(self):
        payload = self.payload(tp=1, replicas=1)
        with patch.object(self.service, "prepare", return_value={}), patch("hitch_inference.pool.subprocess.Popen") as launch:
            self.assertEqual(self.service.start(payload)["state"], "starting")
            drained = self.service.drain({**self.owner(payload), "drainTimeoutMs": 100})
            self.assertEqual(drained["state"], "stopped")
            self.assertTrue(drained["resourcesReleased"])
            self.assertTrue(all(replica["resourcesReleased"] for replica in drained["replicas"]))
            self.assertEqual(self.service.start(payload)["state"], "stopped")
            self.assertEqual(launch.call_count, 1)

    def test_graceful_drain_finishes_admitted_work_and_rejects_new_requests(self):
        payload = self.payload(tp=1, replicas=1)
        access = self.ready(payload)["access"]
        with concurrent.futures.ThreadPoolExecutor() as executor:
            active = executor.submit(self.request, access, "delay:0.6")
            wait_until(lambda: self.count_requests() == 1)
            result = self.service.drain({**self.owner(payload), "drainTimeoutMs": 2000})
            self.assertEqual(result["state"], "stopping")
            def health_drained():
                connection = http.client.HTTPConnection("127.0.0.1", access["port"])
                connection.request("GET", "/health")
                response = connection.getresponse(); response.read(); connection.close()
                return response.status == 503
            wait_until(health_drained)
            self.assertEqual(self.request(access, "late")[0], 503)
            self.assertEqual(active.result()[0], 200)
        wait_until(lambda: self.service.inspect(self.owner(payload))["resourcesReleased"])

    def test_ordinary_api_lifecycle_auth_and_keepalive_stop(self):
        process = subprocess.Popen([sys.executable, "-m", "hitch_inference.http_server", "--config", str(self.config_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        announcement = json.loads(process.stdout.readline())
        port = announcement["port"]
        payload = self.payload(tp=1, replicas=2)
        payload["ownerId"] = "inference-http/" + digest_json(str(Path(self.config["nodeRoot"]).resolve()))
        public = {key: value for key, value in payload.items() if key != "ownerId"}
        def call(method, route, value=None, token="management-token", connection=None):
            client = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            client.request(method, route, None if value is None else json.dumps(value), {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            response = client.getresponse(); data = json.loads(response.read())
            if connection is None: client.close()
            return response.status, data
        self.assertEqual(call("POST", "/deployments", public, token="inference-token")[0], 401)
        self.assertEqual(call("POST", "/deployments", public)[0], 202)
        wait_until(lambda: call("GET", "/deployments/" + payload["serviceId"])[1]["state"] == "ready")
        self.assertEqual(call("GET", "/v1/models", token="inference-token")[1]["data"][0]["id"], payload["lock"]["inference_id"])
        self.assertEqual(call("GET", "/v1/models", token="management-token")[0], 401)
        code, answer = call("POST", "/v1/chat/completions", {"model": payload["lock"]["inference_id"], "messages": [{"role": "user", "content": "ordinary"}]}, token="inference-token")
        self.assertEqual((code, answer["usage"]["total_tokens"]), (200, 5))
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.assertTrue(call("POST", "/deployments/" + payload["serviceId"] + "/stop", {}, connection=connection)[1]["resourcesReleased"])
        self.assertEqual(call("GET", "/v1/models", token="inference-token", connection=connection), (200, {"object": "list", "data": []}))
        connection.close()


if __name__ == "__main__":
    unittest.main()
