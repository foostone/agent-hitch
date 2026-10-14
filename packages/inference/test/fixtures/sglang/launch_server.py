"""CPU test engine fixture; no hardware or SGLang certification claim."""
import argparse
import json
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p = argparse.ArgumentParser()
for name in ("model-path", "served-model-name", "host", "port", "api-key", "admin-api-key", "random-seed", "tp-size", "dp-size", "pp-size", "load-format", "dtype", "context-length", "max-running-requests", "max-total-tokens", "chunked-prefill-size", "max-prefill-tokens", "kv-cache-dtype", "attention-backend", "sampling-backend", "mem-fraction-static", "device", "tool-call-parser", "reasoning-parser"):
    p.add_argument("--" + name)
for name in ("disable-radix-cache", "disable-overlap-schedule", "disable-cuda-graph", "enable-deterministic-inference"):
    p.add_argument("--" + name, action="store_true")
a = p.parse_args()
root = Path(os.environ["HITCH_INFERENCE_FAKE_GPU_STATE"])
root.mkdir(parents=True, exist_ok=True)
devices = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
(root / (str(os.getpid()) + ".json")).write_text(json.dumps({"pid": os.getpid(), "devices": devices, "arguments": vars(a)}))
info = {"version": "0.5.15.post1", "device": a.device or "cuda", "dtype": a.dtype, "kv_cache_dtype": a.kv_cache_dtype,
    "attention_backend": a.attention_backend, "sampling_backend": a.sampling_backend, "context_length": int(a.context_length),
    "max_total_num_tokens": int(a.max_total_tokens), "max_running_requests": int(a.max_running_requests),
    "tp_size": int(a.tp_size), "dp_size": int(a.dp_size), "pp_size": int(a.pp_size),
    "disable_radix_cache": a.disable_radix_cache, "disable_overlap_schedule": a.disable_overlap_schedule,
    "mem_fraction_static": float(a.mem_fraction_static or 0)}

if os.environ.get("HITCH_INFERENCE_FAKE_BAD_REPLICA") in devices:
    info["tp_size"] = 1

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args): pass
    def reply(self, value):
        data = json.dumps(value).encode(); self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        print(json.dumps({"fixture": "get", "path": self.path}), flush=True)
        if self.path == "/flush_cache":
            if self.headers.get("Authorization") != "Bearer " + a.admin_api_key: self.send_error(401); return
            with (root / "flush.jsonl").open("a") as out: out.write(json.dumps({"devices": devices}) + "\n")
            self.reply({"flushed": True})
        elif self.path == "/health": self.reply({"ready": True})
        elif self.headers.get("Authorization") != "Bearer " + a.api_key: self.send_error(401)
        elif self.path == "/server_info": self.reply(info)
        elif self.path == "/v1/models": self.reply({"data": [{"id": a.served_model_name}]})
        else: self.send_error(404)
    def do_POST(self):
        if self.path == "/flush_cache":
            if self.headers.get("Authorization") != "Bearer " + a.admin_api_key: self.send_error(401); return
            with (root / "flush.jsonl").open("a") as out: out.write(json.dumps({"devices": devices}) + "\n")
            self.reply({"flushed": True}); return
        if self.headers.get("Authorization") != "Bearer " + a.api_key: self.send_error(401); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with (root / "requests.jsonl").open("a") as out: out.write(json.dumps({"devices": devices, "body": body}) + "\n")
        text = str(body.get("messages", [{}])[-1].get("content", ""))
        delay = float(text.removeprefix("delay:")) if text.startswith("delay:") else 0
        value = {"id": "chatcmpl-fixture", "object": "chat.completion", "model": body["model"], "replica": devices,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        if body.get("tools"):
            value["choices"][0].update(message={"role": "assistant", "content": None, "tool_calls": [{"id": "call_fixture", "type": "function", "function": {"name": "fixture_tool", "arguments": "{}"}}]}, finish_reason="tool_calls")
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers(); self.close_connection = True
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'); self.wfile.flush()
                time.sleep(delay)
                self.wfile.write(("data: " + json.dumps(value) + "\n\ndata: [DONE]\n\n").encode()); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
        else:
            time.sleep(delay)
            try: self.reply(value)
            except (BrokenPipeError, ConnectionResetError): pass

server = ThreadingHTTPServer((a.host, int(a.port)), Handler)
print(json.dumps({"fixture": "listening", "pid": os.getpid(), "devices": devices,
                  "configuration": info}), flush=True)
server.serve_forever()
