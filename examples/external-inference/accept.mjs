#!/usr/bin/env node
// One-shot standard HTTP acceptance tool. No SDK, retry, engine management, or GPU accounting.
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const help = `Usage: node accept.mjs --endpoint endpoint.json --deployment deployment.json --out artifacts/acceptance.json
  --budget 1024             Output budget, passed unchanged to every generation
  --concurrent 4            Number of simultaneous independent requests (at least 2)
  --timeout-ms 180000        Per-request deadline, including stream consumption
  --worker-metrics URL       Repeat for each worker's /metrics endpoint
  --gateway-metrics URL      Optional official Gateway Prometheus endpoint
  --expect-workers 2         Require positive measured request-count deltas on this many workers

Requires Node.js 22+. Endpoint api is explicitly responses or chat-completions.
The report contains prompts, generated output, tool arguments/results, and usage.
An HTTP report does not attest GPU topology, loaded weight identity, or real Harness behavior.
`;
const args = { budget: 1024, concurrent: 4, timeoutMs: 180000, workerMetrics: [] };
const names = { '--endpoint': 'endpoint', '--deployment': 'deployment', '--out': 'out', '--budget': 'budget', '--concurrent': 'concurrent', '--timeout-ms': 'timeoutMs', '--expect-workers': 'expectWorkers', '--gateway-metrics': 'gatewayMetrics' };
for (let i = 2; i < process.argv.length; i++) {
  const flag = process.argv[i];
  if (flag === '--help') { console.log(help); process.exit(0); }
  const value = process.argv[++i];
  if (!value || value.startsWith('--')) throw new Error(`Missing value for ${flag}`);
  if (flag === '--worker-metrics') args.workerMetrics.push(value);
  else if (names[flag]) args[names[flag]] = value;
  else throw new Error(`Unknown option ${flag}`);
}
for (const field of ['endpoint', 'deployment', 'out']) if (!args[field]) throw new Error(`--${field} is required.\n${help}`);
for (const field of ['budget', 'concurrent', 'timeoutMs', 'expectWorkers']) {
  if (args[field] === undefined) continue;
  args[field] = Number(args[field]);
  if (!Number.isSafeInteger(args[field]) || args[field] < (field === 'concurrent' ? 2 : 1)) throw new Error(`Invalid ${field}`);
}
if (args.expectWorkers && args.workerMetrics.length !== args.expectWorkers) throw new Error('--expect-workers must equal the number of --worker-metrics URLs');

