'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const {
  DEFAULTS,
  agentSelector,
  checkLiteLLM,
  checkMcp,
  hookResponse,
  isLocalAgentInput,
  parseTomlSections,
  summarize,
  validateConfiguration,
  verifyLocalInferenceResource,
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
    mcpCommand: process.execPath,
    mcpArgs: ['/tmp/mcp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpStartupTimeoutSec: 20,
    mcpToolTimeoutSec: 120,
    mcpEnabledTools: ['local_extract', 'local_classify', 'local_review_diff'],
    workerTimeoutMs: 120000,
    adapterPath: '/tmp/mcp/local-inference-mcp.js',
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
    mcpCommand: process.execPath,
    mcpArgs: ['/tmp/mcp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpStartupTimeoutSec: 10,
    mcpToolTimeoutSec: 60,
    mcpEnabledTools: ['local_extract', 'local_classify', 'local_review_diff'],
    workerTimeoutMs: 60000,
    adapterPath: '/tmp/mcp/local-inference-mcp.js',
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

test('rejects MCP preload arguments and untrusted Node executable paths', () => {
  const base = {
    providerBaseUrl: 'http://192.168.1.214:4000/v1', providerWireApi: 'responses',
    providerStreamIdleTimeoutMs: 60000, litellmBaseUrl: 'http://192.168.1.214:4000/v1',
    model: 'muse', mcpEnabled: true, mcpCommand: process.execPath,
    mcpArgs: ['/tmp/mcp/local-inference-mcp.js'], mcpCwd: '/tmp',
    mcpStartupTimeoutSec: 10, mcpToolTimeoutSec: 60,
    mcpEnabledTools: ['local_extract', 'local_classify', 'local_review_diff'],
    workerTimeoutMs: 60000, adapterPath: '/tmp/mcp/local-inference-mcp.js',
    agentName: 'local-muse-worker', agentModel: 'muse', agentProvider: 'litellm_muse',
  };
  assert.match(validateConfiguration({
    ...base,
    mcpArgs: ['--require', 'evil.js', '/tmp/mcp/local-inference-mcp.js'],
  }, 'any').join('\n'), /only argument/);
  assert.match(validateConfiguration({
    ...base,
    mcpCommand: '/tmp/evil/node.exe',
  }, 'any').join('\n'), /current Node\.js executable/);
});

test('MCP probe uses the current Node executable and preserves an explicit configured key', async () => {
  let transportOptions;
  class FakeTransport {
    constructor(options) { transportOptions = options; }
  }
  class FakeClient {
    async connect() {}
    async listTools() {
      return { tools: [{ name: 'local_extract' }, { name: 'local_classify' }, { name: 'local_review_diff' }] };
    }
    async close() {}
  }
  const result = await checkMcp({
    mcpCommand: 'node', mcpArgs: ['/tmp/mcp/local-inference-mcp.js'], mcpCwd: '/tmp',
    adapterPath: '/tmp/mcp/local-inference-mcp.js',
    mcpEnv: { LITELLM_API_KEY: 'configured-key' },
  }, {
    clientClass: FakeClient,
    transportClass: FakeTransport,
    inheritedEnv: { LITELLM_API_KEY: 'parent-key' },
  });
  assert.equal(result.ok, true);
  assert.equal(transportOptions.command, process.execPath);
  assert.equal(transportOptions.env.LITELLM_API_KEY, 'configured-key');
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
  let authorization;
  const result = await checkLiteLLM({
    litellmBaseUrl: 'http://cluster.example/v1',
    model: 'muse',
    mcpEnv: {},
  }, {
    timeoutMs: DEFAULTS.providerTimeoutMs,
    apiKey: 'inherited-test-key',
    fetchImpl: async (url, options) => {
      authorization = options.headers.authorization;
      return new Response(JSON.stringify({
        data: [{ id: 'local' }, { id: 'muse' }],
      }), { status: 200 });
    },
  });
  assert.equal(result.ok, true);
  assert.equal(result.model, 'muse');
  assert.equal(authorization, 'Bearer inherited-test-key');
});

test('invalid Codex configuration is rejected before network or process probes', async () => {
  let fetchCalls = 0;
  let mcpCalls = 0;
  const result = await verifyLocalInferenceResource({
    codexHome: `${__dirname}/missing-codex-home`,
    cache: false,
    fetchImpl: async () => {
      fetchCalls += 1;
      throw new Error('must not fetch');
    },
    mcpProbe: async () => {
      mcpCalls += 1;
      throw new Error('must not spawn');
    },
  });
  assert.equal(result.ok, false);
  assert.equal(fetchCalls, 0);
  assert.equal(mcpCalls, 0);
  assert.equal(result.provider.skipped, true);
  assert.equal(result.mcp.skipped, true);
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
