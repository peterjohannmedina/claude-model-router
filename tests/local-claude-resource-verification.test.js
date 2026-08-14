'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const {
  DEFAULTS,
  agentSelector,
  isLocalMcpTool,
  parseFrontmatter,
  resolveClaudeResourceConfig,
  validateConfiguration,
  verifyLocalInferenceResource,
} = require('../scripts/verify-local-inference-claude-resource.js');

const projectDir = require('node:path').resolve(__dirname, '..');

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
  const config = resolveClaudeResourceConfig({ projectDir });
  assert.equal(config.model, 'muse');
  assert.equal(config.litellmBaseUrl, 'http://192.168.1.214:4000/v1');
  assert.equal(config.mcpRequestTimeoutMs, 60000);
  assert.equal(config.workerTimeoutMs, 60000);
  assert.equal(config.agentName, 'local-muse-worker');
  assert.equal(config.agentModel, 'haiku');
  assert.deepEqual(validateConfiguration(config), []);
});

test('rejects a Claude worker configuration with excessive timeouts', () => {
  const errors = validateConfiguration({
    litellmBaseUrl: 'http://192.168.1.214:4000/v1',
    mcpEnv: { LITELLM_BASE_URL: 'http://192.168.1.214:4000/v1' },
    model: 'muse',
    mcpCommand: 'node',
    mcpArgs: ['/tmp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpRequestTimeoutMs: 120000,
    workerTimeoutMs: 120000,
    adapterPath: '/tmp/local-inference-mcp.js',
    agentName: 'local-muse-worker',
    agentModel: 'haiku',
    agentTools: [
      'mcp__local-inference__local_extract',
      'mcp__local-inference__local_classify',
      'mcp__local-inference__local_review_diff',
    ],
  });
  assert.match(errors.join('\n'), /request timeout/);
  assert.match(errors.join('\n'), /worker timeout/);
});

test('rejects a Claude worker output budget too small to cover reasoning', () => {
  const base = {
    litellmBaseUrl: 'http://192.168.1.214:4000/v1',
    mcpEnv: { LITELLM_BASE_URL: 'http://192.168.1.214:4000/v1' },
    model: 'muse',
    mcpCommand: 'node',
    mcpArgs: ['/tmp/local-inference-mcp.js'],
    mcpCwd: '/tmp',
    mcpRequestTimeoutMs: 60000,
    workerTimeoutMs: 60000,
    adapterPath: '/tmp/local-inference-mcp.js',
    agentName: 'local-muse-worker',
    agentModel: 'haiku',
    agentTools: [
      'mcp__local-inference__local_extract',
      'mcp__local-inference__local_classify',
      'mcp__local-inference__local_review_diff',
    ],
  };
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

test('verifies the Claude route without generating inference', async () => {
  const result = await verifyLocalInferenceResource({
    projectDir,
    cache: false,
    limits: DEFAULTS,
    fetchImpl: async () => new Response(JSON.stringify({
      data: [{ id: 'muse' }],
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
