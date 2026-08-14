'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { StreamableHTTPClientTransport } = require('@modelcontextprotocol/sdk/client/streamableHttp.js');

const {
  LiteLLMClient,
  LocalInferenceError,
  createMcpServer,
  loadConfig,
  parseJsonDocument,
  resolveChatCompletionsUrl,
  startHttpServer,
  validateExtractedValue,
} = require('../mcp/local-inference-mcp.js');

function makeConfig(overrides = {}) {
  return {
    transport: 'stdio',
    host: '127.0.0.1',
    port: 8787,
    mcpBearerToken: '',
    httpJsonResponse: true,
    litellmBaseUrl: 'http://127.0.0.1:4000/v1',
    chatCompletionsUrl: 'http://127.0.0.1:4000/v1/chat/completions',
    apiKey: 'test-key',
    model: 'local-30b',
    maxInputChars: 10000,
    maxOutputTokens: 300,
    timeoutMs: 5000,
    temperature: 0,
    ...overrides,
  };
}

test('resolves LiteLLM chat completions URLs without duplicating /v1', () => {
  assert.equal(
    resolveChatCompletionsUrl('http://cluster.example:4000/v1/'),
    'http://cluster.example:4000/v1/chat/completions',
  );
  assert.equal(
    resolveChatCompletionsUrl('http://cluster.example:4000'),
    'http://cluster.example:4000/v1/chat/completions',
  );
});

test('loadConfig requires a LiteLLM base URL and validates transport', () => {
  assert.throws(
    () => loadConfig({}),
    (error) => error instanceof LocalInferenceError && error.code === 'invalid_configuration',
  );
  const config = loadConfig({
    LITELLM_BASE_URL: 'http://cluster.example:4000/v1',
    LITELLM_MODEL: 'muse-glimmer',
    MCP_TRANSPORT: 'http',
    MCP_PORT: '9000',
    MCP_HTTP_JSON_RESPONSE: 'true',
  });
  assert.equal(config.transport, 'http');
  assert.equal(config.port, 9000);
  assert.equal(config.model, 'muse-glimmer');
  assert.equal(config.chatCompletionsUrl, 'http://cluster.example:4000/v1/chat/completions');
});

test('parseJsonDocument accepts plain, fenced, and embedded JSON', () => {
  assert.deepEqual(parseJsonDocument('{"ok":true}'), { ok: true });
  assert.deepEqual(parseJsonDocument('```json\n{"ok":true}\n```'), { ok: true });
  assert.deepEqual(parseJsonDocument('Result: [1, 2, 3]'), [1, 2, 3]);
  assert.throws(
    () => parseJsonDocument('not json'),
    (error) => error instanceof LocalInferenceError && error.code === 'invalid_model_output',
  );
});

test('validateExtractedValue rejects schema-incompatible local output', () => {
  validateExtractedValue(
    { type: 'object', required: ['answer'], properties: { answer: { type: 'number' } } },
    { answer: 42 },
  );
  assert.throws(
    () => validateExtractedValue(
      { type: 'object', required: ['answer'], properties: { answer: { type: 'number' } } },
      { answer: 'not a number' },
    ),
    (error) => error instanceof LocalInferenceError && error.code === 'invalid_model_output',
  );
});

test('LiteLLMClient sends a bounded chat completion request and normalizes the response', async () => {
  let request;
  const client = new LiteLLMClient(makeConfig(), async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({
      id: 'chatcmpl-test',
      model: 'local-30b@revision-test',
      choices: [{ message: { content: '{"answer":42}' } }],
      usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 },
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  });

  const response = await client.complete({
    traceId: 'local-mcp-test',
    system: 'system',
    user: 'user',
  });
  const body = JSON.parse(request.options.body);
  assert.equal(request.url, 'http://127.0.0.1:4000/v1/chat/completions');
  assert.equal(request.options.headers.authorization, 'Bearer test-key');
  assert.equal(body.model, 'local-30b');
  assert.equal(body.stream, false);
  assert.equal(response.responseModel, 'local-30b@revision-test');
  assert.deepEqual(response.usage, { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 });
});

