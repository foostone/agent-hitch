"""Create offline pinned Git fixtures using official renderer/evaluation functions.

Provider SDKs and model calls are deterministic fakes. No network, model spend,
Docker daemon, or globally installed benchmark dependencies are required.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent / "fixtures/benchmark-producers"

LAB_JUDGE = '''
class Judge:
    def __init__(self, model="claude-sonnet-4-6"):
        self.model = model
    def evaluate_from_file(self, prompt_name, variables):
        import os
        if os.environ.get("LAB_TEST_ERROR"):
            raise RuntimeError(os.environ["ANTHROPIC_API_KEY"])
        if os.environ.get("LAB_TEST_BAD_VERDICT"): return {"verdict": "unknown", "reasoning": "malformed"}
        evidence = variables["agent_output"]
        passed = "File not found:" not in evidence and "No agent output found" not in evidence
        if self.model.startswith("gpt") and variables["criterion_title"] == "second": passed = False
        return {"verdict": "pass" if passed else "fail", "reasoning": evidence}
'''

JOB_JUDGE = '''import argparse,json,os
from jobbench_eval.scorecard_fixture import build_scorecard,build_reward
from pathlib import Path
p=argparse.ArgumentParser()
for n in ["output-dir","rubrics-file","result-file","details-file","lock-file","detail-log-dir","detail-log-prefix","max-workers","max-retries","timeout-per-rubric","evaluated-model"]: p.add_argument("--"+n)
a=p.parse_args()
if os.environ.get("JOB_TEST_ERROR"): raise RuntimeError(os.environ["JUDGE_API_KEY"])
output=Path(a.output_dir)
has_files=output.exists() and any(f.is_file() for f in output.rglob("*"))
rubrics=json.loads(Path(a.rubrics_file).read_text())["rubrics"]
rubric_results=[{"index":i,"weight":r["weight"],"result":{"score":r["weight"] if has_files and i==0 else 0,"passed":has_files and i==0}} for i,r in enumerate(rubrics)]
reward=build_reward(build_scorecard(rubric_results))
if os.environ.get("JOB_TEST_BAD_REWARD"): reward={"reward":float("nan")}
Path(a.result_file).write_text(json.dumps(reward))
Path(a.details_file).write_text(json.dumps({"rubrics":[{} for r in rubrics]}))
for i,r in enumerate(rubrics):
    response={"criteria_results":[{"passed":True} for c in r["criterion"]],"rubric_passed":True,"overall_reasoning":"fixture"}
    Path(a.detail_log_dir,f"jobbench_rubric_{i}.log").write_text(("Parse Status: invalid" if os.environ.get("JOB_TEST_BAD_PARSE") else "Parse Status: direct_json")+"\\nAPI Exit Code: 0\\nCriteria Count: "+str(len(r["criterion"]))+"\\nRAW API RESPONSE\\n================\\n"+json.dumps(response)+"\\n\\n================\\nFINAL RESULT\\n")
'''


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def git_fixture(source):
    for args in [["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic offline source"]]:
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


def create(destination):
    job = destination / "jobbench"; lab = destination / "lab"
    shutil.copytree(ROOT / "jobbench", job); shutil.copytree(ROOT / "lab", lab)
    write(job / "harbor/jobbench_harbor/__init__.py", "")
    write(job / "eval/jobbench_eval/__init__.py", '"""Renderer fixture needs only a harmless helper tree."""\n')
    shutil.copy2(job / "eval/scorecard_fixture.py", job / "eval/jobbench_eval/scorecard_fixture.py")
    write(job / "eval/judge.py", JOB_JUDGE)
    snapshot = destination / "snapshot"
    rubric = {"rubrics": [{"rubric": "first block", "criterion": ["criterion a", "criterion b"], "weight": 1}, {"rubric": "second block", "criterion": ["criterion c"], "weight": 2}]}
    for name in ["task1", "task2"]:
        raw = snapshot / "dataset/fixture-profession" / name
        write(raw / "RUBRICS.json", json.dumps(rubric)); write(raw / "task_card.md", "private task card\n")
        write(raw / "task_folder/TASK_INSTRUCTIONS.txt", "Read public.txt and create answer.txt in the output directory.\n")
        write(raw / "task_folder/public.txt", "public input\n")
    for name in ["lab_core/__init__.py", "lab_core/evaluation/__init__.py"]: write(lab / name, "")
    # Keep the official function bodies; defer annotations on Python 3.9 test hosts.
    write(lab / "lab_core/paths.py", "from __future__ import annotations\n" + (lab / "lab_core/paths.py").read_text())
    write(lab / "lab_core/evaluation/judge.py", (lab / "lab_core/evaluation/judge.py").read_text() + LAB_JUDGE)
    write(lab / "pyproject.toml", '[project]\nname="lab-core"\nversion="0.0.0"\n')
    write(lab / "uv.lock", "# synthetic fixture; never used to build a container\n")
    write(lab / ".gitignore", ".env\n")
    write(lab / "README.md", "Synthetic fixture using pinned official evaluation functions.\n")
    for name in ["area/review/scenario-01", "area/review/scenario-02"]:
        config = {"title": "fixture legal review", "instructions": "Write answer.txt.", "criteria": [
            {"id": "C1", "title": "first", "match_criteria": "first passes", "deliverables": ["answer.txt"]},
            {"id": "C2", "title": "second", "match_criteria": "second passes", "deliverables": ["answer.txt"]}]}
        if name.endswith("02"):
            config.update({"instructions": "", "docs_dir": "../../../shared-documents"})
            write(lab / "tasks" / name / "instructions.md", "Fallback instructions: write answer.txt.\n")
        else: write(lab / "tasks" / name / "documents/input.txt", "local source document\n")
        write(lab / "tasks" / name / "task.json", json.dumps(config))
    write(lab / "tasks/shared-documents/Tax Sections — Source/shared.txt", "shared corpus document\n")
    # Stand-ins for optional extraction/provider dependencies. Text-file logic is official.
    stubs = destination / "stubs"
    for name in ["pandas.py", "pdfplumber.py"]: write(stubs / name, "")
    write(stubs / "markitdown.py", "class MarkItDown: pass\n")
    write(stubs / "anthropic.py", "import os\nclass Anthropic:\n    def __init__(self): raise RuntimeError(os.environ.get('ANTHROPIC_API_KEY','synthetic matcher failure'))\n")
    write(stubs / "sitecustomize.py", "import enum\nif not hasattr(enum, 'StrEnum'):\n    class CompatStrEnum(str, enum.Enum): pass\n    enum.StrEnum = CompatStrEnum\n")
    return {"jobbench": str(job), "jobbench_revision": git_fixture(job), "lab": str(lab), "lab_revision": git_fixture(lab), "snapshot": str(snapshot), "stubs": str(stubs)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("destination", type=Path)
    print(json.dumps(create(p.parse_args().destination)))
