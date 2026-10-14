"""Shared projection for the TypeScript/Python serial-boundary regression."""
import json
import sys
from hitch_inference.request_validation import validate_request, engine_request

fixture = json.load(open(sys.argv[1]))
rows = json.load(sys.stdin)
result = []
for row in rows:
    frozen = {**fixture["lock"], "protocol": {**fixture["lock"]["protocol"], "api": row["api"]}}
    try:
        canonical = validate_request(row["body"], frozen)
        repeated = validate_request(canonical, frozen)
        result.append({"valid": True, "canonical": repeated, "engine": engine_request(repeated, frozen)})
    except ValueError:
        result.append({"valid": False})
print(json.dumps(result))
