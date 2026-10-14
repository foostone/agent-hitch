"""Strict physical GPU observations shared by all process owners."""
import re
import subprocess
from .content import require
def parse_gpu_process_rows(output):
    require(
        isinstance(output, str),
        "gpu-process-observation-invalid",
        "GPU process observation is incomplete",
    )
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split(",")]
        require(
            len(fields) == 2
            and re.fullmatch(r"GPU-[A-Za-z0-9-]+", fields[0])
            and fields[1].isdigit()
            and int(fields[1]) > 0,
            "gpu-process-observation-invalid",
            "GPU process observation is incomplete",
        )
        rows.append({"device": fields[0], "pid": int(fields[1])})
    return rows

def gpu_processes(devices):
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    # Validate the entire observation before selecting devices. Unknown output
    # is not proof of an empty GPU and must not authorize training/eval handoff.
    return [row for row in parse_gpu_process_rows(result.stdout) if row["device"] in devices]
