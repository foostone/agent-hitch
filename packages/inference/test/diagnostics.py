"""Bounded startup diagnostics from private fixture state, excluding credentials."""
import json
import re
import sys
from pathlib import Path


def startup_diagnostics(root):
    records = []
    for path in sorted(Path(root).rglob("status.json")):
        try:
            status = json.loads(path.read_text())
            records.append({"service": path.parent.name, **{
                key: status[key] for key in ("state", "error", "resourcesReleased") if key in status
            }})
        except (OSError, ValueError) as error:
            records.append({"file": path.name, "error": type(error).__name__})
    for path in sorted(Path(root).rglob("*.log")):
        try:
            with path.open("rb") as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 8192))
                lines = source.read().decode(errors="replace").splitlines()[-20:]
        except OSError as error:
            records.append({"file": path.name, "error": type(error).__name__})
            continue
        lines = [line for line in lines if not re.search(r"api.?key|admin.?token|engine.?token|authorization", line, re.I)]
        records.append({"service": path.parent.name, "log": path.name,
                        "tail": re.sub(r"[a-f0-9]{64}", "[digest/token]", "\n".join(lines))[-3000:]})
    for path in sorted(Path(root).rglob("device-leases.json")):
        try:
            ledger = json.loads(path.read_text())
            records.append({"owners": [{key: owner[key] for key in ("owner", "devices", "processes", "closing") if key in owner}
                                       for owner in ledger.get("owners", {}).values()]})
        except (OSError, ValueError):
            pass
    for path in sorted(Path(root).rglob("gpu-observations.jsonl")):
        records.append({"gpu_observations": path.read_text().splitlines()[-4:]})
    return json.dumps(records, indent=2)[:20000]


if __name__ == "__main__":
    print(startup_diagnostics(sys.argv[1]))