test('LiteLLMClient names an exhausted output budget instead of a protocol fault', async () => {
  // A reasoning model emits reasoning before its answer, so a budget that runs
  // out mid-reasoning returns empty content with finish_reason=length.
  const starved = new LiteLLMClient(makeConfig(), async () => new Response(JSON.stringify({
    model: 'local-30b',
    choices: [{ message: { content: '', reasoning_content: 'thinking...' }, finish_reason: 'length' }],
    usage: { completion_tokens: 1200 },
  }), { status: 200, headers: { 'content-type': 'application/json' } }));
  await assert.rejects(
    () => starved.complete({ traceId: 't', system: 's', user: 'u' }),
    (error) => error instanceof LocalInferenceError
      && error.code === 'output_budget_exhausted'
      && /LOCAL_WORKER_MAX_OUTPUT_TOKENS/.test(error.message),
  );

  // An empty response for any other reason stays a protocol error.
  const empty = new LiteLLMClient(makeConfig(), async () => new Response(JSON.stringify({
    model: 'local-30b',
    choices: [{ message: { content: '' }, finish_reason: 'stop' }],
  }), { status: 200, headers: { 'content-type': 'application/json' } }));
  await assert.rejects(
    () => empty.complete({ traceId: 't', system: 's', user: 'u' }),
    (error) => error instanceof LocalInferenceError && error.code === 'upstream_protocol_error',
  );
});

test('MCP tools call the local worker and return structured evidence', async () => {
  const config = makeConfig();
  const fakeClient = {
    async complete({ user }) {
      assert.match(user, /Extract a value matching this JSON Schema/);
      return {
        content: '{"language":"JavaScript"}',
        responseModel: 'local-30b@revision-test',
        usage: { total_tokens: 10 },
      };
    },
  };
  const server = createMcpServer(config, fakeClient);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: 'test-client', version: '1.0.0' });
  await Promise.all([
    server.connect(serverTransport),
    client.connect(clientTransport),
  ]);

  const result = await client.callTool({
    name: 'local_extract',
    arguments: {
      text: 'This project uses JavaScript.',
      schema: { type: 'object', properties: { language: { type: 'string' } } },
      source: 'README.md',
    },
  });
  assert.equal(result.isError, undefined);
  assert.deepEqual(result.structuredContent.result, { language: 'JavaScript' });
  assert.equal(result.structuredContent.route, 'local');
  assert.equal(result.structuredContent.response_model, 'local-30b@revision-test');
  assert.equal(result.structuredContent.evidence[0].source, 'README.md');

  await client.close();
  await server.close();
});

test('MCP tools reject oversized input before calling LiteLLM', async () => {
  let calls = 0;
  const config = makeConfig({ maxInputChars: 10 });
  const server = createMcpServer(config, {
    async complete() {
      calls += 1;
      return { content: '{}', responseModel: config.model };
    },
  });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: 'test-client', version: '1.0.0' });
  await Promise.all([
    server.connect(serverTransport),
    client.connect(clientTransport),
  ]);

  const result = await client.callTool({
    name: 'local_extract',
    arguments: {
      text: 'this input is too long',
      schema: { type: 'object' },
    },
  });
  assert.equal(result.isError, true);
  assert.equal(calls, 0);
  assert.match(result.content[0].text, /input_too_large/);

  await client.close();
  await server.close();
});

test('stateless Streamable HTTP serves the same MCP tools', async () => {
  const config = makeConfig({ port: 0 });
  const server = await startHttpServer(config, {
    async complete() {
      return {
        content: '{"items":[{"index":0,"label":"yes","reason":"matched"}]}',
        responseModel: 'local-30b@revision-test',
      };
    },
  });
  const address = server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  const client = new Client({ name: 'http-test-client', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`));
  await client.connect(transport);

  const tools = await client.listTools();
  assert.deepEqual(
    tools.tools.map((tool) => tool.name),
    ['local_extract', 'local_classify', 'local_review_diff'],
  );
  const result = await client.callTool({
    name: 'local_classify',
    arguments: {
      items: ['a change'],
      labels: ['yes', 'no'],
      criteria: 'Use yes when the item matches.',
    },
  });
  assert.equal(result.isError, undefined);
  assert.equal(result.structuredContent.result.items[0].label, 'yes');

  await client.close();
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
});
