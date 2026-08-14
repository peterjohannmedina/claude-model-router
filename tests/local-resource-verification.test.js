'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const {
  DEFAULTS,
  agentSelector,
  checkLiteLLM,
  hookResponse,
  isLocalAgentInput,
  parseTomlSections,
  summarize,
  validateConfiguration,
} = require('../scripts/verify-local-inference-resource.js');

test('parses the Codex resource fields used by the verifier', () => {
  const sections = parseTomlSections(`
model = "gpt-5.6-luna"

[model_providers.litellm_muse]
base_url = "http://192.168.1.214:4000/v1"
wire_api = "responses"

[mcp_servers.local_inference]
enabled = true
args = ["/tmp/local-inference-mcp.js"]
tool_timeout_sec = 60

[mcp_servers.local_inference.env]
LITELLM_BASE_URL = "http://192.168.1.214:4000/v1"
LITELLM_MODEL = "muse"
`);
  assert.equal(sections.get('model_providers.litellm_muse').wire_api, 'responses');
  assert.deepEqual(sections.get('mcp_servers.local_inference').args, ['/tmp/local-inference-mcp.js']);
  assert.equal(sections.get('mcp_servers.local_inference').tool_timeout_sec, 60);
  assert.equal(sections.get('mcp_servers.local_inference.env').LITELLM_MODEL, 'muse');
});

test('rejects unavailable or excessive local-resource configuration', () => {
  const errors = validateConfiguration({
    providerBaseUrl: 'http://192.168.1.214:4000/v1',
    providerWireApi: 'responses',
    providerStreamIdleTimeoutMs: 120000,
    litellmBaseUrl: 'http://192.168.1.214:4000/v1',
    model: 'muse',
    mcpEnabled: true,
    mcpCommand: '/opt/homebrew/bin/node',
    mcpArgs: ['/tmp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpStartupTimeoutSec: 20,
    mcpToolTimeoutSec: 120,
    mcpEnabledTools: ['local_extract', 'local_classify', 'local_review_diff'],
    workerTimeoutMs: 120000,
    adapterPath: '/tmp/local-inference-mcp.js',
    agentName: 'local-muse-worker',
    agentModel: 'muse',
    agentProvider: 'litellm_muse',
  }, 'any');
  assert.match(errors.join('\n'), /startup timeout/);
  assert.match(errors.join('\n'), /tool timeout/);
  assert.match(errors.join('\n'), /worker timeout/);
  assert.match(errors.join('\n'), /provider stream timeout/);
});

test('rejects an output budget too small to cover reasoning tokens', () => {
  const base = {
    providerBaseUrl: 'http://192.168.1.214:4000/v1',
    providerWireApi: 'responses',
    providerStreamIdleTimeoutMs: 60000,
    litellmBaseUrl: 'http://192.168.1.214:4000/v1',
    model: 'muse',
    mcpEnabled: true,
    mcpCommand: '/opt/homebrew/bin/node',
    mcpArgs: ['/tmp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpStartupTimeoutSec: 10,
    mcpToolTimeoutSec: 60,
    mcpEnabledTools: ['local_extract', 'local_classify', 'local_review_diff'],
    workerTimeoutMs: 60000,
    adapterPath: '/tmp/local-inference-mcp.js',
    agentName: 'local-muse-worker',
    agentModel: 'muse',
    agentProvider: 'litellm_muse',
  };
  // 1200 fit the answer but not the reasoning that precedes it, which the
  // model emits first — the response came back empty.
  assert.match(
    validateConfiguration({ ...base, workerMaxOutputTokens: 1200 }, 'any').join('\n'),
    /output budget must be at least 2000/,
  );
  assert.deepEqual(validateConfiguration({ ...base, workerMaxOutputTokens: 4000 }, 'any'), []);
  // Unset means the adapter's own default applies, which is large enough.
  assert.deepEqual(validateConfiguration({ ...base, workerMaxOutputTokens: 0 }, 'any'), []);
});

test('scopes local-worker detection to the agent selector, not the prompt', () => {
  // A frontier agent whose prompt merely mentions muse must stay unblocked —
  // denying it would remove the fallback exactly when the local model is down.
  assert.equal(
    isLocalAgentInput(agentSelector({
      tool_name: 'Agent',
      tool_input: { subagent_type: 'code-reviewer', prompt: 'Review the muse integration docs' },
    })),
    false,
  );
  assert.equal(
    isLocalAgentInput(agentSelector({
      tool_name: 'Agent',
      tool_input: { subagent_type: 'local-muse-worker', prompt: 'classify these lines' },
    })),
    true,
  );
  assert.equal(agentSelector({ tool_input: { prompt: 'no agent named here' } }), '');
  assert.equal(isLocalAgentInput(agentSelector({})), false);
});

test('checks that LiteLLM advertises the configured model without inference', async () => {
  const result = await checkLiteLLM({
    litellmBaseUrl: 'http://cluster.example/v1',
    model: 'muse',
    mcpEnv: {},
  }, {
    timeoutMs: DEFAULTS.providerTimeoutMs,
    fetchImpl: async () => new Response(JSON.stringify({
      data: [{ id: 'local' }, { id: 'muse' }],
    }), { status: 200 }),
  });
  assert.equal(result.ok, true);
  assert.equal(result.model, 'muse');
});

test('recognizes local agent requests and produces a blocking hook result', () => {
  assert.equal(isLocalAgentInput({ agent_type: 'local-muse-worker' }), true);
  assert.equal(isLocalAgentInput({ prompt: 'Use the frontier reviewer' }), false);
  const response = hookResponse('PreToolUse', {
    ok: false,
    elapsedMs: 7,
    config: { model: 'muse', mcpToolTimeoutSec: 60 },
    errors: ['LiteLLM did not advertise muse'],
  }, { block: true });
  assert.equal(response.hookSpecificOutput.permissionDecision, 'deny');
  assert.match(response.hookSpecificOutput.permissionDecisionReason, /frontier worker/);
  assert.match(summarize({
    ok: true,
    elapsedMs: 4,
    config: { model: 'muse', mcpToolTimeoutSec: 60 },
    errors: [],
  }), /AVAILABLE/);
});
