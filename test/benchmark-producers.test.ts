import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rename, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { buildHarborJobConfig } from "../src/backends/harbor/backend.js";
import { loadBenchmark, mapBenchmarkMetrics } from "../src/benchmarks/index.js";
import { exportStandardBenchmarkDataset, loadBenchmarkAdapterManifest, validateEvalRequest } from "../src/evals/index.js";

function python(args: string[], env: NodeJS.ProcessEnv = process.env, expected = 0): string {
  const result = spawnSync("python3", args, { encoding: "utf8", env: { ...env, PYTHONDONTWRITEBYTECODE: "1" } });
  assert.equal(result.status, expected, result.stderr || result.stdout);
  return result.stdout;
}

async function fixture(t: test.TestContext) {
  const root = await mkdtemp(path.join(tmpdir(), "hitch-producer-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = JSON.parse(python(["test-support/benchmark_producers_fixture.py", path.join(root, "source")])) as Record<string, string>;
  return { root, source };
}

test("JobBench official renderer imports, validates and compiles a fractional total-only score", async t => {
  const { root, source } = await fixture(t), output = path.join(root, "package");
  const args = ["benchmark-packages/jobbench/import.py", "--source", source.jobbench!, "--source-revision", source.jobbench_revision!,
    "--snapshot", source.snapshot!, "--dataset-revision", "a".repeat(40), "--seed", "23", "--count", "2", "--out", output];
  python(args);
  const loaded = await loadBenchmark(output);
  assert.equal(loaded.manifest.metrics.reward!.type, "scalar");
  assert.equal(loaded.manifest.task_ids.length, 2);
  assert.ok(loaded.tasks.every(task => task.harbor.task === undefined), "Harbor must derive its flat lock identity from the task directory");
  assert.equal(mapBenchmarkMetrics({ reward: 0.3333 }, loaded.manifest, loaded.tasks[0]!.config).primary, 0.3333);
  const task = path.join(output, loaded.tasks[0]!.path);
  const publicFiles = await readdir(path.join(task, "environment"));
  assert.equal(publicFiles.includes("RUBRICS.json"), false);
  assert.equal(publicFiles.includes("judge.py"), false);
  const config = await readFile(path.join(task, "task.toml"), "utf8");
  assert.match(config, /HITCH_JOBBENCH_JUDGE_API_KEY/);
  assert.doesNotMatch(config, /\$\{OPENAI_API_KEY/);
  assert.equal((await readFile(path.join(task, "tests/RUBRICS.json"), "utf8")).includes("criterion a"), true);
  await exportStandardBenchmarkDataset(output, path.join(root, "compiled"));
  const compiled = await loadBenchmarkAdapterManifest(path.join(root, "compiled"));
  assert.deepEqual(compiled!.scoring, { total_score: { source_metric: "reward", direction: "maximize", range: [0, 1], reducer: "task-macro-mean" } });
  const second = path.join(root, "second"); python([...args.slice(0, -1), second]);
  assert.deepEqual((await loadBenchmark(second)).lock, loaded.lock, "same input, selection and modes must lock identically");
  python(args, process.env, 1); // Never overwrite an existing package.
});

test("LAB keeps nested source IDs, shared documents and fallback instructions through standard compilation", async t => {
  const { root, source } = await fixture(t), output = path.join(root, "package");
  await writeFile(path.join(source.lab!, "lab_core/.env"), "IGNORED_KEY=private-source-secret");
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!, "--count", "2", "--out", output]);
  const loaded = await loadBenchmark(output);
  assert.equal(loaded.manifest.primary_metric, "lab_all_pass");
  assert.equal(loaded.manifest.metrics.lab_all_pass!.type, "scalar");
  assert.ok(loaded.tasks.every(task => task.id.length < 100 && !task.id.includes("/") && task.config.source_task_id.startsWith("area/review/")));
  for (const task of loaded.tasks) await assert.rejects(readFile(path.join(output, task.path, "tests/vendor/lab_core/.env")));
  const fallback = loaded.tasks.find(task => task.config.source_task_id.endsWith("02"))!;
  assert.match(await readFile(path.join(output, fallback.path, "instruction.md"), "utf8"), /Fallback instructions/);
  assert.match(await readFile(path.join(output, fallback.path, "environment/documents/Tax Sections — Source/shared.txt"), "utf8"), /shared corpus/);
  assert.equal((await readdir(path.join(output, fallback.path, "environment"))).includes("task.json"), false);
  await exportStandardBenchmarkDataset(output, path.join(root, "compiled"));
  const manifest = (await loadBenchmarkAdapterManifest(path.join(root, "compiled")))!;
  assert.equal(manifest.scoring.total_score.source_metric, "lab_all_pass");
  assert.equal(manifest.scoring.process_score, undefined);
  const identity = `sha256:${"a".repeat(64)}`;
  const request = await validateEvalRequest({ dataset: path.join(root, "compiled"), harness_ref: "codex@version:0.145.0", model: "candidate-model", pass_env: ["OPENAI_API_KEY"] });
  const job = await buildHarborJobConfig({ request, backendDirectory: root, runtimeDirectory: root,
    resolvedRevision: { harness_id: "codex", revision: { type: "version", version: "0.145.0" }, identity } as never,
    preparedArtifact: { directory: root, artifact_id: identity, artifact_integrity: identity, entrypoint_integrity: identity, harness_id: "codex", revision_identity: identity,
      adapter_version: "1", recipe_version: "1", platform: "linux/amd64", node_version: process.version, source_type: "version" },
    env: { OPENAI_API_KEY: "candidate-only-sentinel", HITCH_LAB_JUDGE_OPENAI_API_KEY: "private-openai-judge-sentinel",
      HITCH_LAB_JUDGE_ANTHROPIC_API_KEY: "private-anthropic-judge-sentinel", HITCH_JOBBENCH_JUDGE_API_KEY: "private-jobbench-judge-sentinel" },
  });
  const agent = (job.agents as Array<{ env: Record<string, string>; kwargs: { credential_names: string[] } }>)[0]!;
  assert.deepEqual(agent.env, { OPENAI_API_KEY: "${OPENAI_API_KEY}" });
  assert.deepEqual(agent.kwargs.credential_names, ["OPENAI_API_KEY"]);
  assert.doesNotMatch(JSON.stringify(job), /private-.*-judge-sentinel|candidate-only-sentinel/);
  const single = path.join(root, "single");
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!, "--judges", "gpt-5.5", "--out", single]);
  assert.equal((await loadBenchmark(single)).manifest.metrics.lab_all_pass!.type, "binary");
});

