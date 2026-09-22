'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { dispatch, runPowerShell } = require('../skills/claude-model-routing/scripts/run-ganglion-task');
const { readPolicy, updatePolicy } = require('../skills/claude-model-routing/scripts/manage-routing-policy');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-dispatch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { statePath: path.join(root, 'policy.json'), packet: 'Classify this bounded example: red.', eligible: true, taskId: 'task-1' };
}
const selected = { id: 'resident', base_url: 'http://127.0.0.1:8471/v1', model: 'ganglion', api_key_env: 'TEST_ONLY_KEY', wire_api: 'ChatCompletions' };

test('uses exactly the selected protocol and stdin packet, then records once', async t => {
  const options = fixture(t);
  const calls = [];
  const runner = async (...args) => {
    calls.push(args);
    return calls.length === 1 ? JSON.stringify({ selected: { ...selected, wire_api: 'Responses' } }) : 'candidate evidence';
  };
  const result = await dispatch(options, runner);
  assert.equal(result.status, 'succeeded');
  assert.equal(result.result, 'candidate evidence');
  assert.equal(calls.length, 2);
  assert.equal(calls[0][0], 'sweep-ganglion-resources.ps1');
  assert.equal(calls[1][1][calls[1][1].indexOf('-WireApi') + 1], 'Responses');
  assert.equal(calls[1][2], options.packet);
  assert.ok(calls[1][3] > 0 && calls[1][3] <= 1800000);
  assert.equal(readPolicy(options.statePath).ganglion_subagent_tasks, 1);
  assert.equal((await dispatch(options, runner)).status, 'already_recorded');
  assert.equal(calls.length, 2);
});

test('target met returns control without inventing a native outcome', async t => {
  const options = fixture(t);
  updatePolicy(options.statePath, 'set', { target: 0 });
  const result = await dispatch(options, () => { throw new Error('must not call'); });
  assert.equal(result.status, 'native_required');
  assert.equal(readPolicy(options.statePath).eligible_subagent_tasks, 0);
});

test('unavailable sweep and failed worker are counted once without fabricated text', async t => {
  const options = fixture(t);
  const result = await dispatch(options, async () => JSON.stringify({ selected: null }));
  assert.equal(result.status, 'unavailable');
  assert.equal(result.result, undefined);
  assert.equal(readPolicy(options.statePath).unavailable_subagent_tasks, 1);
  let call = 0;
  const failed = await dispatch({ ...options, taskId: 'task-2' }, async () => {
    if (++call === 1) return JSON.stringify({ selected });
    throw new Error('Local worker exceeded the routing wait budget');
  });
  assert.match(failed.reason, /budget/);
  assert.equal(readPolicy(options.statePath).unavailable_subagent_tasks, 2);
});

test('an accounting lock preserves completed result for record-only retry', async t => {
  const options = fixture(t);
  let call = 0;
  const result = await dispatch(options, async () => {
    if (++call === 1) return JSON.stringify({ selected });
    fs.writeFileSync(options.statePath + '.lock', 'other writer');
    return 'completed evidence';
  });
  assert.equal(result.status, 'succeeded');
  assert.equal(result.accounting_pending, true);
  assert.equal(result.accounting_route, 'ganglion');
  assert.equal(result.result, 'completed evidence');
  assert.equal(call, 2);
  fs.unlinkSync(options.statePath + '.lock');
  updatePolicy(options.statePath, 'record', { eligible: true, route: result.accounting_route, taskId: result.task_id });
  assert.equal(readPolicy(options.statePath).eligible_subagent_tasks, 1);
});

test('requires eligibility and bounded packet before any process starts', async t => {
  const options = fixture(t);
  await assert.rejects(dispatch({ ...options, eligible: false }), /screened/);
  await assert.rejects(dispatch({ ...options, packet: 'a'.repeat(50001) }), /50000/);
  await assert.rejects(dispatch({ ...options, maxTokens: 4097 }), /4096/);
  assert.equal(fs.existsSync(options.statePath), false);
});

test('real process runner reports command failure without exposing stderr', { skip: process.platform !== 'win32' }, async () => {
  await assert.rejects(runPowerShell('missing-worker.ps1', [], '', 5000), /command failed/);
});
