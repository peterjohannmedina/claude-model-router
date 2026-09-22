#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const baseVerifier = require('./verify-local-inference-resource.js');

const {
  agentSelector,
  checkLiteLLM,
  checkMcp,
  hookResponse,
  isLocalAgentInput,
  isSafeNodeCommand,
  summarize,
} = baseVerifier;

const RESOURCE_NAME = 'local-muse-worker';
const MCP_SERVER_NAME = 'local-inference';
const EXPECTED_TOOLS = baseVerifier.EXPECTED_TOOLS;
const ADAPTER_DEFAULT_OUTPUT_TOKENS = 4000;
const DEFAULTS = Object.freeze({
  providerTimeoutMs: 2500,
  mcpTimeoutMs: 4500,
  overallTimeoutMs: 7000,
  cacheTtlMs: 10000,
  maxRequestTimeoutMs: 60000,
  maxWorkerTimeoutMs: 60000,
  minWorkerOutputTokens: 2000,
});

function readFileIfPresent(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8');
  } catch {
    return null;
  }
}

function readJsonIfPresent(filePath) {
  const text = readFileIfPresent(filePath);
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function normalizeUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  return value.trim().replace(/\/+$/, '');
}

function expandValue(value, projectDir, env = process.env) {
  if (typeof value !== 'string') return value;
  return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g, (match, name, fallback) => {
    if (Object.prototype.hasOwnProperty.call(env, name)) return env[name];
    return fallback === undefined ? '' : fallback;
  });
}

