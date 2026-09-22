#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

function defaults() {
  return {
    schema_version: 1, local_llm_target_percent: 50, wait_for_results: true,
    wait_timeout_sec: 1800, eligible_subagent_tasks: 0, ganglion_subagent_tasks: 0,
    other_local_subagent_tasks: 0, other_subagent_tasks: 0, unavailable_subagent_tasks: 0,
    last_route: 'none', updated_at: null, recent_task_ids: [], conservation_mode: false,
    usage: { session: null, weekly: null, triggered_buckets: [] },
  };
}

function integer(value, name, min, max) {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer from ${min} to ${max}`);
  }
  return value;
}

function validate(policy) {
  integer(policy.local_llm_target_percent, 'target', 0, 100);
  integer(policy.wait_timeout_sec, 'timeout', 1, 86400);
  for (const name of ['eligible_subagent_tasks', 'ganglion_subagent_tasks',
    'other_local_subagent_tasks', 'other_subagent_tasks', 'unavailable_subagent_tasks']) {
    integer(policy[name], name, 0, Number.MAX_SAFE_INTEGER);
  }
  if (typeof policy.wait_for_results !== 'boolean' || typeof policy.conservation_mode !== 'boolean') {
    throw new Error('wait_for_results and conservation_mode must be booleans');
  }
  if (!Array.isArray(policy.recent_task_ids) || policy.recent_task_ids.some(id => typeof id !== 'string')) {
    throw new Error('recent_task_ids must be a list of task IDs');
  }
  if (!policy.usage || !Array.isArray(policy.usage.triggered_buckets) ||
      policy.usage.triggered_buckets.some(bucket => !['session', 'weekly'].includes(bucket))) {
    throw new Error('Invalid usage state');
  }
  return policy;
}

function summarize(policy) {
  const local = policy.ganglion_subagent_tasks + policy.other_local_subagent_tasks;
  const share = policy.eligible_subagent_tasks ? Math.round(1000 * local / policy.eligible_subagent_tasks) / 10 : null;
  return {
    ...policy, local_llm_share_percent: share,
    target_met: policy.local_llm_target_percent === 0 || (share !== null && share >= policy.local_llm_target_percent),
    prefer_local: policy.local_llm_target_percent > 0 && (share === null || share < policy.local_llm_target_percent),
  };
}

function readPolicy(statePath) {
  let data;
  try { data = JSON.parse(fs.readFileSync(statePath, 'utf8').replace(/^\uFEFF/, '')); }
  catch (error) {
    if (error.code === 'ENOENT') return summarize(defaults());
    throw new Error(`Cannot read routing policy: ${error instanceof SyntaxError ? 'invalid JSON' : error.code || error.message}`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.schema_version !== 1) {
    throw new Error('Unsupported routing policy schema');
  }
  return summarize(validate({ ...defaults(), ...data }));
}

function applyAction(policy, action, options = {}) {
  const next = structuredClone(policy);
  if (action === 'set') {
    if (options.target !== undefined) next.local_llm_target_percent = integer(options.target, 'target', 0, 100);
    if (options.wait !== undefined) next.wait_for_results = options.wait;
    if (options.timeout !== undefined) next.wait_timeout_sec = integer(options.timeout, 'timeout', 1, 86400);
  } else if (action === 'record') {
    if (options.eligible !== true) throw new Error('record requires --eligible');
    const counters = { ganglion: 'ganglion_subagent_tasks', local: 'other_local_subagent_tasks',
      native: 'other_subagent_tasks', unavailable: 'unavailable_subagent_tasks' };
    if (!Object.hasOwn(counters, options.route)) throw new Error('Unknown outcome route');
    if (options.taskId !== undefined && (typeof options.taskId !== 'string' || !options.taskId.trim() || options.taskId.length > 128)) {
      throw new Error('task-id must contain 1 to 128 characters');
    }
    if (options.taskId && next.recent_task_ids.includes(options.taskId)) return summarize(next);
    next.eligible_subagent_tasks += 1;
    next[counters[options.route]] += 1;
    next.last_route = options.route;
    if (options.taskId) next.recent_task_ids = [...next.recent_task_ids, options.taskId].slice(-256);
  } else if (action === 'reset') {
    for (const key of ['eligible_subagent_tasks', 'ganglion_subagent_tasks', 'other_local_subagent_tasks',
      'other_subagent_tasks', 'unavailable_subagent_tasks']) next[key] = 0;
    next.recent_task_ids = [];
    next.last_route = 'none';
  } else if (action === 'observe-usage') {
    if (!['session', 'weekly'].includes(options.bucket)) throw new Error('bucket must be session or weekly');
    integer(options.used, 'used', 0, 100);
    const triggered = new Set(next.usage.triggered_buckets);
    const unknownTrigger = next.conservation_mode && triggered.size === 0;
    if (options.used >= 90) triggered.add(options.bucket);
    else if (options.resetObserved === true) triggered.delete(options.bucket);
    next.usage[options.bucket] = { used_percent: options.used, observed_at: new Date().toISOString() };
    next.usage.triggered_buckets = [...triggered];
    next.conservation_mode = unknownTrigger || triggered.size > 0;
  } else { throw new Error(`Unknown action: ${action}`); }
  next.updated_at = new Date().toISOString();
  return summarize(validate(next));
}

function updatePolicy(statePath, action, options) {
  if (action === 'get') return readPolicy(statePath);
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const lockPath = `${statePath}.lock`;
  let lock;
  try { lock = fs.openSync(lockPath, 'wx', 0o600); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error('Routing policy is locked; retry after the other writer finishes');
    throw error;
  }
  const temporary = `${statePath}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(lock, JSON.stringify({ pid: process.pid }));
    const next = applyAction(readPolicy(statePath), action, options);
    fs.writeFileSync(temporary, JSON.stringify(next, null, 2) + '\n', { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    fs.renameSync(temporary, statePath);
    return next;
  } finally {
    fs.closeSync(lock);
    fs.rmSync(lockPath);
    fs.rmSync(temporary, { force: true });
  }
}

function parseArgs(args) {
  const [action = 'get', ...rest] = args;
  const options = {};
  const names = { target: 'target', wait: 'wait', timeout: 'timeout', route: 'route',
    'task-id': 'taskId', bucket: 'bucket', used: 'used', state: 'statePath' };
  for (let i = 0; i < rest.length; i++) {
    const flag = rest[i];
    if (flag === '--eligible') { options.eligible = true; continue; }
    if (flag === '--reset-observed') { options.resetObserved = true; continue; }
    const name = names[flag.slice(2)];
    if (!flag.startsWith('--') || !name || rest[i + 1] === undefined) throw new Error(`Unknown or incomplete option: ${flag}`);
    const raw = rest[++i];
    if (['target', 'timeout', 'used'].includes(name)) {
      if (!/^\d+$/.test(raw)) throw new Error(`${flag} must be an integer`);
      options[name] = Number(raw);
    } else if (name === 'wait') {
      if (!['true', 'false'].includes(raw)) throw new Error('--wait must be true or false');
      options.wait = raw === 'true';
    } else options[name] = raw;
  }
  return { action, options };
}

if (require.main === module) {
  try {
    const { action, options } = parseArgs(process.argv.slice(2));
    const root = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
    const statePath = path.resolve(options.statePath || path.join(root, 'state', 'claude-routing-policy.json'));
    console.log(JSON.stringify(updatePolicy(statePath, action, options), null, 2));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = { defaults, readPolicy, applyAction, updatePolicy, parseArgs, summarize };
