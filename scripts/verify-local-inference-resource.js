#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { StdioClientTransport } = require('@modelcontextprotocol/sdk/client/stdio.js');

const RESOURCE_NAME = 'local-muse-worker';
const EXPECTED_MODEL = 'muse';
const EXPECTED_TOOLS = ['local_extract', 'local_classify', 'local_review_diff'];
// Mirrors DEFAULT_MAX_OUTPUT_TOKENS in mcp/local-inference-mcp.js: the budget
// the adapter uses when the env var is unset.
const ADAPTER_DEFAULT_OUTPUT_TOKENS = 4000;
const DEFAULTS = Object.freeze({
  providerTimeoutMs: 2500,
  mcpTimeoutMs: 4500,
  overallTimeoutMs: 7000,
  cacheTtlMs: 10000,
  maxStartupTimeoutSec: 10,
  maxToolTimeoutSec: 60,
  maxWorkerTimeoutMs: 60000,
  // A reasoning model spends this budget on reasoning before answering, so a
  // cap sized for the answer alone yields empty responses on real workloads.
  minWorkerOutputTokens: 2000,
});

function stripTomlComment(line) {
  let quoted = false;
  let escaped = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quoted) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') quoted = false;
      continue;
    }
    if (character === '"') quoted = true;
    else if (character === '#') return line.slice(0, index);
  }
  return line;
}

function splitTomlArray(raw) {
  const values = [];
  let start = 0;
  let depth = 0;
  let quoted = false;
  let escaped = false;
  for (let index = 0; index < raw.length; index += 1) {
    const character = raw[index];
    if (quoted) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') quoted = false;
      continue;
    }
    if (character === '"') quoted = true;
    else if (character === '[') depth += 1;
    else if (character === ']') depth -= 1;
    else if (character === ',' && depth === 0) {
      values.push(raw.slice(start, index).trim());
      start = index + 1;
    }
  }
  if (raw.slice(start).trim()) values.push(raw.slice(start).trim());
  return values;
}

