import json, os, sys
from pathlib import Path
import psutil
if any("query-gpu" in a for a in sys.argv):
    print("\n".join("GPU-fixture-" + str(i) for i in range(4)))
else:
    root = Path(os.environ["HITCH_INFERENCE_FAKE_GPU_STATE"])
    rows = []
    for path in root.glob("*.json"):
        value = json.loads(path.read_text())
        try:
            process = psutil.Process(value["pid"])
            if process.status() == psutil.STATUS_ZOMBIE: continue
        except psutil.NoSuchProcess: continue
        for device in value["devices"]:
            if device and device != os.environ.get("HITCH_INFERENCE_FAKE_MISSING_GPU"):
                rows.append({"device": device, "pid": value["pid"]})
    with (root / "gpu-observations.jsonl").open("a") as output:
        output.write(json.dumps(rows) + "\n")
    for row in rows: print(row["device"] + ", " + str(row["pid"]))