test("official verifiers preserve empty/missing output, all-pass semantics and judge failure without leaking keys", async t => {
  const { root, source } = await fixture(t);
  for (const kind of ["jobbench", "lab"]) {
    const output = path.join(root, kind);
    python([`benchmark-packages/${kind}/import.py`, "--source", source[kind]!, ...(kind === "jobbench"
      ? ["--source-revision", source.jobbench_revision!, "--snapshot", source.snapshot!, "--dataset-revision", "b".repeat(40)]
      : ["--revision", source.lab_revision!, "--task", "area/review/scenario-01"]), "--out", output]);
    const task = (await loadBenchmark(output)).tasks[0]!, tests = path.join(output, task.path, "tests");
    const evidence = path.join(root, `${kind}-output`), logs = path.join(root, `${kind}-logs`);
    const env = { ...process.env, PYTHONPATH: source.stubs, JUDGE_API_KEY: "synthetic-private-jobbench-secret", ANTHROPIC_API_KEY: "synthetic-private-lab-secret" };
    const run = (expected = 0, extra: NodeJS.ProcessEnv = {}) => python([path.join(tests, "hitch_grade.py"), "--tests", tests, "--output", evidence, "--logs", logs], { ...env, ...extra }, expected);
    run();
    let rewards = JSON.parse(await readFile(path.join(logs, "reward.json"), "utf8"));
    assert.equal(rewards[kind === "jobbench" ? "reward" : "lab_all_pass"], 0, "empty evidence is judged by official behavior");
    await mkdir(evidence);
    await writeFile(path.join(evidence, "answer.txt"), "submitted work\n");
    run();
    rewards = JSON.parse(await readFile(path.join(logs, "reward.json"), "utf8"));
    assert.equal(rewards[kind === "jobbench" ? "reward" : "lab_all_pass"], kind === "jobbench" ? 0.3333 : 0.5);
    if (kind === "lab") assert.equal(rewards.lab_criterion_pass, 0.75, "criterion fraction differs from canonical all-pass");
    for (const failure of kind === "jobbench" ? ["JOB_TEST_ERROR", "JOB_TEST_BAD_REWARD", "JOB_TEST_BAD_PARSE"] : ["LAB_TEST_ERROR", "LAB_TEST_BAD_VERDICT"]) {
      run(1, { [failure]: "1" });
      await assert.rejects(readFile(path.join(logs, "reward.json")));
    }
    if (kind === "lab") {
      await rename(path.join(evidence, "answer.txt"), path.join(evidence, "unrelated.csv"));
      run(1); // The official matcher swallows the fake Anthropic exception.
      assert.match(await readFile(path.join(logs, "grading-error.json"), "utf8"), /matcher failed/);
      await rename(path.join(evidence, "unrelated.csv"), path.join(evidence, "answer.txt"));
    }
    for (const file of await readdir(logs)) if (file.endsWith(".json") || file.endsWith(".log")) {
      const text = await readFile(path.join(logs, file), "utf8");
      assert.doesNotMatch(text, /synthetic-private-(jobbench|lab)-secret/);
    }
    // Unsafe output must invalidate verification and clear any old valid reward.
    await symlink(path.join(evidence, "answer.txt"), path.join(evidence, "unsafe"));
    run(1); await assert.rejects(readFile(path.join(logs, "reward.json")));
  }
});


