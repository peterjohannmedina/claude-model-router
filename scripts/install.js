#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const SKILLS = ['claude-model-routing', 'claude-routing'];
const AGENTS = ['haiku-efficient.md', 'sonnet-general.md', 'opus-expert.md', 'ganglion-worker.md'];
const START = '<!-- claude-model-routing:start -->';
const END = '<!-- claude-model-routing:end -->';

function mergeInstructions(existing, snippet) {
  const text = existing.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  const block = snippet.replace(/\r\n/g, '\n').trim();
  const starts = text.split(START).length - 1;
  const ends = text.split(END).length - 1;
  if (starts !== ends || starts > 1 || (starts && text.indexOf(END) < text.indexOf(START))) {
    throw new Error('Existing CLAUDE.md has ambiguous routing markers; repair them before installing');
  }
  if (!starts) return `${text.trimEnd()}${text.trim() ? '\n\n' : ''}${block}\n`;
  return text.slice(0, text.indexOf(START)) + block + text.slice(text.indexOf(END) + END.length);
}

function assertNoLinks(target) {
  let current = path.resolve(target);
  while (true) {
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) {
      throw new Error(`Refusing a symlink/junction installation target: ${current}`);
    }
    const parent = path.dirname(current);
    if (current === parent) break;
    current = parent;
  }
}

function replaceOwnedSkill(from, to, destination) {
  const resolved = path.resolve(to);
  const parent = path.join(destination, 'skills');
  if (!SKILLS.some(name => resolved === path.join(parent, name))) {
    throw new Error('Refusing to replace a directory outside the owned skill paths');
  }
  assertNoLinks(parent);
  fs.mkdirSync(parent, { recursive: true });
  const stage = fs.mkdtempSync(path.join(parent, '.routing-stage-'));
  const backup = stage + '.previous';
  // Before any recursive cleanup or move, check the resolved targets stay within
  // the explicit config root. Staging and backups are immediate children only.
  for (const candidate of [stage, backup, resolved]) {
    if (path.dirname(path.resolve(candidate)) !== parent) throw new Error('Unsafe skill replacement path');
  }
  let moved = false;
  let installed = false;
  try {
    fs.cpSync(from, stage, { recursive: true });
    if (fs.existsSync(resolved)) { fs.renameSync(resolved, backup); moved = true; }
    try { fs.renameSync(stage, resolved); installed = true; }
    catch (error) {
      if (moved) fs.renameSync(backup, resolved);
      throw error;
    }
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
    // Retain the backup if a rollback itself failed; never erase the last copy.
    if (installed) fs.rmSync(backup, { recursive: true, force: true });
  }
}

function install({ sourceRoot = path.resolve(__dirname, '..'), userRoot, configRoot, skipGlobal = false, dryRun = false } = {}) {
  const source = fs.realpathSync(sourceRoot);
  const destination = path.resolve(configRoot || (userRoot ? path.join(userRoot, '.claude') :
    process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude')));
  const relative = path.relative(source, destination);
  if (!relative || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))) {
    throw new Error('Refusing to install inside the source package');
  }
  const copies = [
    ...SKILLS.map(name => [path.join(source, 'skills', name), path.join(destination, 'skills', name)]),
    ...AGENTS.map(name => [path.join(source, 'assets', 'agents', name), path.join(destination, 'agents', name)]),
  ];
  for (const [from, to] of copies) {
    if (!fs.existsSync(from)) throw new Error(`Incomplete package: ${from}`);
    assertNoLinks(to);
    if (fs.statSync(from).isDirectory()) {
      for (const file of fs.readdirSync(from, { recursive: true, withFileTypes: true })) {
        const base = file.parentPath || file.path;
        const sourcePath = path.join(base, file.name);
        if (file.isSymbolicLink()) throw new Error('Package must not contain symlinks');
        assertNoLinks(path.join(to, path.relative(from, sourcePath)));
      }
    }
  }
  const instructionPath = path.join(destination, 'CLAUDE.md');
  assertNoLinks(instructionPath);
  let instructions;
  if (!skipGlobal) {
    const prior = fs.existsSync(instructionPath) ? fs.readFileSync(instructionPath, 'utf8') : '';
    instructions = mergeInstructions(prior, fs.readFileSync(path.join(source, 'assets', 'CLAUDE.md.snippet'), 'utf8'));
  }
  if (!dryRun) {
    for (const [from, to] of copies) {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      if (fs.statSync(from).isDirectory()) replaceOwnedSkill(from, to, destination);
      else fs.copyFileSync(from, to);
    }
    if (!skipGlobal) fs.writeFileSync(instructionPath, instructions, 'utf8');
  }
  return { dry_run: dryRun, config_root: destination, skills: SKILLS, agents: AGENTS,
    global_instruction: !skipGlobal, settings_changed: false, mcp_registration_changed: false };
}

if (require.main === module) {
  try {
    const options = {};
    const args = process.argv.slice(2);
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--skip-global-instruction') options.skipGlobal = true;
      else if (args[i] === '--dry-run') options.dryRun = true;
      else if (['--user-root', '--config-root', '--source-root'].includes(args[i]) && args[i + 1]) {
        const key = { '--user-root': 'userRoot', '--config-root': 'configRoot', '--source-root': 'sourceRoot' }[args[i]];
        options[key] = args[++i];
      } else throw new Error(`Unknown or incomplete option: ${args[i]}`);
    }
    console.log(JSON.stringify(install(options), null, 2));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = { install, mergeInstructions };
