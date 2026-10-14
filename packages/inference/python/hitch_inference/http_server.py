"""Ordinary inference HTTP API. No evaluation, training, Harbor or Docker state."""
import argparse
import json
import threading
from pathlib import Path
from .content import atomic_json, digest_json, require
from .gateway import AdmissionError, GatewayServer, Handler, Router
from .node import NodeService
from .pool import InferenceService
from .state import load, lock


class DeploymentManager:
    def __init__(self, config):
        self.node = NodeService(config)
        self.config = config
        self.service = InferenceService({**config, "inferencePort": None, "dynamicInferencePorts": True}, self.node.identity)
        self.path = Path(config["nodeRoot"]) / "deployments.json"
        self.owner = "inference-http/" + digest_json(str(Path(config["nodeRoot"]).resolve()))
        self.mutex = threading.RLock()
        self.routes = {}

    def deployments(self):
        return load(self.path, {"schemaVersion": 1, "deployments": {}})["deployments"]

    def deploy(self, value):
        require(isinstance(value, dict) and set(value) == {"serviceId", "model", "runtime", "lock"},
                "invalid-deployment", "deployment requires exactly serviceId, model, runtime and lock")
        payload = {**value, "ownerId": self.owner}
        with self.mutex, lock(self.path.with_suffix(".lock")):
            deployments = self.deployments()
            previous = deployments.get(value["serviceId"])
            require(not previous or previous == payload, "deployment-identity-conflict", "a deployment ID cannot change model, runtime or topology")
            self.service.prepare(payload)
            deployments[value["serviceId"]] = payload
            atomic_json(self.path, {"schemaVersion": 1, "deployments": deployments})
        return self.service.start(payload)

    def inspect(self, service_id):
        payload = self.deployments().get(service_id)
        require(payload, "deployment-not-found", "deployment does not exist")
        return self.service.inspect({"serviceId": service_id, "ownerId": self.owner, "inferenceId": payload["lock"]["inference_id"]})

    def stop(self, service_id, options=None):
        payload = self.deployments().get(service_id)
        require(payload, "deployment-not-found", "deployment does not exist")
        with self.mutex:
            route = self.routes.pop(service_id, None)
            if route:
                route.drain()
        return self.service.stop({"serviceId": service_id, "ownerId": self.owner, "inferenceId": payload["lock"]["inference_id"], **(options or {})})

    def drain(self, service_id, timeout=30000):
        payload = self.deployments().get(service_id)
        require(payload, "deployment-not-found", "deployment does not exist")
        with self.mutex:
            route = self.routes.get(service_id)
            if route:
                route.drain()
        return self.service.drain({"serviceId": service_id, "ownerId": self.owner,
            "inferenceId": payload["lock"]["inference_id"], "drainTimeoutMs": timeout})

    def recover(self, service_id):
        payload = self.deployments().get(service_id)
        require(payload, "deployment-not-found", "deployment does not exist")
        directory = self.service.directory({"serviceId": service_id, "ownerId": self.owner})
        identity = load(directory / "identity.json")
        status = load(directory / "status.json", {})
        require(identity, "deployment-not-found", "deployment admission identity is missing")
        return self.service.recover({"serviceId": service_id, "ownerId": self.owner,
            "inferenceId": payload["lock"]["inference_id"], "previousNode": identity["node"], "expectedHandle": status.get("handle")})

    def route(self, model):
        matches = [(key, payload, self.inspect(key)) for key, payload in self.deployments().items()
                   if model in (payload["lock"]["inference_id"], payload["model"]["model_id"])]
        ready = [(key, payload, status) for key, payload, status in matches if status["state"] == "ready"]
        if len(ready) > 1:
            raise AdmissionError(409, "ambiguous_model_alias")
        if not ready:
            raise AdmissionError(503 if matches else 404, "deployment_unavailable" if matches else "model_not_found")
        service_id, payload, status = ready[0]
        with self.mutex:
            if service_id not in self.routes:
                execution = payload["lock"]["execution"]
                topology = payload["lock"].get("topology", {})
                running = topology.get("max_running_requests", execution["max_running_requests"])
                self.routes[service_id] = Router([{**status["access"], "baseUrl": "http://127.0.0.1:" + str(status["access"]["port"])}],
                    running=running, queued=topology.get("max_queued_requests", execution["max_queued_requests"]),
                    queue_timeout=execution["queue_timeout_ms"] / 1000,
                    request_timeout=execution["request_timeout_ms"] / 1000, engine_capacity=running, frozen=payload["lock"])
            return self.routes[service_id]

    @staticmethod
    def public(status):
        return {key: value for key, value in status.items() if key not in ("access", "runtime", "serverInfo")}


