#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { StringDecoder } = require('node:string_decoder');
const { readPolicy, updatePolicy } = require('./manage-routing-policy');

function runPowerShell(script, args, input, timeoutMs) {
  const executable = process.platform === 'win32' ? 'powershell.exe' : 'pwsh';
  return new Promise((resolve, reject) => {
    const child = spawn(executable, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
      path.join(__dirname, script), ...args], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '';
    const decoder = new StringDecoder('utf8');
    let bytes = 0;
    let failed = false;
    const fail = message => { failed = true; child.kill(); reject(new Error(message)); };
    const timer = setTimeout(() => fail('Local worker exceeded the routing wait budget'), timeoutMs);
    for (const stream of [child.stdout, child.stderr]) {
      stream.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 256 * 1024) return fail('Local worker exceeded the output limit');
        if (stream === child.stdout) output += decoder.write(chunk);
      });
    }
    child.on('error', error => { clearTimeout(timer); reject(new Error(`Cannot start PowerShell: ${error.code}`)); });
    child.on('close', code => {
      clearTimeout(timer);
      if (failed) return;
      // Do not return raw provider/PowerShell errors: they may echo request data or credentials.
      if (code !== 0) reject(new Error(`Local worker command failed (exit ${code})`));
      else resolve((output + decoder.end()).replace(/^\uFEFF/, '').trim());
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input || '');
  });
}

async function dispatch({ packet, statePath, eligible, taskId = randomUUID(), maxTokens = 512 }, runner = runPowerShell) {
  if (eligible !== true) throw new Error('Local dispatch requires an explicitly screened eligible task');
  if (typeof packet !== 'string' || !packet.trim() || packet.length > 50000) throw new Error('Task packet must contain 1 to 50000 characters');
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 4096) throw new Error('max-tokens must be from 1 to 4096');
  if (typeof taskId !== 'string' || !taskId.trim() || taskId.length > 128) throw new Error('Invalid task ID');
  const policy = readPolicy(statePath);
  if (policy.recent_task_ids.includes(taskId)) return { status: 'already_recorded', task_id: taskId };
  if (!policy.prefer_local) return { status: 'native_required', task_id: taskId, reason: 'Local target is disabled or met; parent must record the eventual native outcome.' };
  const deadline = Date.now() + policy.wait_timeout_sec * 1000;
  const remaining = () => {
    const ms = deadline - Date.now();
    if (ms <= 0) throw new Error('Routing wait budget exhausted');
    return ms;
  };
  let selected;
  let text;
  let reason;
  try {
    const sweepOutput = await runner('sweep-ganglion-resources.ps1', [], '', remaining());
    let sweep;
    try { sweep = JSON.parse(sweepOutput); }
    catch { throw new Error('Ganglion sweep returned invalid JSON'); }
    selected = sweep.selected;
    if (!selected) throw new Error('No ready Ganglion route');
    if (!['ChatCompletions', 'Responses'].includes(selected.wire_api) ||
        !['base_url', 'model', 'api_key_env'].every(key => typeof selected[key] === 'string' && selected[key])) {
      throw new Error('Invalid Ganglion route selection');
    }
    const timeout = Math.max(1, Math.min(1800, Math.floor(remaining() / 1000)));
    text = await runner('invoke-ganglion-worker.ps1', [
      '-BaseUrl', selected.base_url, '-Model', selected.model, '-ApiKeyEnv', selected.api_key_env,
      '-WireApi', selected.wire_api, '-MaxTokens', String(maxTokens), '-TimeoutSec', String(timeout),
    ], packet, remaining());
    if (!text.trim()) throw new Error('Local worker returned no usable text');
  } catch (error) { reason = error.message; }
  // One owner records the attempt, after inference has finished. Never retry a successful
  // inference just because a concurrent policy writer temporarily holds the state lock.
  const route = reason ? 'unavailable' : 'ganglion';
  try { updatePolicy(statePath, 'record', { eligible: true, route, taskId }); }
  catch (error) {
    return { status: reason ? 'unavailable' : 'succeeded', task_id: taskId,
      result: reason ? undefined : text, reason, accounting_pending: true, accounting_route: route };
  }
  return { status: reason ? 'unavailable' : 'succeeded', task_id: taskId,
    route: selected?.id, result: reason ? undefined : text, reason, accounting_pending: false };
}

if (require.main === module) {
  (async () => {
    const args = process.argv.slice(2);
    const options = {};
    const names = { '--input': 'input', '--state': 'statePath', '--task-id': 'taskId', '--max-tokens': 'maxTokens' };
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--eligible') options.eligible = true;
      else if (names[args[i]] && args[i + 1] !== undefined) options[names[args[i]]] = args[++i];
      else throw new Error(`Unknown or incomplete option: ${args[i]}`);
    }
    const root = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
    options.statePath = path.resolve(options.statePath || path.join(root, 'state', 'claude-routing-policy.json'));
    options.packet = fs.readFileSync(options.input || 0, 'utf8');
    if (options.maxTokens !== undefined) options.maxTokens = Number(options.maxTokens);
    console.log(JSON.stringify(await dispatch(options), null, 2));
  })().catch(error => { console.error(error.message); process.exitCode = 1; });
}

module.exports = { dispatch, runPowerShell };