function parseFrontmatter(text) {
  const match = String(text || '').trimStart().match(/^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
  if (!match) return {};

  const fields = {};
  let activeList = null;
  for (const line of match[1].split(/\r?\n/)) {
    const assignment = line.match(/^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/);
    if (assignment) {
      const [, key, rawValue] = assignment;
      if (!rawValue) {
        fields[key] = [];
        activeList = key;
      } else {
        fields[key] = rawValue.replace(/^['"]|['"]$/g, '');
        activeList = null;
      }
      continue;
    }
    const item = line.match(/^\s+-\s+(.+)$/);
    if (item && activeList) fields[activeList].push(item[1].replace(/^['"]|['"]$/g, ''));
  }
  return fields;
}

function resolveClaudeResourceConfig({
  projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd(),
  mcpPath = path.join(projectDir, '.mcp.json'),
  agentPath = path.join(projectDir, '.claude', 'agents', `${RESOURCE_NAME}.md`),
  env = process.env,
} = {}) {
  const resolvedProjectDir = path.resolve(projectDir);
  const resolvedMcpPath = path.resolve(mcpPath);
  const resolvedAgentPath = path.resolve(agentPath);
  const document = readJsonIfPresent(resolvedMcpPath) || {};
  const servers = document.mcpServers || document;
  const server = servers[MCP_SERVER_NAME] || {};
  const rawEnv = server.env && typeof server.env === 'object' ? server.env : {};
  const mcpEnv = Object.fromEntries(
    Object.entries(rawEnv)
      .map(([key, value]) => [key, expandValue(value, resolvedProjectDir, env)])
      .filter(([, value]) => value !== undefined && value !== null && value !== ''),
  );
  for (const name of [
    'LITELLM_BASE_URL',
    'LITELLM_BASE',
    'LITELLM_API_KEY',
    'LITELLM_MODEL',
    'LOCAL_WORKER_MAX_OUTPUT_TOKENS',
    'LOCAL_WORKER_TIMEOUT_MS',
    'LOCAL_WORKER_TEMPERATURE',
  ]) {
    if (!(name in mcpEnv) && env[name] !== undefined && env[name] !== '') mcpEnv[name] = env[name];
  }
  const mcpArgs = Array.isArray(server.args)
    ? server.args.map((argument) => expandValue(String(argument), resolvedProjectDir, env))
    : [];
  const agentText = readFileIfPresent(resolvedAgentPath);
  const agent = parseFrontmatter(agentText);
  const toolTimeoutMs = Number(server.timeout || 0);

  return {
    projectDir: resolvedProjectDir,
    mcpPath: resolvedMcpPath,
    agentPath: resolvedAgentPath,
    mcpEnabled: Boolean(server.command),
    mcpType: server.type || 'stdio',
    mcpCommand: expandValue(server.command || '', resolvedProjectDir, env),
    mcpArgs,
    mcpCwd: expandValue(server.cwd || resolvedProjectDir, resolvedProjectDir, env),
    mcpToolTimeoutMs: toolTimeoutMs,
    mcpToolTimeoutSec: toolTimeoutMs / 1000,
    mcpEnv,
    litellmBaseUrl: normalizeUrl(mcpEnv.LITELLM_BASE_URL || mcpEnv.LITELLM_BASE),
    model: mcpEnv.LITELLM_MODEL || '',
    workerTimeoutMs: Number(mcpEnv.LOCAL_WORKER_TIMEOUT_MS || 0),
    workerMaxOutputTokens: Number(mcpEnv.LOCAL_WORKER_MAX_OUTPUT_TOKENS || 0),
    adapterPath: mcpArgs.find((argument) => String(argument).endsWith('local-inference-mcp.js')) || '',
    agentName: agent.name || '',
    agentModel: agent.model || '',
    agentTools: Array.isArray(agent.tools) ? agent.tools : [],
  };
}

function validateConfiguration(config, limits = DEFAULTS) {
  const errors = [];
  const add = (condition, message) => {
    if (!condition) errors.push(message);
  };
  add(Boolean(config.litellmBaseUrl), 'Claude local-inference MCP has no LITELLM_BASE_URL');
  add(Boolean(config.model), 'Claude local-inference MCP has no LITELLM_MODEL');
  add(config.mcpType === 'stdio', 'Claude local-inference MCP server must use stdio');
  add(Boolean(config.mcpCommand), 'Claude local-inference MCP server has no command');
  add(isSafeNodeCommand(config.mcpCommand),
    'Claude local-inference MCP server command must be node, node.exe, or the current Node.js executable');
  const expectedAdapterPath = path.join(config.projectDir, 'mcp', 'local-inference-mcp.js');
  const resolvedAdapterPath = config.adapterPath
    ? path.resolve(config.mcpCwd, config.adapterPath)
    : '';
  add(config.mcpArgs.length === 1 && resolvedAdapterPath === expectedAdapterPath,
    'Claude local-inference MCP server must use the project adapter as its only argument');
  add(Boolean(config.mcpCwd), 'Claude local-inference MCP server has no working directory');
  add(path.resolve(config.mcpCwd || '.') === config.projectDir,
    'Claude local-inference MCP working directory must be the project directory');
  add(config.mcpToolTimeoutMs >= 1000 && config.mcpToolTimeoutMs <= limits.maxRequestTimeoutMs,
    `Claude MCP tool timeout must be between 1000 and ${limits.maxRequestTimeoutMs}ms`);
  add(config.workerTimeoutMs > 0 && config.workerTimeoutMs <= limits.maxWorkerTimeoutMs,
    `local worker timeout must be between 1 and ${limits.maxWorkerTimeoutMs}ms`);
  add((config.workerMaxOutputTokens || ADAPTER_DEFAULT_OUTPUT_TOKENS) >= limits.minWorkerOutputTokens,
    `local worker output budget must be at least ${limits.minWorkerOutputTokens} tokens to cover reasoning`);
  add(config.agentName === RESOURCE_NAME, `Claude agent ${RESOURCE_NAME} is missing`);
  add(config.agentModel === 'haiku', `Claude agent ${RESOURCE_NAME} must use haiku`);
  const expectedAgentTools = EXPECTED_TOOLS.map((tool) => `mcp__${MCP_SERVER_NAME}__${tool}`);
  add(config.agentTools.length === expectedAgentTools.length
    && expectedAgentTools.every((tool) => config.agentTools.includes(tool)),
    `Claude agent ${RESOURCE_NAME} must be restricted to the local inference tools`);
  add(normalizeUrl(config.litellmBaseUrl) === normalizeUrl(
    config.mcpEnv.LITELLM_BASE_URL || config.mcpEnv.LITELLM_BASE,
  ),
    'Claude local-inference LiteLLM URL is inconsistent');
  return errors;
}

function cachePath() {
  return process.env.CLAUDE_LOCAL_RESOURCE_CACHE_PATH
    || path.join(os.tmpdir(), 'claude-local-inference-resource.json');
}

function configSignature(config) {
  return JSON.stringify({
    projectDir: config.projectDir,
    mcpPath: config.mcpPath,
    agentPath: config.agentPath,
    litellmBaseUrl: config.litellmBaseUrl,
    model: config.model,
    mcpCommand: config.mcpCommand,
    mcpType: config.mcpType,
    mcpArgs: config.mcpArgs,
    mcpCwd: config.mcpCwd,
    mcpToolTimeoutMs: config.mcpToolTimeoutMs,
    workerTimeoutMs: config.workerTimeoutMs,
    workerMaxOutputTokens: config.workerMaxOutputTokens,
    agentName: config.agentName,
    agentModel: config.agentModel,
    agentTools: config.agentTools,
  });
}

async function readCache(signature, ttlMs) {
  try {
    const cached = JSON.parse(await fsp.readFile(cachePath(), 'utf8'));
    if (cached.signature === signature && Date.now() - cached.checkedAt < ttlMs) {
      return { ...cached.result, fromCache: true };
    }
  } catch {
    return null;
  }
  return null;
}

async function writeCache(signature, result) {
  const target = cachePath();
  const temporary = `${target}.${process.pid}.tmp`;
  try {
    await fsp.writeFile(temporary, JSON.stringify({ signature, checkedAt: Date.now(), result }));
    await fsp.rename(temporary, target);
  } catch {
    await fsp.unlink(temporary).catch(() => {});
  }
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    timer.unref?.();
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function verifyLocalInferenceResource({
  projectDir,
  env = process.env,
  cache = true,
  limits = DEFAULTS,
  fetchImpl = globalThis.fetch,
  mcpProbe = checkMcp,
} = {}) {
  const startedAt = Date.now();
  const config = resolveClaudeResourceConfig({ projectDir, env });
  const signature = configSignature(config);
  if (cache) {
    const cached = await readCache(signature, limits.cacheTtlMs);
    if (cached) return cached;
  }

  const configurationErrors = validateConfiguration(config, limits);
  if (configurationErrors.length) {
    const result = makeResult(config, startedAt, configurationErrors, {
      ok: false,
      skipped: true,
      error: 'Provider probe skipped because configuration is invalid',
    }, {
      ok: false,
      skipped: true,
      error: 'MCP probe skipped because configuration is invalid',
    });
    if (cache) await writeCache(signature, result);
    return result;
  }
  const providerPromise = checkLiteLLM(config, {
    timeoutMs: limits.providerTimeoutMs,
    fetchImpl,
  });
  const mcpPromise = mcpProbe(config, { timeoutMs: limits.mcpTimeoutMs });
  const [provider, mcp] = await withTimeout(
    Promise.all([providerPromise, mcpPromise]),
    limits.overallTimeoutMs,
    'Claude local resource health check timed out',
  ).catch((error) => [
    { ok: false, error: error.message, elapsedMs: Date.now() - startedAt },
    { ok: false, error: error.message, elapsedMs: Date.now() - startedAt },
  ]);

  const errors = [
    ...configurationErrors,
    ...(provider.ok ? [] : [provider.error || 'LiteLLM provider health check failed']),
    ...(mcp.ok ? [] : [mcp.error || 'MCP worker health check failed']),
  ];
  const result = makeResult(config, startedAt, errors, provider, mcp);
  if (cache) await writeCache(signature, result);
  return result;
}

function makeResult(config, startedAt, errors, provider, mcp) {
  return {
    ok: errors.length === 0,
    target: 'claude',
    checkedAt: new Date().toISOString(),
    elapsedMs: Date.now() - startedAt,
    config: {
      projectDir: config.projectDir,
      mcpPath: config.mcpPath,
      agentPath: config.agentPath,
      model: config.model,
      providerBaseUrl: config.litellmBaseUrl,
      litellmBaseUrl: config.litellmBaseUrl,
      mcpToolTimeoutMs: config.mcpToolTimeoutMs,
      mcpToolTimeoutSec: config.mcpToolTimeoutSec,
      workerTimeoutMs: config.workerTimeoutMs,
      agentName: config.agentName,
      agentModel: config.agentModel,
      agentTools: config.agentTools,
    },
    provider,
    mcp,
    errors,
  };
}

function isLocalMcpTool(toolName) {
  return /^mcp__local[-_]inference__/.test(toolName || '');
}

function isExpectedMcpToolInput(input) {
  if (!isLocalMcpTool(input?.tool_name)) return false;
  return input.mcp_server?.name === MCP_SERVER_NAME && input.mcp_server?.source === 'project';
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = Buffer.concat(chunks).toString('utf8').trim();
  if (!input) return {};
  try {
    return JSON.parse(input);
  } catch {
    return {};
  }
}

async function runHook(eventName) {
  const input = await readStdin();
  if (eventName === 'PreToolUse') {
    const toolName = input.tool_name || '';
    if (toolName === 'Agent' || toolName === 'Task') {
      if (!isLocalAgentInput(agentSelector(input))) return;
    } else if (!isLocalMcpTool(toolName)) {
      return;
    } else if (!isExpectedMcpToolInput(input)) {
      const result = {
        ok: false,
        elapsedMs: 0,
        config: { model: process.env.LITELLM_MODEL || 'unconfigured', mcpToolTimeoutSec: 0 },
        errors: ['Local inference MCP provenance is not the expected project server'],
      };
      process.stdout.write(`${JSON.stringify(hookResponse(eventName, result, { block: true }))}\n`);
      return;
    }
  }
  if (eventName === 'SubagentStart' && !isLocalAgentInput(input.agent_type || input.agent_role)) return;

  const projectDir = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
  const result = await verifyLocalInferenceResource({
    projectDir,
    cache: true,
  }).catch((error) => ({
    ok: false,
    elapsedMs: 0,
    config: { model: process.env.LITELLM_MODEL || 'unconfigured', mcpToolTimeoutSec: 0 },
    provider: { ok: false },
    mcp: { ok: false },
    errors: [error.message],
  }));
  process.stdout.write(`${JSON.stringify(hookResponse(eventName, result, { block: !result.ok }))}\n`);
}

async function main() {
  const hookArgument = process.argv.find((argument) => argument.startsWith('--hook='));
  if (hookArgument) {
    await runHook(hookArgument.slice('--hook='.length));
    return;
  }
  const projectArgument = process.argv.find((argument) => argument.startsWith('--project-dir='));
  const projectDir = projectArgument ? projectArgument.slice('--project-dir='.length) : undefined;
  const result = await verifyLocalInferenceResource({
    projectDir,
    cache: !process.argv.includes('--no-cache'),
  });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (!result.ok) process.exitCode = 1;
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  DEFAULTS,
  MCP_SERVER_NAME,
  RESOURCE_NAME,
  EXPECTED_TOOLS,
  agentSelector,
  isLocalMcpTool,
  isExpectedMcpToolInput,
  parseFrontmatter,
  resolveClaudeResourceConfig,
  summarize,
  validateConfiguration,
  verifyLocalInferenceResource,
};
