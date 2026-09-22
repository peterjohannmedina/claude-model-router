'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { defaults, readPolicy, updatePolicy, applyAction, parseArgs } = require('../skills/claude-model-routing/scripts/manage-routing-policy');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-policy-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return path.join(root, 'state', 'policy.json');
}

test('get is read-only and cold start prefers local unless disabled', t => {
  const state = fixture(t);
  const value = updatePolicy(state, 'get');
  assert.equal(value.prefer_local, true);
  assert.equal(value.target_met, false);
  assert.equal(value.local_llm_share_percent, null);
  assert.equal(fs.existsSync(path.dirname(state)), false);
  const disabled = updatePolicy(state, 'set', { target: 0 });
  assert.equal(disabled.prefer_local, false);
  assert.equal(disabled.target_met, true);
});

test('migrates deployed BOM policy and preserves counters and wait preferences', t => {
  const state = fixture(t);
  fs.mkdirSync(path.dirname(state));
  const old = { schema_version: 1, local_llm_target_percent: 75, wait_for_results: false,
    wait_timeout_sec: 900, eligible_subagent_tasks: 3, ganglion_subagent_tasks: 1,
    other_subagent_tasks: 1, unavailable_subagent_tasks: 1 };
  fs.writeFileSync(state, '\uFEFF' + JSON.stringify(old));
  const result = updatePolicy(state, 'set', { target: 60 });
  assert.equal(result.eligible_subagent_tasks, 3);
  assert.equal(result.local_llm_share_percent, 33.3);
  assert.equal(result.wait_for_results, false);
  assert.equal(result.wait_timeout_sec, 900);
  assert.equal(result.other_local_subagent_tasks, 0);
});

test('records all local routes, unavailable opportunities, and deduplicates task IDs', t => {
  const state = fixture(t);
  for (const [taskId, route] of [['a', 'ganglion'], ['b', 'native'], ['c', 'local'], ['d', 'unavailable']]) {
    updatePolicy(state, 'record', { eligible: true, taskId, route });
  }
  const result = updatePolicy(state, 'record', { eligible: true, taskId: 'a', route: 'native' });
  assert.equal(result.eligible_subagent_tasks, 4);
  assert.equal(result.local_llm_share_percent, 50);
  assert.equal(result.prefer_local, false);
  assert.equal(result.other_subagent_tasks, 1);
  assert.equal(result.unavailable_subagent_tasks, 1);
  assert.throws(() => updatePolicy(state, 'record', { route: 'native' }), /eligible/);
});

test('conservation survives missing readings, lower readings, and history reset', t => {
  const state = fixture(t);
  updatePolicy(state, 'observe-usage', { bucket: 'session', used: 90 });
  updatePolicy(state, 'observe-usage', { bucket: 'weekly', used: 95 });
  updatePolicy(state, 'observe-usage', { bucket: 'weekly', used: 10 });
  assert.equal(updatePolicy(state, 'reset').conservation_mode, true);
  assert.equal(updatePolicy(state, 'observe-usage', { bucket: 'session', used: 3, resetObserved: true }).conservation_mode, true);
  assert.equal(readPolicy(state).conservation_mode, true);
  assert.equal(updatePolicy(state, 'observe-usage', { bucket: 'weekly', used: 4, resetObserved: true }).conservation_mode, false);
});

test('unknown legacy conservation trigger is never cleared by guessing', () => {
  const policy = { ...defaults(), conservation_mode: true };
  assert.equal(applyAction(policy, 'observe-usage', { bucket: 'weekly', used: 0, resetObserved: true }).conservation_mode, true);
});

test('lock conflicts and malformed state leave original data intact', t => {
  const state = fixture(t);
  updatePolicy(state, 'set', { target: 65 });
  const before = fs.readFileSync(state, 'utf8');
  fs.writeFileSync(state + '.lock', 'held');
  assert.throws(() => updatePolicy(state, 'reset'), /locked/);
  assert.equal(fs.readFileSync(state, 'utf8'), before);
  assert.equal(fs.readFileSync(state + '.lock', 'utf8'), 'held');
  fs.unlinkSync(state + '.lock');
  fs.writeFileSync(state, 'corrupted');
  assert.throws(() => updatePolicy(state, 'set', { target: 50 }), /invalid JSON/);
  assert.equal(fs.readFileSync(state, 'utf8'), 'corrupted');
  assert.equal(fs.existsSync(state + '.lock'), false);
});

test('rejects invalid controls and accepts explicit wait false', () => {
  for (const target of [-1, 101, NaN, 3.5]) assert.throws(() => applyAction(defaults(), 'set', { target }));
  assert.throws(() => parseArgs(['set', '--wait', 'yes']), /true or false/);
  assert.throws(() => parseArgs(['set', '--target', '1e2']), /integer/);
  assert.throws(() => parseArgs(['set', '--unknown', 'x']), /Unknown/);
  assert.equal(parseArgs(['set', '--wait', 'false']).options.wait, false);
});