const endpoint = JSON.parse(await readFile(args.endpoint, 'utf8'));
const deployment = JSON.parse(await readFile(args.deployment, 'utf8'));
function check(condition, message) { if (!condition) throw new Error(message); }
check(['responses', 'chat-completions'].includes(endpoint.api), 'Endpoint must explicitly select responses or chat-completions');
check(typeof endpoint.wire_model === 'string' && endpoint.wire_model.length > 0, 'Missing wire_model');
const responses = endpoint.api === 'responses';
const base = endpoint.base_url.replace(/\/+$/, '');
for (const url of [base, ...args.workerMetrics, args.gatewayMetrics].filter(Boolean)) {
  const parsed = new URL(url);
  check(['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password, 'Use HTTP(S) URLs without embedded credentials');
}
let apiKey;
if (endpoint.auth?.kind === 'env') {
  apiKey = process.env[endpoint.auth.name];
  check(typeof apiKey === 'string' && apiKey.length > 0, `Missing credential environment variable ${endpoint.auth.name}`);
} else check(endpoint.auth?.kind === 'none', 'This probe supports auth kind env or none');
function redact(value, key = '') {
  if (/^(api_?key|authorization|password|secret|credential|access_token)$/i.test(key)) return '[REDACTED]';
  if (typeof value === 'string') return apiKey ? value.split(apiKey).join('[REDACTED]') : value;
  if (Array.isArray(value)) return value.map(item => redact(item));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redact(item, name)]));
  return value;
}
const report = {
  schema_version: '1', observation_kind: 'standard-http-endpoint-acceptance',
  started_at: new Date().toISOString(), passed: false,
  hardware_validation: 'NOT PERFORMED BY THIS HTTP PROBE',
  identity_validation: 'operator metadata only; not proof of loaded weights',
  harness_validation: 'NOT PERFORMED; run the selected real Harness separately',
  configuration: { endpoint, deployment, budget: args.budget, temperature: 0, concurrent_requests: args.concurrent, request_timeout_ms: args.timeoutMs, transparent_retries: 0 },
  probe: { node_version: process.version, sha256: createHash('sha256').update(await readFile(fileURLToPath(import.meta.url))).digest('hex') },
  checks: [], exchanges: [],
};
const generationUrl = `${base}/${responses ? 'responses' : 'chat/completions'}`;
const user = text => ({ role: 'user', content: text });
function body(history, { stream = false, tools, toolChoice } = {}) {
  return {
    model: endpoint.wire_model, temperature: 0,
    ...(responses ? { input: history, max_output_tokens: args.budget, store: false } : { messages: history, max_tokens: args.budget }),
    stream,
    ...(!responses && stream ? { stream_options: { include_usage: true } } : {}),
    ...(tools ? { tools, parallel_tool_calls: false, tool_choice: toolChoice ?? 'auto' } : {}),
  };
}
function view(json) {
  if (responses) {
    check(['completed', 'incomplete'].includes(json.status), `Unexpected Responses status ${json.status}`);
    if (json.status === 'incomplete') check(json.incomplete_details?.reason === 'max_output_tokens', 'Unexpected incomplete response reason');
    return { text: (json.output ?? []).flatMap(item => item.type === 'message' ? (item.content ?? []).filter(part => part.type === 'output_text').map(part => part.text) : []).join(''), tools: (json.output ?? []).filter(item => item.type === 'function_call').map(item => ({ id: item.call_id, name: item.name, arguments: item.arguments })), history: json.output ?? [], usage: json.usage, terminal: json.status };
  }
  const choice = json.choices?.[0];
  check(choice && choice.finish_reason !== null && choice.finish_reason !== undefined, 'Missing chat finish_reason');
  const message = choice.message;
  check(message?.role === 'assistant', 'Missing assistant chat message');
  return { text: message.content ?? '', tools: (message.tool_calls ?? []).map(item => ({ id: item.id, name: item.function?.name, arguments: item.function?.arguments })), history: [{ role: 'assistant', content: message.content ?? null, ...(message.tool_calls?.length ? { tool_calls: message.tool_calls } : {}) }], usage: json.usage, terminal: choice.finish_reason };
}
function verifyUsage(result) {
  const usage = result.usage;
  check(usage && typeof usage === 'object' && !Array.isArray(usage), 'Missing or invalid usage; missing values are not replaced with zero');
  function count(name, aliases) {
    const fields = aliases.filter(field => Object.hasOwn(usage, field));
    check(fields.length > 0 && fields.every(field => Number.isSafeInteger(usage[field]) && usage[field] >= 0), `Missing or invalid ${name} usage; missing values are not replaced with zero`);
    const value = usage[fields[0]];
    check(fields.every(field => usage[field] === value), `Inconsistent ${name} usage aliases: ${fields.join(', ')}`);
    return { value, fields };
  }
  // Verify recognized wire variants without rewriting the captured upstream usage.
  const input = count('input', ['input_tokens', 'prompt_tokens']);
  const output = count('output', ['output_tokens', 'completion_tokens']);
  check(output.value <= args.budget, `Reported output usage ${output.value} exceeds the unchanged budget ${args.budget}`);
  return { input_tokens: input.value, output_tokens: output.value, source_fields: { input_tokens: input.fields, output_tokens: output.fields } };
}
async function exchange(name, url, { data, mode = 'json', signal, onContent, expectedAbort = false, acceptedStatus, key = apiKey } = {}) {
  const started = performance.now();
  const record = { name, started_at: new Date().toISOString(), request: { method: data ? 'POST' : 'GET', url, ...(data ? { body: data } : {}), authentication: key === apiKey ? endpoint.auth.kind : 'intentionally-invalid-key' } };
  report.exchanges.push(record);
  try {
    const response = await fetch(url, { method: data ? 'POST' : 'GET', headers: { ...(data ? { 'Content-Type': 'application/json' } : {}), ...(key ? { Authorization: `Bearer ${key}` } : {}), 'X-Request-ID': `hitch-accept-${randomUUID()}` }, ...(data ? { body: JSON.stringify(data) } : {}), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(args.timeoutMs)]) : AbortSignal.timeout(args.timeoutMs) });
    record.response = { status: response.status, headers: Object.fromEntries(['content-type', 'x-request-id', 'retry-after'].flatMap(key => response.headers.has(key) ? [[key, response.headers.get(key)]] : [])) };
    if (!response.ok || acceptedStatus || mode === 'text') {
      record.response.body = await response.text();
      check(acceptedStatus ? acceptedStatus.includes(response.status) : response.ok, `HTTP ${response.status} for ${name}`);
      return record.response.body;
    }
    if (mode === 'json') {
      const text = await response.text();
      record.response.body = text;
      const json = JSON.parse(text);
      record.response.body = json;
      const result = view(json);
      record.response.usage_verification = verifyUsage(result);
      return result;
    }
    check(response.headers.get('content-type')?.includes('text/event-stream'), 'Streaming response is not SSE');
    const events = [];
    record.response.events = events;
    let buffer = '', totalBytes = 0, streamedText = '', finalResponse, usage, finishReason, done = false;
    const calls = new Map();
    function event(block) {
      const lines = block.split(/\r?\n/);
      const payload = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (!payload) return;
      if (payload === '[DONE]') { done = true; events.push({ data: '[DONE]', elapsed_ms: performance.now() - started }); return; }
      const json = JSON.parse(payload);
      events.push({ event: lines.find(line => line.startsWith('event:'))?.slice(6).trim(), data: json, elapsed_ms: performance.now() - started });
      check(json.type !== 'error' && json.type !== 'response.failed' && !json.error, 'Upstream reported a streaming error');
      if (responses) {
        if (json.type === 'response.output_text.delta' && json.delta) { streamedText += json.delta; onContent?.(); }
        if (['response.completed', 'response.incomplete'].includes(json.type)) finalResponse = json.response;
      } else {
        if (json.usage) usage = json.usage;
        for (const choice of json.choices ?? []) {
          if (choice.delta?.content) { streamedText += choice.delta.content; onContent?.(); }
          if (choice.finish_reason) finishReason = choice.finish_reason;
          for (const delta of choice.delta?.tool_calls ?? []) {
            const call = calls.get(delta.index) ?? { id: '', type: 'function', function: { name: '', arguments: '' } };
            call.id += delta.id ?? '';
            call.function.name += delta.function?.name ?? '';
            call.function.arguments += delta.function?.arguments ?? '';
            calls.set(delta.index, call);
          }
        }
      }
    }
    const decoder = new TextDecoder();
    for await (const chunk of response.body) {
      totalBytes += chunk.byteLength;
      check(totalBytes <= 8 * 1024 * 1024, 'SSE response exceeds probe capture limit');
      buffer += decoder.decode(chunk, { stream: true });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        event(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) event(buffer);
    if (responses) check(finalResponse, 'SSE ended without a terminal Responses snapshot');
    else check(done && finishReason, 'SSE ended without chat finish_reason and [DONE]');
    const result = view(responses ? finalResponse : { choices: [{ finish_reason: finishReason, message: { role: 'assistant', content: streamedText || null, ...(calls.size ? { tool_calls: [...calls.values()] } : {}) } }], usage });
    record.response.usage_verification = verifyUsage(result);
    result.streamed_text = streamedText;
    result.streamed_tool_arguments = responses ? events.filter(item => item.data?.type === 'response.function_call_arguments.delta').map(item => item.data.delta ?? '').join('') : [...calls.values()].map(item => item.function.arguments).join('');
    record.response.terminal = result.terminal;
    record.response.usage = result.usage;
    return result;
  } catch (error) {
    record.error = { name: error.name, message: error.message };
    if (expectedAbort && signal?.aborted) { record.expected_client_abort = true; return { aborted: true }; }
    throw error;
  } finally { record.duration_ms = performance.now() - started; }
}
async function checkStep(name, run) {
  const item = { name, passed: false };
  report.checks.push(item);
  try { await run(); item.passed = true; }
  catch (error) { item.error = error.message; throw error; }
}
async function generate(name, history, options = {}) { return exchange(name, generationUrl, { data: body(history, options), mode: options.stream ? 'sse' : 'json', ...options }); }
const marker = prefix => `${prefix}_${randomUUID().replaceAll('-', '')}`;
const assertText = (result, expected) => check(typeof result.text === 'string' && result.text.includes(expected), `Generated output did not contain expected marker ${expected}`);
async function toolRoundtrip(stream) {
  const parcel = marker('PARCEL'), delivery = marker('DELIVERED');
  const definition = { name: 'lookup_delivery_code', description: 'Look up the delivery confirmation code for a parcel.', parameters: { type: 'object', properties: { parcel_id: { type: 'string' } }, required: ['parcel_id'], additionalProperties: false }, strict: true };
  const tools = [responses ? { type: 'function', ...definition } : { type: 'function', function: definition }];
  const toolChoice = responses ? { type: 'function', name: definition.name } : { type: 'function', function: { name: definition.name } };
  const history = [user(`Call lookup_delivery_code with parcel_id ${parcel}. Then tell me the delivery_code returned by the tool. Do not invent the result.`)];
  const first = await generate(`tools-${stream ? 'stream' : 'json'}-call`, history, { tools, toolChoice, stream });
  check(first.tools.length === 1, 'Expected exactly one function call');
  if (stream) check(first.streamed_tool_arguments.length > 0, 'Streaming tool response did not emit function arguments');
  const call = first.tools[0];
  check(call.id && call.name === definition.name, 'Model returned an undeclared function or missing call id');
  const input = JSON.parse(call.arguments);
  check(input.parcel_id === parcel && Object.keys(input).length === 1, 'Tool arguments do not match the requested parcel');
  // Execute this one allowlisted, pure local tool; then send its actual output back.
  const output = { parcel_id: input.parcel_id, delivery_code: delivery };
  report.exchanges.push({ name: 'local-tool-execution', tool: call.name, call_id: call.id, input, output });
  const resultItem = responses ? { type: 'function_call_output', call_id: call.id, output: JSON.stringify(output) } : { role: 'tool', tool_call_id: call.id, content: JSON.stringify(output) };
  const final = await generate(`tools-${stream ? 'stream' : 'json'}-continue`, [...history, ...first.history, resultItem], { tools, toolChoice: 'none', stream });
  assertText(final, delivery);
  if (stream) check(final.streamed_text.includes(delivery), 'Tool continuation text was not delivered in SSE deltas');
  check(final.tools.length === 0, 'Model did not finish after the tool result');
}
async function metricsSnapshot(label) {
  return Promise.all(args.workerMetrics.map(async (url, index) => {
    const text = await exchange(`${label}-worker-${index + 1}-metrics`, url, { mode: 'text' });
    const matches = [...text.matchAll(/^sglang:num_requests_total(?:\{[^}]*\})?\s+([0-9eE.+-]+)/gm)];
    return { url, request_count: matches.length ? matches.reduce((sum, match) => sum + Number(match[1]), 0) : null };
  }));
}
async function cancellationIsolation() {
  const cancelled = new AbortController();
  const cancelledFirst = Promise.withResolvers(), survivorFirst = Promise.withResolvers();
  let cancelledEnded = false, survivorEnded = false;
  const cancelMarker = marker('CANCEL'), survivorMarker = marker('SURVIVE');
  const prompt = value => [user(`Begin with ${value}. Then write a long tutorial with at least 150 numbered sentences about arithmetic, explaining each sentence in detail.`)];
  const cancelRequest = generate('cancel-one-active-stream', prompt(cancelMarker), { stream: true, signal: cancelled.signal, expectedAbort: true, onContent: () => cancelledFirst.resolve() }).finally(() => { cancelledEnded = true; cancelledFirst.reject(new Error('Cancelled request ended before content')); });
  const survivorRequest = generate('independent-surviving-stream', prompt(survivorMarker), { stream: true, onContent: () => survivorFirst.resolve() }).finally(() => { survivorEnded = true; survivorFirst.reject(new Error('Survivor ended before content')); });
  // Attach handlers immediately so a failure cannot leave an unhandled rejection.
  const settled = Promise.allSettled([cancelRequest, survivorRequest]);
  try {
    await Promise.all([cancelledFirst.promise, survivorFirst.promise]);
    check(!cancelledEnded && !survivorEnded, 'Streams did not overlap long enough to observe cancellation isolation');
    report.cancellation = { aborted_at: new Date().toISOString(), both_streams_had_content: true, both_streams_incomplete_at_abort: true, scope: 'only the cancelled request AbortController; no stop, drain, flush, or release API' };
    cancelled.abort(new DOMException('Acceptance cancels this request only', 'AbortError'));
  } catch (error) { cancelled.abort(); await settled; throw error; }
  const [a, b] = await settled;
  check(a.status === 'fulfilled' && a.value.aborted, 'Cancelled stream did not observe client abort');
  if (b.status === 'rejected') throw b.reason;
  assertText(b.value, survivorMarker);
  const after = marker('STILL_SERVING');
  assertText(await generate('service-remains-available-after-cancel', [user(`Reply with exactly ${after}.`)]), after);
}
try {
  if (apiKey) await checkStep('authentication rejects an invalid key on the generation endpoint', () => exchange('invalid-auth', generationUrl, { data: body([user('Say hello.')]), acceptedStatus: [401, 403], key: `invalid-${randomUUID()}` }));
  await checkStep('normal-budget generation and usage', async () => {
    const expected = marker('GENERATED');
    assertText(await generate('plain-generation', [user(`Reply with exactly ${expected}.`)]), expected);
  });
  await checkStep('complete multi-turn history without previous_response_id', async () => {
    const expected = marker('REMEMBER');
    const history = [user(`Remember this marker: ${expected}. Reply with Remembered.`)];
    const first = await generate('history-first-turn', history);
    assertText(await generate('history-second-turn', [...history, ...first.history, user('Repeat the marker from my first message exactly.')]), expected);
  });
  await checkStep('SSE terminal event and usage', async () => {
    const expected = marker('STREAMED');
    const result = await generate('stream-generation', [user(`Reply with exactly ${expected}.`)], { stream: true });
    assertText(result, expected);
    check(result.streamed_text.includes(expected), 'Text was not delivered in SSE deltas');
  });
  await checkStep('JSON tool call, local execution, result, and continuation', () => toolRoundtrip(false));
  await checkStep('streaming tool call, local execution, result, and continuation', () => toolRoundtrip(true));
  const before = await metricsSnapshot('before-concurrency');
  if (args.gatewayMetrics) await exchange('before-gateway-metrics', args.gatewayMetrics, { mode: 'text' });
  await checkStep('independent concurrent requests', async () => {
    await Promise.all(Array.from({ length: args.concurrent }, async (_, index) => {
      const expected = marker(`CONCURRENT_${index}`);
      assertText(await generate(`concurrent-${index + 1}`, [user(`Reply with exactly ${expected}.`)]), expected);
    }));
  });
  await checkStep('abort an active stream while another completes and service remains usable', cancellationIsolation);
  const after = await metricsSnapshot('after-concurrency');
  if (args.gatewayMetrics) await exchange('after-gateway-metrics', args.gatewayMetrics, { mode: 'text' });
  report.worker_distribution = before.map((entry, index) => ({ url: entry.url, before: entry.request_count, after: after[index].request_count, delta: entry.request_count !== null && after[index].request_count !== null ? after[index].request_count - entry.request_count : null }));
  if (args.expectWorkers) await checkStep('server metrics show requests on every expected worker', async () => {
    check(report.worker_distribution.every(entry => Number.isFinite(entry.delta) && entry.delta > 0), 'Missing worker request counters, reset counters, or no positive request delta on a worker');
  });
  report.passed = true;
} catch (error) {
  report.error = { name: error.name, message: error.message };
} finally {
  report.finished_at = new Date().toISOString();
  const output = resolve(args.out);
  await mkdir(dirname(output), { recursive: true });
  await writeFile(output, `${JSON.stringify(redact(report), null, 2)}\n`, { mode: 0o600 });
  console.log(`${report.passed ? 'PASS' : 'FAIL'}: HTTP acceptance report written to ${output}`);
  if (!report.passed) { console.error(redact(report.error?.message ?? 'Acceptance failed')); process.exitCode = 1; }
}
