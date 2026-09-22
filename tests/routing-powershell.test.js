'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { spawn, spawnSync } = require('node:child_process');
const executable = process.platform === 'win32' ? 'powershell.exe' : 'pwsh';
const available = spawnSync(executable, ['-NoProfile', '-Command', '$PSVersionTable.PSVersion.ToString()'], { windowsHide: true }).status === 0;
const directory = path.resolve(__dirname, '../skills/claude-model-routing/scripts');

function run(script, args = [], input = '', extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path.join(directory, script), ...args], {
      windowsHide: true, env: { ...process.env, ROUTER_TEST_KEY: 'synthetic-test-token', ...extraEnv },
    });
    let stdout = '', stderr = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('Mock PowerShell test timed out')); }, 15000);
    child.stdout.on('data', chunk => stdout += chunk.toString('utf8'));
    child.stderr.on('data', chunk => stderr += chunk.toString('utf8'));
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => { clearTimeout(timer); code === 0 ? resolve(stdout.trim()) : reject(new Error(stderr)); });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

async function mock(t, { busy = false, unavailable = false } = {}) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let text = '';
    for await (const chunk of req) text += chunk.toString('utf8');
    requests.push({ url: req.url, body: text ? JSON.parse(text) : null, auth: req.headers.authorization });
    let body;
    if (unavailable) { res.writeHead(503); return res.end('{}'); }
    if (req.url.endsWith('/health')) body = { status: 'ok' };
    else if (req.url.endsWith('/models')) body = { data: [{ id: 'test-worker' }] };
    else if (req.url.endsWith('/runtime-instances')) body = { runtime_instances: [{ id: 'test', lifecycle: 'ready', in_flight: busy ? 1 : 0, leased_slots: 0, queue_depth: 0 }] };
    else if (req.url.endsWith('/metrics')) body = { queues: [] };
    else if (req.url.endsWith('/chat/completions')) body = { choices: [{ message: { content: 'Evidence: café ✓' } }] };
    else if (req.url.endsWith('/responses')) body = { output: [{ content: [{ type: 'output_text', text: 'Evidence: café ✓' }] }] };
    else { res.writeHead(404); return res.end('{}'); }
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(body));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return { base: `http://127.0.0.1:${server.address().port}/v1`, requests };
}

function sweepArgs(local, continuity, gateway) {
  return ['-LocalBaseUrl', local.base, '-ContinuityBaseUrl', continuity.base, '-GatewayBaseUrl', gateway.base,
    '-LocalModel', 'test-worker', '-ContinuityModel', 'test-worker', '-GatewayModel', 'test-worker',
    '-LocalApiKeyEnv', 'ROUTER_TEST_KEY', '-ContinuityApiKeyEnv', 'ROUTER_TEST_KEY', '-GatewayApiKeyEnv', 'ROUTER_TEST_KEY'];
}

test('PowerShell cascade stops at ready resident and does a real completion probe against a mock', { skip: !available }, async t => {
  const resident = await mock(t), continuity = await mock(t), gateway = await mock(t);
  const result = JSON.parse(await run('sweep-ganglion-resources.ps1', sweepArgs(resident, continuity, gateway)));
  assert.equal(result.selected.base_url, resident.base);
  assert.ok(resident.requests.some(req => req.url.endsWith('/chat/completions')));
  assert.equal(continuity.requests.length, 0);
  assert.equal(gateway.requests.length, 0);
  assert.ok(resident.requests.every(req => req.auth === 'Bearer synthetic-test-token'));
});

test('PowerShell cascade skips continuity for busy resident and uses Responses gateway', { skip: !available }, async t => {
  const resident = await mock(t, { busy: true }), continuity = await mock(t), gateway = await mock(t);
  const result = JSON.parse(await run('sweep-ganglion-resources.ps1', sweepArgs(resident, continuity, gateway)));
  assert.equal(result.selected.base_url, gateway.base);
  assert.equal(result.selected.wire_api, 'Responses');
  assert.equal(continuity.requests.length, 0);
  assert.ok(gateway.requests.some(req => req.url.endsWith('/responses')));
});

test('PowerShell cascade uses continuity for unavailable resident', { skip: !available }, async t => {
  const resident = await mock(t, { unavailable: true }), continuity = await mock(t), gateway = await mock(t);
  const result = JSON.parse(await run('sweep-ganglion-resources.ps1', sweepArgs(resident, continuity, gateway)));
  assert.equal(result.selected.base_url, continuity.base);
  assert.equal(gateway.requests.length, 0);
});

test('external gateway is opt-in even when a gateway token is present', { skip: !available }, async t => {
  const resident = await mock(t, { unavailable: true });
  const continuity = await mock(t, { unavailable: true });
  const args = ['-LocalBaseUrl', resident.base, '-ContinuityBaseUrl', continuity.base,
    '-LocalApiKeyEnv', 'ROUTER_TEST_KEY', '-ContinuityApiKeyEnv', 'ROUTER_TEST_KEY'];
  const result = JSON.parse(await run('sweep-ganglion-resources.ps1', args, '', {
    GANGLION_GATEWAY_BASE_URL: '', GANGLION_API_KEY: 'synthetic-gateway-token',
  }));
  assert.equal(result.selected, null);
  assert.match(result.checks.at(-1).reason, /not configured/);
});

test('bundled worker preserves Unicode and handles both API formats without tools', { skip: !available }, async t => {
  const endpoint = await mock(t);
  for (const wire of ['ChatCompletions', 'Responses']) {
    const result = await run('invoke-ganglion-worker.ps1', ['-BaseUrl', endpoint.base, '-Model', 'test-worker', '-ApiKeyEnv', 'ROUTER_TEST_KEY', '-WireApi', wire], 'Summarize: café ✓');
    assert.equal(result, 'Evidence: café ✓');
    const body = endpoint.requests.at(-1).body;
    const messages = body.messages || body.input;
    assert.equal(messages.at(-1).content, 'Summarize: café ✓');
    assert.equal(body.tools, undefined);
    if (wire === 'Responses') assert.equal(body.store, false);
  }
});

test('bundled probe catalog-only does not generate and normal probe checks completion', { skip: !available }, async t => {
  const endpoint = await mock(t);
  const args = ['-BaseUrl', endpoint.base, '-Model', 'test-worker', '-ApiKeyEnv', 'ROUTER_TEST_KEY', '-WireApi', 'Responses'];
  assert.match(await run('test-ganglion-access.ps1', [...args, '-SkipCompletion']), /catalog OK/);
  assert.equal(endpoint.requests.length, 1);
  assert.match(await run('test-ganglion-access.ps1', args), /completion=usable/);
  assert.ok(endpoint.requests.some(req => req.url.endsWith('/responses')));
});

test('legacy PowerShell policy interface forwards to portable state manager', { skip: !available }, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-legacy-policy-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const state = path.join(root, 'state.json');
  const result = JSON.parse(await run('manage-claude-routing-policy.ps1', ['-Action', 'Set', '-LocalTargetPercent', '70', '-StatePath', state]));
  assert.equal(result.local_llm_target_percent, 70);
  const recorded = JSON.parse(await run('manage-claude-routing-policy.ps1', ['-Action', 'Record', '-EligibleTask', '-SubagentRoute', 'ganglion', '-TaskId', 'legacy-test', '-StatePath', state]));
  assert.equal(recorded.ganglion_subagent_tasks, 1);
});
