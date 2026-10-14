import json
import sys
from hitch_inference.plan import build_lock
fixture = json.load(open(sys.argv[1]))
result = []
for row in fixture["cases"]:
    model = {**fixture["model"], "dtype": row["dtype"], "context_tokens": row["context"], "model_type": row["model_type"],
             "files": [{"path": "model.safetensors", "size": row["weights"], "sha256": "sha256:" + "a" * 64}]}
    result.append(build_lock(model, fixture["runtime"], fixture["binding"], row["replicas"], row["profile"], api=row["api"]))
print(json.dumps(result))