test("single-judge LAB grading errors and malformed verdicts cannot become a valid zero", async t => {
  const { root, source } = await fixture(t), output = path.join(root, "single");
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!, "--task", "area/review/scenario-01", "--judges", "gpt-5.5", "--out", output]);
  const task = (await loadBenchmark(output)).tasks[0]!, tests = path.join(output, task.path, "tests");
  const evidence = path.join(root, "output"), logs = path.join(root, "logs");
  await mkdir(evidence); await writeFile(path.join(evidence, "answer.txt"), "work");
  for (const failure of ["LAB_TEST_ERROR", "LAB_TEST_BAD_VERDICT"]) {
    python([path.join(tests, "hitch_grade.py"), "--tests", tests, "--output", evidence, "--logs", logs], {
      ...process.env, PYTHONPATH: source.stubs, ANTHROPIC_API_KEY: "private-single-judge-secret", [failure]: "1",
    }, 1);
    await assert.rejects(readFile(path.join(logs, "reward.json")));
    assert.match(await readFile(path.join(logs, "grading-error.json"), "utf8"), /could not grade/);
  }
});

test("producers reject missing/unsafe source inputs and a dirty or mismatched source revision", async t => {
  const { root, source } = await fixture(t);
  const job = ["benchmark-packages/jobbench/import.py", "--source", source.jobbench!, "--source-revision", source.jobbench_revision!, "--snapshot", source.snapshot!, "--dataset-revision", "c".repeat(40), "--task", "main--fixture-profession--task1"];
  const raw = path.join(source.snapshot!, "dataset/fixture-profession/task1");
  await rm(path.join(raw, "RUBRICS.json"));
  python([...job, "--out", path.join(root, "missing-rubric")], process.env, 1);
  await symlink(path.join(raw, "task_folder/public.txt"), path.join(raw, "RUBRICS.json"));
  python([...job, "--out", path.join(root, "symlink-rubric")], process.env, 1);
  const lab = ["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!, "--task", "area/review/scenario-01"];
  await writeFile(path.join(source.lab!, "README.md"), "dirty source");
  python([...lab, "--out", path.join(root, "dirty")], process.env, 1);
  // Commit a task with an unavailable document corpus. The importer must fail after clean-revision validation.
  await rm(path.join(source.lab!, "tasks/area/review/scenario-01/documents"), { recursive: true });
  const commit = () => {
    for (const args of [["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "invalid source fixture"]]) {
      const result = spawnSync("git", ["-C", source.lab!, ...args], { encoding: "utf8" }); assert.equal(result.status, 0, result.stderr);
    }
    return spawnSync("git", ["-C", source.lab!, "rev-parse", "HEAD"], { encoding: "utf8" }).stdout.trim();
  };
  let revision = commit();
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", revision, "--task", "area/review/scenario-01", "--out", path.join(root, "missing-docs")], process.env, 1);
  await symlink(path.join(source.lab!, "tasks/shared-documents"), path.join(source.lab!, "tasks/area/review/scenario-01/documents"));
  revision = commit();
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", revision, "--task", "area/review/scenario-01", "--out", path.join(root, "symlink-docs")], process.env, 1);
  python([...lab, "--out", path.join(root, "wrong-revision")], process.env, 1);
});

test("LAB explicit single Chat Completions judge preserves official prompts and rejects invalid responses", async t => {
  const { root, source } = await fixture(t), output = path.join(root, "chat-single");
  const args = ["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!,
    "--task", "area/review/scenario-01", "--judges", "deepseek-flash", "--out", output];
  python(args, process.env, 1); // Unknown models require an explicit transport; no guessed provider.
  python([...args, "--judge-api", "openai-chat-completions", "--judge-reasoning-effort", "high"]);
  const benchmark = await loadBenchmark(output), task = benchmark.tasks[0]!, tests = path.join(output, task.path, "tests");
  assert.equal(benchmark.manifest.metrics.lab_all_pass!.type, "binary");
  await exportStandardBenchmarkDataset(output, path.join(root, "compiled-chat"));
  assert.equal((await loadBenchmarkAdapterManifest(path.join(root, "compiled-chat")))!.scoring.total_score.source_metric, "lab_all_pass");
  const evidence = path.join(root, "output"), logs = path.join(root, "logs"), requests = path.join(root, "chat-requests.jsonl");
  await mkdir(evidence); await writeFile(path.join(evidence, "answer.txt"), "submitted legal work");
  const env = { ...process.env, PYTHONPATH: source.stubs, OPENAI_API_KEY: "synthetic-private-chat-judge-key",
    OPENAI_BASE_URL: "https://api.example.invalid/v1", LAB_CHAT_REQUESTS: requests };
  const run = (expected = 0, extra: NodeJS.ProcessEnv = {}) => python([path.join(tests, "hitch_grade.py"), "--tests", tests, "--output", evidence, "--logs", logs], { ...env, ...extra }, expected);
  run();
  assert.deepEqual(JSON.parse(await readFile(path.join(logs, "reward.json"), "utf8")), { lab_all_pass: 1, lab_criterion_pass: 1 });
  const request = JSON.parse((await readFile(requests, "utf8")).trim().split("\n")[0]!);
  assert.deepEqual(Object.keys(request).sort(), ["max_tokens", "messages", "model", "reasoning_effort", "response_format", "temperature"]);
  assert.equal(request.model, "deepseek-flash"); assert.equal(request.max_tokens, 64000);
  assert.equal(request.reasoning_effort, "high"); assert.deepEqual(request.response_format, { type: "json_object" });
  const prompt = await readFile(path.join(tests, "vendor/lab_core/evaluation/prompts/rubric_criterion.txt"), "utf8");
  assert.ok(prompt.includes("{task_description}"));
  assert.ok(request.messages[0].content.includes("fixture legal review"));
  const prompts = (await readFile(requests, "utf8")).trim().split("\n").map(line => JSON.parse(line).messages[0].content as string);
  assert.ok(prompts.some(prompt => prompt.includes("first passes")));
  assert.ok(prompts.some(prompt => prompt.includes("second passes")));
  assert.ok(request.messages[0].content.includes("submitted legal work"));
  const provenance = JSON.parse(await readFile(path.join(logs, "judge-provenance.json"), "utf8"));
  assert.equal(provenance.transport.api, "openai-chat-completions");
  assert.equal(provenance.transport.base_url, env.OPENAI_BASE_URL);
  run(0, { LAB_CHAT_FAILURE: "fail" });
  assert.equal(JSON.parse(await readFile(path.join(logs, "reward.json"), "utf8")).lab_all_pass, 0, "a valid fail remains a scored zero");
  for (const mode of ["api", "parse", "verdict", "length", "empty", "no-choice", "multiple-choices"]) {
    run(1, { LAB_CHAT_FAILURE: mode });
    await assert.rejects(readFile(path.join(logs, "reward.json")));
    assert.match(await readFile(path.join(logs, "grading-error.json"), "utf8"), /could not grade/);
    for (const name of await readdir(logs)) assert.doesNotMatch(await readFile(path.join(logs, name), "utf8"), /synthetic-private-chat-judge-key/);
  }
  python(["benchmark-packages/lab/import.py", "--source", source.lab!, "--revision", source.lab_revision!,
    "--judges", "deepseek-flash", "deepseek-v4-pro", "--judge-api", "openai-chat-completions", "--out", path.join(root, "invalid-dual")], process.env, 1);
});
