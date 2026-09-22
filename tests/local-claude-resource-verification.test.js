'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const {
  DEFAULTS,
  agentSelector,
  isExpectedMcpToolInput,
  isLocalMcpTool,
  parseFrontmatter,
  resolveClaudeResourceConfig,
  validateConfiguration,
  verifyLocalInferenceResource,
} = require('../scripts/verify-local-inference-claude-resource.js');

const projectDir = require('node:path').resolve(__dirname, '..');
const configuredEnv = {
  LITELLM_BASE_URL: 'http://192.168.1.214:4000/v1',
  LITELLM_MODEL: 'test-local-model',
};

function validConfig(overrides = {}) {
  return {
    projectDir,
    litellmBaseUrl: configuredEnv.LITELLM_BASE_URL,
    mcpEnv: { ...configuredEnv },
    model: configuredEnv.LITELLM_MODEL,
    mcpType: 'stdio',
    mcpCommand: 'node',
    mcpArgs: [`${projectDir}/mcp/local-inference-mcp.js`],
    mcpCwd: projectDir,
    mcpToolTimeoutMs: 60000,
    workerTimeoutMs: 60000,
    adapterPath: `${projectDir}/mcp/local-inference-mcp.js`,
    agentName: 'local-muse-worker',
    agentModel: 'haiku',
    agentTools: [
      'mcp__local-inference__local_extract',
      'mcp__local-inference__local_classify',
      'mcp__local-inference__local_review_diff',
    ],
    ...overrides,
  };
}

test('parses the Claude local worker frontmatter and recognizes its MCP tools', () => {
  const frontmatter = parseFrontmatter(`
---
name: local-muse-worker
model: haiku
tools:
  - mcp__local-inference__local_extract
  - mcp__local-inference__local_classify
maxTurns: 8
---
`);
  assert.equal(frontmatter.name, 'local-muse-worker');
  assert.equal(frontmatter.model, 'haiku');
  assert.deepEqual(frontmatter.tools, [
    'mcp__local-inference__local_extract',
    'mcp__local-inference__local_classify',
  ]);
  assert.equal(isLocalMcpTool('mcp__local-inference__local_extract'), true);
  assert.equal(isLocalMcpTool('mcp__other__local_extract'), false);
});

test('resolves the checked-in Claude MCP and agent configuration', () => {
  const config = resolveClaudeResourceConfig({ projectDir, env: configuredEnv });
  assert.equal(config.model, 'test-local-model');
  assert.equal(config.litellmBaseUrl, 'http://192.168.1.214:4000/v1');
  assert.equal(config.mcpArgs[0], './mcp/local-inference-mcp.js');
  assert.equal(require('node:path').resolve(config.mcpCwd, config.adapterPath),
    require('node:path').join(projectDir, 'mcp', 'local-inference-mcp.js'));
  assert.equal(config.mcpToolTimeoutMs, 60000);
  assert.equal(config.workerTimeoutMs, 60000);
  assert.equal(config.agentName, 'local-muse-worker');
  assert.equal(config.agentModel, 'haiku');
  assert.deepEqual(validateConfiguration(config), []);
});

test('rejects a Claude worker configuration with excessive timeouts', () => {
  const errors = validateConfiguration(validConfig({
    mcpToolTimeoutMs: 120000,
    workerTimeoutMs: 120000,
  }));
  assert.match(errors.join('\n'), /tool timeout/);
  assert.match(errors.join('\n'), /worker timeout/);
});

test('rejects a Claude worker output budget too small to cover reasoning', () => {
  const base = validConfig();
  assert.match(
    validateConfiguration({ ...base, workerMaxOutputTokens: 1200 }).join('\n'),
    /output budget must be at least 2000/,
  );
  assert.deepEqual(validateConfiguration({ ...base, workerMaxOutputTokens: 4000 }), []);
});

test('does not gate frontier subagents whose prompt mentions muse', () => {
  assert.equal(agentSelector({
    tool_name: 'Task',
    tool_input: { subagent_type: 'general-purpose', prompt: 'summarize the muse rollout' },
  }), 'general-purpose');
  assert.equal(agentSelector({
    tool_name: 'Task',
    tool_input: { subagent_type: 'local-muse-worker' },
  }), 'local-muse-worker');
});

test('accepts any configured local model and rejects a missing model', () => {
  assert.deepEqual(validateConfiguration(validConfig({ model: 'another-local-model' })), []);
  assert.match(
    validateConfiguration(validConfig({ model: '' })).join('\n'),
    /has no LITELLM_MODEL/,
  );
});

test('requires exact local-agent tools and expected MCP provenance', () => {
  assert.match(
    validateConfiguration(validConfig({
      agentTools: [...validConfig().agentTools, 'Bash'],
    })).join('\n'),
    /restricted to the local inference tools/,
  );
  assert.equal(isExpectedMcpToolInput({
    tool_name: 'mcp__local-inference__local_extract',
    mcp_server: { name: 'local-inference', source: 'project' },
  }), true);
  assert.equal(isExpectedMcpToolInput({
    tool_name: 'mcp__local-inference__local_extract',
    mcp_server: { name: 'local-inference', source: 'sdk' },
  }), false);
});

test('rejects extra MCP arguments and an untrusted Node executable path', () => {
  assert.match(validateConfiguration(validConfig({
    mcpArgs: ['--require', 'evil.js', `${projectDir}/mcp/local-inference-mcp.js`],
  })).join('\n'), /only argument/);
  assert.match(validateConfiguration(validConfig({
    mcpCommand: `${projectDir}/untrusted/node.exe`,
  })).join('\n'), /current Node\.js executable/);
});

test('invalid Claude configuration is rejected before network or process probes', async () => {
  let fetchCalls = 0;
  let mcpCalls = 0;
  const result = await verifyLocalInferenceResource({
    projectDir,
    env: {},
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

test('verifies the Claude route without generating inference', async () => {
  const result = await verifyLocalInferenceResource({
    projectDir,
    env: configuredEnv,
    cache: false,
    limits: DEFAULTS,
    fetchImpl: async () => new Response(JSON.stringify({
      data: [{ id: 'test-local-model' }],
    }), { status: 200 }),
    mcpProbe: async () => ({
      ok: true,
      elapsedMs: 1,
      toolNames: ['local_extract', 'local_classify', 'local_review_diff'],
    }),
  });
  assert.equal(result.ok, true);
  assert.equal(result.target, 'claude');
  assert.equal(result.config.agentName, 'local-muse-worker');
  assert.deepEqual(result.errors, []);
});
