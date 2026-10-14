"""Deployment prepare replay, tamper detection and nonblocking stop regressions."""
import concurrent.futures
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
import test_runtime as runtime
from hitch_inference.content import ContractError, atomic_json, digest_json
from hitch_inference.http_server import DeploymentManager
from hitch_inference.preparation import ModelPreparation
from hitch_inference.process_service import ProcessService
from hitch_inference.snapshots import materialize, seal_directory
from hitch_inference.state import load, lock


class PreparationTest(unittest.TestCase):
    setUp = runtime.RuntimeTest.setUp
    tearDown = runtime.RuntimeTest.tearDown
    payload = runtime.RuntimeTest.payload
    owner = staticmethod(runtime.RuntimeTest.owner)
    ready = runtime.RuntimeTest.ready
    request = runtime.RuntimeTest.request

    def test_concurrent_rpc_prepare_once_and_replica_replay(self):
        payload = self.payload()
        file = self.root / "prepare-payload.json"
        atomic_json(file, payload)
        counter = self.root / "calls.jsonl"
        code = r"""
import json, sys
from pathlib import Path
from unittest.mock import patch
import hitch_inference.preparation as preparation
from hitch_inference.node import main
counter = Path(sys.argv[1])
original_materialize, original_seal = preparation.materialize, preparation.seal_directory
def counted(label, original):
    def run(*args, **kwargs):
        with counter.open('a') as output: output.write(label + '\n')
        return original(*args, **kwargs)
    return run
sys.argv = ['node', 'rpc', '--config', sys.argv[2]]
with patch.object(preparation, 'materialize', counted('materialize', original_materialize)), patch.object(preparation, 'seal_directory', counted('seal', original_seal)):
    main()
"""
        envelope = {"schemaVersion": 2, "requestId": "prepare-test", "node": self.node.identity,
                    "operation": "inference.prepare", "inputDigest": digest_json(payload), "payload": payload}
        processes = [subprocess.Popen([sys.executable, "-c", code, str(counter), str(self.config_file)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        with concurrent.futures.ThreadPoolExecutor() as executor:
            outputs = list(executor.map(lambda p: p.communicate(json.dumps(envelope), timeout=20), processes))
        for process, (output, errors) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, errors + output)
            self.assertEqual(json.loads(output)["result"]["replicas"], 2)
        self.assertEqual(counter.read_text().splitlines(), ["materialize", "seal"])
        status = self.ready(payload)
        self.assertEqual(status["state"], "ready")
        receipts = list((self.service.legacy.root / "preparations").glob("*.json"))
        self.assertEqual(len(receipts), 1, "replicas reuse their parent deployment receipt")
        # A new deployment must fully verify even if it serves the same model bytes.
        second = copy.deepcopy(payload)
        second["serviceId"] = "inference_" + "a" * 32
        with patch("hitch_inference.preparation.materialize", wraps=materialize) as copied, patch("hitch_inference.preparation.seal_directory", wraps=seal_directory) as sealed:
            self.service.prepare(second)
            self.assertEqual((copied.call_count, sealed.call_count), (1, 1))
        old_boot = ModelPreparation(self.config, {**self.node.identity, "generation": "different-boot"})
        with self.assertRaises(ContractError): old_boot.verify(payload)

    def test_witness_rejects_tampering_and_keeps_sibling_models_independent(self):
        (self.model_directory / "说明.txt").write_text("model metadata")
        first = self.payload(tp=1, replicas=1)
        self.service.prepare(first)
        destination = self.service.legacy.preparation.destination(first)
        self.service.legacy.preparation.verify(first)
        # Adding another model changes models/ metadata, not the first model's identity.
        tokenizer = self.model_directory / "tokenizer.json"
        tokenizer.write_text('{"version":"other-model"}')
        second = self.payload(tp=1, replicas=1)
        self.service.prepare(second)
        self.service.legacy.preparation.verify(first)
        weight = destination / "model.safetensors"
        before, original = weight.stat(), weight.read_bytes()
        weight.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        os.utime(weight, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ContractError): self.service.legacy.preparation.verify(first)
        weight.write_bytes(original)
        third = copy.deepcopy(first); third["serviceId"] = "inference_" + "b" * 32
        self.service.prepare(third)
        (destination / "unexpected.txt").write_text("unexpected")
        with self.assertRaises(ContractError): self.service.legacy.preparation.verify(third)
        (destination / "unexpected.txt").unlink()
        fourth = copy.deepcopy(first); fourth["serviceId"] = "inference_" + "c" * 32
        self.service.prepare(fourth)
        weight.unlink(); weight.symlink_to(self.node.store.path(first["model"]["files"][-1]["sha256"]))
        with self.assertRaises(ContractError): self.service.legacy.preparation.verify(fourth)
        weight.unlink(); weight.write_bytes(original)
        fifth = copy.deepcopy(first); fifth["serviceId"] = "inference_" + "d" * 32
        self.service.prepare(fifth)
        backup = destination.with_name(destination.name + ".backup")
        destination.rename(backup); shutil.copytree(backup, destination)
        with self.assertRaises(ContractError): self.service.legacy.preparation.verify(fifth)

    def test_slow_prepare_does_not_block_other_stop_and_stop_fences_its_launch(self):
        manager = DeploymentManager(self.config)
        first = self.payload(tp=1, replicas=1); first["ownerId"] = manager.owner
        value = lambda p: {k: v for k, v in p.items() if k != "ownerId"}
        manager.deploy(value(first))
        runtime.wait_until(lambda: manager.inspect(first["serviceId"])["state"] == "ready")
        second = self.payload(tp=1, replicas=1); second["ownerId"] = manager.owner
        entered, release = threading.Event(), threading.Event()
        original = materialize
        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args, **kwargs)
        with patch("hitch_inference.preparation.materialize", side_effect=slow), concurrent.futures.ThreadPoolExecutor() as executor:
            deployment = executor.submit(manager.deploy, value(second))
            self.assertTrue(entered.wait(5))
            stopped = executor.submit(manager.stop, first["serviceId"], {"force": True}).result(timeout=5)
            self.assertTrue(stopped["resourcesReleased"])
            self.assertTrue(manager.service.stop(self.owner(second))["resourcesReleased"])
            release.set()
            with self.assertRaises(ContractError): deployment.result(timeout=10)
        self.assertFalse((manager.service.directory(second) / "access.json").exists())
        self.assertNotIn(second["serviceId"], manager.deployments())
        broken = copy.deepcopy(value(first)); broken["serviceId"] = "inference_" + "e" * 32
        broken["lock"]["generation"]["max_output_tokens"] += 1
        with self.assertRaises(ContractError): manager.deploy(broken)
        self.assertNotIn(broken["serviceId"], manager.deployments())

    def test_partial_identity_admission_replays_in_one_start(self):
        for pooled, partial in ((True, False), (True, True), (False, False), (False, True)):
            with self.subTest(pooled=pooled, partial=partial):
                payload = self.payload(tp=1, replicas=1)
                if not pooled:
                    payload["lock"]["schema_version"] = "2"
                    payload["lock"].pop("topology")
                    payload["lock"]["execution"]["platform"].pop("device_constraints")
                    payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
                service = self.service if pooled else ProcessService({**self.config, "dynamicInferencePorts": True}, self.node.identity)
                directory = service.directory(payload)
                atomic_json(directory / "identity.json", {"node": self.node.identity, "ownerId": payload["ownerId"],
                            "inferenceId": payload["lock"]["inference_id"], "inputDigest": digest_json(payload)})
                if partial:
                    atomic_json(directory / "request.json", payload)
                    atomic_json(directory / "config.json", self.config)
                self.assertIn(service.start(payload)["state"], ("starting", "ready"))
                runtime.wait_until(lambda: service.inspect(self.owner(payload))["state"] == "ready")
                self.assertTrue(service.stop({**self.owner(payload), "force": True})["resourcesReleased"])

    def test_process_start_uses_the_same_launch_lock_as_inspect(self):
        payload = self.payload(tp=1, replicas=1)
        payload["lock"]["schema_version"] = "2"
        payload["lock"].pop("topology")
        payload["lock"]["execution"]["platform"].pop("device_constraints")
        payload["lock"]["inference_id"] = digest_json({k: v for k, v in payload["lock"].items() if k != "inference_id"})
        service = ProcessService({**self.config, "dynamicInferencePorts": True}, self.node.identity)
        directory = service.directory(payload)
        atomic_json(directory / "identity.json", {"node": self.node.identity, "ownerId": payload["ownerId"],
                    "inferenceId": payload["lock"]["inference_id"], "inputDigest": digest_json(payload)})
        entered, release = threading.Event(), threading.Event()
        def observe_with_launch_lock():
            with lock(directory / "launch.lock"):
                entered.set()
                self.assertTrue(release.wait(5))
                atomic_json(directory / "status.json", {"schemaVersion": 2, "state": "admitting", "resourcesReleased": False})
        with concurrent.futures.ThreadPoolExecutor() as executor:
            observer = executor.submit(observe_with_launch_lock)
            self.assertTrue(entered.wait(5))
            with patch.object(service, "prepare", wraps=service.prepare) as prepare:
                starting = executor.submit(service.start, payload)
                with self.assertRaises(concurrent.futures.TimeoutError): starting.result(timeout=0.1)
                self.assertFalse(prepare.called, "start must wait for the in-progress status observation")
                release.set(); observer.result(timeout=5)
                self.assertIn(starting.result(timeout=10)["state"], ("starting", "ready"))
        runtime.wait_until(lambda: service.inspect(self.owner(payload))["state"] == "ready")

    def test_snapshots_are_hf_only_and_keep_original_hf_identity(self):
        expected = seal_directory(self.node.store, self.model_directory, serving=True)
        self.assertEqual(seal_directory(self.node.store, self.model_directory), expected)
        with self.assertRaises(ContractError): seal_directory(self.node.store, self.model_directory, serving=False)
        for format in ("trainer-files", "harbor-dataset"):
            ref = self.node.store.put_json({"schemaVersion": 1, "format": format, "files": []})
            with self.assertRaises(ContractError): materialize(self.node.store, ref, self.root / "not-a-model")