function parseTomlValue(raw) {
  const value = raw.trim();
  if (value.startsWith('"') && value.endsWith('"')) return JSON.parse(value);
  if (value.startsWith('[') && value.endsWith(']')) {
    return splitTomlArray(value.slice(1, -1)).map(parseTomlValue);
  }
  if (value === 'true') return true;
  if (value === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
  return value;
}

function parseTomlSections(text) {
  const sections = new Map([['', {}]]);
  let section = '';
  let multiline = false;

  for (const originalLine of text.split(/\r?\n/)) {
    const line = stripTomlComment(originalLine).trim();
    if (multiline) {
      if (line.includes('"""')) multiline = false;
      continue;
    }
    if (!line) continue;
    const sectionMatch = line.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      section = sectionMatch[1];
      if (!sections.has(section)) sections.set(section, {});
      continue;
    }
    const assignment = line.match(/^([A-Za-z0-9_-]+)\s*=\s*(.+)$/);
    if (!assignment) continue;
    const [, key, rawValue] = assignment;
    if (rawValue.includes('"""')) {
      if ((rawValue.match(/"""/g) || []).length % 2 !== 0) multiline = true;
      continue;
    }
    try {
      sections.get(section)[key] = parseTomlValue(rawValue);
    } catch {
      sections.get(section)[key] = undefined;
    }
  }
  return sections;
}

function normalizeUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  return value.trim().replace(/\/+$/, '');
}

function readFileIfPresent(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf8');
  } catch {
    return null;
  }
}

function resolveResourceConfig({ codexHome = process.env.CODEX_HOME || path.join(os.homedir(), '.codex') } = {}) {
  const configPath = path.join(codexHome, 'config.toml');
  const agentPath = path.join(codexHome, 'agents', `${RESOURCE_NAME}.toml`);
  const configText = readFileIfPresent(configPath);
  const agentText = readFileIfPresent(agentPath);
  const sections = configText ? parseTomlSections(configText) : new Map();
  const agentSections = agentText ? parseTomlSections(agentText) : new Map();
  const provider = sections.get('model_providers.litellm_muse') || {};
  const mcp = sections.get('mcp_servers.local_inference') || {};
  const mcpEnv = sections.get('mcp_servers.local_inference.env') || {};
  const agent = agentSections.get('') || {};
  const mcpArgs = Array.isArray(mcp.args) ? mcp.args : [];
  const adapterPath = mcpArgs.find((argument) => String(argument).endsWith('local-inference-mcp.js')) || '';

  return {
    codexHome,
    configPath,
    agentPath,
    providerBaseUrl: normalizeUrl(provider.base_url),
    providerWireApi: provider.wire_api,
    providerStreamIdleTimeoutMs: Number(provider.stream_idle_timeout_ms || 0),
    mcpEnabled: mcp.enabled !== false,
    mcpCommand: mcp.command || '',
    mcpArgs,
    mcpCwd: mcp.cwd || '',
    mcpStartupTimeoutSec: Number(mcp.startup_timeout_sec || 0),
    mcpToolTimeoutSec: Number(mcp.tool_timeout_sec || 0),
    mcpEnabledTools: Array.isArray(mcp.enabled_tools) ? mcp.enabled_tools : [],
    mcpEnv: mcpEnv,
    litellmBaseUrl: normalizeUrl(mcpEnv.LITELLM_BASE_URL),
    model: mcpEnv.LITELLM_MODEL || '',
    workerTimeoutMs: Number(mcpEnv.LOCAL_WORKER_TIMEOUT_MS || 0),
    workerMaxOutputTokens: Number(mcpEnv.LOCAL_WORKER_MAX_OUTPUT_TOKENS || 0),
    adapterPath,
    agentName: agent.name || '',
    agentModel: agent.model || '',
    agentProvider: agent.model_provider || '',
  };
}

function validateConfiguration(config, target, limits = DEFAULTS) {
  const errors = [];
  const add = (condition, message) => {
    if (!condition) errors.push(message);
  };
  const needsProvider = target === 'any' || target === 'mcp' || target === 'agent';
  const needsMcp = target === 'any' || target === 'mcp' || target === 'agent';
  const needsAgent = target === 'any' || target === 'agent';

  add(Boolean(config.providerBaseUrl), 'Codex provider litellm_muse has no base_url');
  add(config.providerWireApi === 'responses', 'Codex provider litellm_muse must use wire_api=responses');
  add(Boolean(config.litellmBaseUrl), 'MCP worker has no LITELLM_BASE_URL');
  add(config.model === EXPECTED_MODEL, `MCP worker model must be ${EXPECTED_MODEL}`);
  add(normalizeUrl(config.providerBaseUrl) === normalizeUrl(config.litellmBaseUrl), 'provider and MCP LiteLLM URLs differ');

  if (needsMcp) {
    add(config.mcpEnabled, 'local_inference MCP server is disabled');
    add(Boolean(config.mcpCommand), 'local_inference MCP server has no command');
    add(Boolean(config.adapterPath), 'local_inference MCP server does not reference local-inference-mcp.js');
    add(Boolean(config.mcpCwd), 'local_inference MCP server has no cwd');
    add(config.mcpEnabledTools.length === EXPECTED_TOOLS.length
      && EXPECTED_TOOLS.every((tool) => config.mcpEnabledTools.includes(tool)),
    'local_inference MCP server does not expose the expected bounded tools');
    add(config.mcpStartupTimeoutSec > 0 && config.mcpStartupTimeoutSec <= limits.maxStartupTimeoutSec,
      `MCP startup timeout must be between 1 and ${limits.maxStartupTimeoutSec}s`);
    add(config.mcpToolTimeoutSec > 0 && config.mcpToolTimeoutSec <= limits.maxToolTimeoutSec,
      `MCP tool timeout must be between 1 and ${limits.maxToolTimeoutSec}s`);
    add(config.workerTimeoutMs > 0 && config.workerTimeoutMs <= limits.maxWorkerTimeoutMs,
      `local worker timeout must be between 1 and ${limits.maxWorkerTimeoutMs}ms`);
    add((config.workerMaxOutputTokens || ADAPTER_DEFAULT_OUTPUT_TOKENS) >= limits.minWorkerOutputTokens,
      `local worker output budget must be at least ${limits.minWorkerOutputTokens} tokens to cover reasoning`);
  }

  if (needsAgent) {
    add(config.agentName === RESOURCE_NAME, `custom agent ${RESOURCE_NAME} is missing`);
    add(config.agentModel === EXPECTED_MODEL, `custom agent ${RESOURCE_NAME} must use ${EXPECTED_MODEL}`);
    add(config.agentProvider === 'litellm_muse', `custom agent ${RESOURCE_NAME} must use litellm_muse`);
  }

  if (needsProvider) {
    add(config.providerStreamIdleTimeoutMs > 0 && config.providerStreamIdleTimeoutMs <= limits.maxWorkerTimeoutMs,
      `provider stream timeout must be between 1 and ${limits.maxWorkerTimeoutMs}ms`);
  }
  return errors;
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    timer.unref?.();
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function fetchJson(url, { timeoutMs, apiKey = '', fetchImpl = globalThis.fetch } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  timer.unref?.();
  try {
    const headers = { accept: 'application/json' };
    if (apiKey) headers.authorization = `Bearer ${apiKey}`;
    const response = await fetchImpl(url, { headers, signal: controller.signal });
    const body = await response.json().catch(() => null);
    return { response, body };
  } finally {
    clearTimeout(timer);
  }
}

async function checkLiteLLM(config, { timeoutMs = DEFAULTS.providerTimeoutMs, fetchImpl = globalThis.fetch } = {}) {
  const startedAt = Date.now();
  if (!config.litellmBaseUrl || !config.model) {
    return { ok: false, elapsedMs: Date.now() - startedAt, error: 'LiteLLM URL or model is not configured' };
  }
  const endpoint = `${config.litellmBaseUrl}/models`;
  try {
    const { response, body } = await fetchJson(endpoint, {
      timeoutMs,
      apiKey: config.mcpEnv.LITELLM_API_KEY || '',
      fetchImpl,
    });
    const models = Array.isArray(body?.data) ? body.data : Array.isArray(body?.models) ? body.models : [];
    const modelIds = models.map((item) => typeof item === 'string' ? item : item?.id).filter(Boolean);
    const ok = response.ok && modelIds.includes(config.model);
    return {
      ok,
      endpoint,
      status: response.status,
      model: config.model,
      modelCount: modelIds.length,
      elapsedMs: Date.now() - startedAt,
      error: ok ? undefined : `LiteLLM did not advertise ${config.model}`,
    };
  } catch (error) {
    return { ok: false, endpoint, elapsedMs: Date.now() - startedAt, error: error.message };
  }
}

async function checkMcp(config, { timeoutMs = DEFAULTS.mcpTimeoutMs, clientClass = Client, transportClass = StdioClientTransport } = {}) {
  const startedAt = Date.now();
  if (!config.mcpCommand || !config.adapterPath) {
    return { ok: false, elapsedMs: Date.now() - startedAt, error: 'MCP command or adapter is not configured' };
  }
  const client = new clientClass({ name: 'codex-local-resource-check', version: '1.0.0' });
  const transport = new transportClass({
    command: config.mcpCommand,
    args: config.mcpArgs,
    cwd: config.mcpCwd,
    env: {
      ...config.mcpEnv,
      ...(process.env.LITELLM_API_KEY ? { LITELLM_API_KEY: process.env.LITELLM_API_KEY } : {}),
    },
    stderr: 'pipe',
  });
  try {
    await withTimeout(client.connect(transport), timeoutMs, 'MCP initialization timed out');
    const listed = await withTimeout(client.listTools(), timeoutMs, 'MCP tools/list timed out');
    const toolNames = (listed.tools || []).map((tool) => tool.name);
    const missingTools = EXPECTED_TOOLS.filter((tool) => !toolNames.includes(tool));
    const ok = missingTools.length === 0;
    return {
      ok,
      elapsedMs: Date.now() - startedAt,
      toolNames,
      error: ok ? undefined : `MCP worker is missing ${missingTools.join(', ')}`,
    };
  } catch (error) {
    return { ok: false, elapsedMs: Date.now() - startedAt, error: error.message };
  } finally {
    const pid = transport.pid;
    const closePromise = client.close().catch(() => {});
    await Promise.race([
      closePromise,
      new Promise((resolve) => {
        const timer = setTimeout(resolve, 750);
        timer.unref?.();
      }),
    ]);
    if (pid) {
      try {
        process.kill(pid, 'SIGTERM');
      } catch {}
    }
  }
}

function cachePath() {
  return process.env.LOCAL_RESOURCE_CACHE_PATH
    || path.join(os.tmpdir(), 'codex-local-inference-resource.json');
}

function configSignature(config) {
  return JSON.stringify({
    providerBaseUrl: config.providerBaseUrl,
    litellmBaseUrl: config.litellmBaseUrl,
    model: config.model,
    mcpCommand: config.mcpCommand,
    mcpArgs: config.mcpArgs,
    mcpCwd: config.mcpCwd,
    mcpToolTimeoutSec: config.mcpToolTimeoutSec,
    workerTimeoutMs: config.workerTimeoutMs,
    agentModel: config.agentModel,
    agentProvider: config.agentProvider,
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

async function verifyLocalInferenceResource({
  target = 'any',
  codexHome,
  cache = true,
  limits = DEFAULTS,
  fetchImpl = globalThis.fetch,
  mcpProbe = checkMcp,
} = {}) {
  const startedAt = Date.now();
  const config = resolveResourceConfig({ codexHome });
  const signature = `${target}:${configSignature(config)}`;
  if (cache) {
    const cached = await readCache(signature, limits.cacheTtlMs);
    if (cached) return cached;
  }

  const configurationErrors = validateConfiguration(config, target, limits);
  const providerPromise = checkLiteLLM(config, { timeoutMs: limits.providerTimeoutMs, fetchImpl });
  const mcpPromise = target === 'any' || target === 'mcp' || target === 'agent'
    ? mcpProbe(config, { timeoutMs: limits.mcpTimeoutMs })
    : Promise.resolve({ ok: true, skipped: true, elapsedMs: 0 });
  const [provider, mcp] = await withTimeout(
    Promise.all([providerPromise, mcpPromise]),
    limits.overallTimeoutMs,
    'local resource health check timed out',
  ).catch((error) => [
    { ok: false, error: error.message, elapsedMs: Date.now() - startedAt },
    { ok: false, error: error.message, elapsedMs: Date.now() - startedAt },
  ]);

  const errors = [
    ...configurationErrors,
    ...(provider.ok ? [] : [provider.error || 'LiteLLM provider health check failed']),
    ...(mcp.ok ? [] : [mcp.error || 'MCP worker health check failed']),
  ];
  const result = {
    ok: errors.length === 0,
    target,
    checkedAt: new Date().toISOString(),
    elapsedMs: Date.now() - startedAt,
    config: {
      model: config.model,
      providerBaseUrl: config.providerBaseUrl,
      litellmBaseUrl: config.litellmBaseUrl,
      mcpToolTimeoutSec: config.mcpToolTimeoutSec,
      workerTimeoutMs: config.workerTimeoutMs,
      agentName: config.agentName,
      agentModel: config.agentModel,
      agentProvider: config.agentProvider,
    },
    provider,
    mcp,
    errors,
  };
  if (cache) await writeCache(signature, result);
  return result;
}

function isLocalAgentInput(toolInput) {
  const serialized = JSON.stringify(toolInput || '').toLowerCase();
  return /(^|[^a-z0-9])(local-muse-worker|local_muse_worker|litellm_muse|muse)([^a-z0-9]|$)/.test(serialized);
}

// Only the field naming the agent decides whether a spawn targets the local
// worker. Matching against the whole tool input swept in the prompt, so any
// task that merely mentioned muse was denied when the local resource was
// down — blocking the frontier fallback exactly when it was needed.
const AGENT_SELECTOR_KEYS = ['subagent_type', 'agent_type', 'agent_role', 'agent'];

function agentSelector(input) {
  const source = { ...(input || {}), ...((input && input.tool_input) || {}) };
  for (const key of AGENT_SELECTOR_KEYS) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return '';
}

function summarize(result) {
  const state = result.ok ? 'AVAILABLE' : 'UNAVAILABLE';
  const errors = result.errors.length ? `; reason=${result.errors.slice(0, 2).join(' | ')}` : '';
  const timeout = result.config?.mcpToolTimeoutSec ? `; max_task_timeout=${result.config.mcpToolTimeoutSec}s` : '';
  return `Local inference resource ${state}: model=${result.config?.model || EXPECTED_MODEL}; health=${result.elapsedMs}ms${timeout}${errors}`;
}

function hookResponse(eventName, result, { block = false } = {}) {
  const message = summarize(result);
  if (eventName === 'PreToolUse' && block) {
    return {
      hookSpecificOutput: {
        hookEventName: eventName,
        permissionDecision: 'deny',
        permissionDecisionReason: `${message}. Keep this task on a frontier worker; do not retry the unavailable local resource in this turn.`,
      },
    };
  }
  if (eventName === 'PreToolUse') {
    return { systemMessage: message, hookSpecificOutput: { hookEventName: eventName } };
  }
  if (eventName === 'SubagentStart') {
    return {
      systemMessage: message,
      hookSpecificOutput: {
        hookEventName: eventName,
        additionalContext: `${message}. Use the local worker only for bounded read-only work; escalate final judgment to the parent.`,
      },
    };
  }
  return {
    systemMessage: message,
    hookSpecificOutput: {
      hookEventName: eventName,
      additionalContext: `${message}. If unavailable, do not assign local-muse-worker or call local_inference; keep the task on a frontier worker.`,
    },
  };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

async function runHook(eventName) {
  const input = await readStdin();
  if (eventName === 'PreToolUse') {
    const toolName = input.tool_name || '';
    if (toolName === 'Agent' || toolName === 'spawn_agent') {
      if (!isLocalAgentInput(agentSelector(input))) return;
    } else if (!toolName.startsWith('mcp__local_inference__')) {
      return;
    }
  }
  if (eventName === 'SubagentStart' && !isLocalAgentInput(input.agent_type || input.agent_role)) return;

  const result = await verifyLocalInferenceResource({
    target: eventName === 'SessionStart' ? 'any' : eventName === 'SubagentStart' ? 'agent' : 'any',
  }).catch((error) => ({
    ok: false,
    elapsedMs: 0,
    config: { model: EXPECTED_MODEL },
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
  const targetArgument = process.argv.find((argument) => argument.startsWith('--target='));
  const target = targetArgument ? targetArgument.slice('--target='.length) : 'any';
  const result = await verifyLocalInferenceResource({ target, cache: !process.argv.includes('--no-cache') });
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
  EXPECTED_MODEL,
  EXPECTED_TOOLS,
  RESOURCE_NAME,
  agentSelector,
  checkLiteLLM,
  checkMcp,
  hookResponse,
  isLocalAgentInput,
  parseTomlSections,
  resolveResourceConfig,
  summarize,
  validateConfiguration,
  verifyLocalInferenceResource,
};