class ServiceHandler(Handler):
    def do_GET(self):
        manager = self.server.manager
        if self.path == "/health":
            self.reply(200, {"status": "ready"})
            return
        management = self.path.startswith("/deployments")
        if not self.authorized(self.server.management_token if management else self.server.api_token):
            self.error(401, "authentication_required")
            return
        try:
            if self.path == "/v1/models":
                data = []
                for service_id, payload in manager.deployments().items():
                    status = manager.inspect(service_id)
                    if status["state"] == "ready":
                        data.append({"id": payload["lock"]["inference_id"], "object": "model", "owned_by": "hitch-inference"})
                self.reply(200, {"object": "list", "data": data})
            elif self.path == "/deployments":
                self.reply(200, {"deployments": [manager.public(manager.inspect(key)) for key in manager.deployments()]})
            elif self.path.startswith("/deployments/"):
                self.reply(200, manager.public(manager.inspect(self.path.removeprefix("/deployments/"))))
            else:
                self.error(404, "route_not_found")
        except Exception as error:
            self.error(400, getattr(error, "code", "invalid_request"))

    def do_POST(self):
        management = self.path.startswith("/deployments")
        if not self.authorized(self.server.management_token if management else self.server.api_token):
            self.error(401, "authentication_required")
            return
        try:
            if self.path == "/deployments":
                result = self.server.manager.deploy(json.loads(self.body()))
                self.reply(202, self.server.manager.public(result))
            elif self.path.startswith("/deployments/") and self.path.endswith("/recover"):
                require(json.loads(self.body()) == {}, "invalid-control-request", "recovery accepts an empty JSON object")
                key = self.path[len("/deployments/"):-len("/recover")]
                self.reply(200, self.server.manager.public(self.server.manager.recover(key)))
            elif self.path.startswith("/deployments/") and self.path.endswith("/drain"):
                body = json.loads(self.body())
                require(isinstance(body, dict) and set(body) <= {"timeout_ms"}, "invalid-control-request", "drain accepts timeout_ms")
                key = self.path[len("/deployments/"):-len("/drain")]
                self.reply(202, self.server.manager.public(self.server.manager.drain(key, body.get("timeout_ms", 30000))))
            elif self.path.startswith("/deployments/") and self.path.endswith("/stop"):
                body = json.loads(self.body())
                require(isinstance(body, dict) and set(body) <= {"force", "drainTimeoutMs"}
                    and ("force" not in body or type(body["force"]) is bool), "invalid-control-request", "stop accepts force and drainTimeoutMs")
                key = self.path[len("/deployments/"):-len("/stop")]
                self.reply(200, self.server.manager.public(self.server.manager.stop(key, body)))
            elif self.path in ("/v1/chat/completions", "/v1/responses"):
                body = self.body()
                value = json.loads(body)
                require(isinstance(value, dict) and isinstance(value.get("model"), str), "invalid-model", "model is required")
                self.server.manager.route(value["model"]).relay(self, self.path, body)
            else:
                self.error(404, "route_not_found")
        except AdmissionError as error:
            if error.status != 499:
                self.error(error.status, error.code)
        except Exception as error:
            self.error(400, getattr(error, "code", "invalid_request"))


def main():
    parser = argparse.ArgumentParser(description="Standalone immutable inference service")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load(args.config)
    import os
    api_token = os.environ.get(config.get("apiTokenEnv", "HITCH_INFERENCE_API_TOKEN"))
    management_token = os.environ.get(config.get("managementTokenEnv", "HITCH_INFERENCE_MANAGEMENT_TOKEN"))
    require(api_token and management_token and api_token != management_token,
            "missing-service-credentials", "distinct inference and management tokens are required in configured environment variables")
    manager = DeploymentManager(config)
    server = GatewayServer((config.get("host", "127.0.0.1"), config.get("port", 8080)), None, {}, ServiceHandler)
    server.manager, server.api_token, server.management_token = manager, api_token, management_token
    print(json.dumps({"ready": True, "port": server.server_address[1], "node": manager.node.identity}), flush=True)
    try:
        server.serve_forever()
    finally:
        # Server restarts do not stop durable deployments or release client-independent leases.
        server.server_close()


if __name__ == "__main__":
    main()
