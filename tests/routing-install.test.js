'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { install, mergeInstructions } = require('../scripts/install');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-install-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test('portable install is repeatable, complete, and preserves unrelated config and state', t => {
  const root = fixture(t);
  const config = path.join(root, '.claude');
  fs.mkdirSync(path.join(config, 'state'), { recursive: true });
  fs.writeFileSync(path.join(config, 'settings.json'), '{"model":"existing"}');
  fs.writeFileSync(path.join(config, 'state', 'claude-routing-policy.json'), '{"keep":"history"}');
  fs.writeFileSync(path.join(config, 'CLAUDE.md'), '\uFEFF# Personal instructions\r\nPreserve café.\r\n');
  install({ userRoot: root });
  const first = fs.readFileSync(path.join(config, 'CLAUDE.md'), 'utf8');
  install({ userRoot: root });
  assert.equal(fs.readFileSync(path.join(config, 'CLAUDE.md'), 'utf8'), first);
  assert.match(first, /Preserve café/);
  assert.equal(first.split('<!-- claude-model-routing:start -->').length, 2);
  assert.equal(fs.readFileSync(path.join(config, 'settings.json'), 'utf8'), '{"model":"existing"}');
  assert.equal(fs.readFileSync(path.join(config, 'state', 'claude-routing-policy.json'), 'utf8'), '{"keep":"history"}');
  for (const agent of ['haiku-efficient', 'sonnet-general', 'opus-expert', 'ganglion-worker']) {
    assert.ok(fs.existsSync(path.join(config, 'agents', agent + '.md')));
  }
  for (const script of ['manage-routing-policy.js', 'run-ganglion-task.js', 'manage-claude-routing-policy.ps1',
    'sweep-ganglion-resources.ps1', 'test-ganglion-access.ps1', 'invoke-ganglion-worker.ps1']) {
    const file = path.join(config, 'skills', 'claude-model-routing', 'scripts', script);
    assert.ok(fs.existsSync(file));
    assert.doesNotMatch(fs.readFileSync(file, 'utf8'), /C:\\Users\\NM2|\.codex[\\/]/i);
  }
  assert.equal(fs.existsSync(path.join(root, '.mcp.json')), false);
});

test('dry run has no side effects and skip-global leaves CLAUDE.md absent', t => {
  const root = fixture(t);
  install({ userRoot: root, dryRun: true });
  assert.equal(fs.existsSync(path.join(root, '.claude')), false);
  install({ userRoot: root, skipGlobal: true });
  assert.equal(fs.existsSync(path.join(root, '.claude', 'CLAUDE.md')), false);
});

test('upgrade replaces owned skill contents so removed scripts do not survive', t => {
  const root = fixture(t);
  install({ userRoot: root });
  const skills = path.join(root, '.claude', 'skills');
  const stale = path.join(skills, 'claude-model-routing', 'scripts', 'removed-worker.js');
  fs.writeFileSync(stale, 'obsolete package code');
  fs.mkdirSync(path.join(skills, 'unrelated-personal-skill'));
  fs.writeFileSync(path.join(skills, 'unrelated-personal-skill', 'SKILL.md'), 'preserve');
  install({ userRoot: root });
  assert.equal(fs.existsSync(stale), false);
  assert.equal(fs.readFileSync(path.join(skills, 'unrelated-personal-skill', 'SKILL.md'), 'utf8'), 'preserve');
  assert.equal(fs.readdirSync(skills).some(name => name.startsWith('.routing-stage-')), false);
});

test('rejects source overlap and ambiguous markers before copying', t => {
  const root = fixture(t);
  assert.throws(() => install({ configRoot: path.resolve(__dirname, '../bad-install') }), /source package/);
  const config = path.join(root, '.claude');
  fs.mkdirSync(config);
  fs.writeFileSync(path.join(config, 'CLAUDE.md'), '<!-- claude-model-routing:start -->');
  assert.throws(() => install({ configRoot: config }), /ambiguous/);
  assert.equal(fs.existsSync(path.join(config, 'skills')), false);
  assert.throws(() => mergeInstructions('<!-- claude-model-routing:end --><!-- claude-model-routing:start -->', 'x'), /ambiguous/);
});

test('refuses to install through a symlink or Windows junction', t => {
  const root = fixture(t);
  const actual = path.join(root, 'actual');
  const alias = path.join(root, 'alias');
  fs.mkdirSync(actual);
  fs.symlinkSync(actual, alias, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => install({ configRoot: alias }), /symlink\/junction/);
  assert.deepEqual(fs.readdirSync(actual), []);
});
