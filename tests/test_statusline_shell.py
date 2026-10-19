"""Inert Bash composition tests. No installed display, settings, or state is used."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SESSION = 'a73aa531-f48c-4abe-9d88-4bf520852968'


def bash_executable():
    explicit = os.environ.get('ROUTING_TEST_BASH')
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    if os.name == 'nt':
        # Do not select Windows' WSL bash shim or guess a user installation.
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
        return candidate if candidate.is_file() else None
    found = shutil.which('bash')
    return Path(found) if found else None


def shell_path(path):
    return Path(path).absolute().as_posix()


def quoted(path):
    return shlex.quote(shell_path(path))


def inert_environment():
    # No credentials, BASH_ENV, startup files, or Claude variables reach fixtures.
    names = ('PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC',
             'PATHEXT', 'TEMP', 'TMP', 'LANG')
    result = {key: os.environ[key] for key in names if key in os.environ}
    result['PYTHONDONTWRITEBYTECODE'] = '1'
    return result


class StatuslineShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = bash_executable()
        if cls.bash is None:
            if os.environ.get('ROUTING_REQUIRE_SHELL_TESTS') == '1':
                raise AssertionError('a native Bash executable is required for inert shell tests')
            raise unittest.SkipTest('native Bash unavailable; no alternate shell or policy bypass attempted')
        cls.env = inert_environment()
        try:
            probe = subprocess.run([str(cls.bash), '--noprofile', '--norc', '-c', 'exit 0'],
                                   capture_output=True, timeout=10, env=cls.env)
        except OSError:
            if os.environ.get('ROUTING_REQUIRE_SHELL_TESTS') == '1':
                raise
            raise unittest.SkipTest('native Bash startup unavailable; no escalation or alternate path attempted')
        if probe.returncode:
            denied = (os.name == 'nt' and b'NtCreateDirectoryObject' in probe.stderr
                      and b'0xC0000022' in probe.stderr)
            if denied and os.environ.get('ROUTING_REQUIRE_SHELL_TESTS') != '1':
                raise unittest.SkipTest('native Bash MSYS startup denied; no escalation or alternate path attempted')
            raise AssertionError('native Bash startup prerequisite failed: ' + str(probe.returncode))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='statusline shell ', dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'temporary state'
        self.state.mkdir()
        self.received = self.root / 'synthetic received.bin'
        self.display = self.root / 'inert display script.sh'
        self.consumer = self.root / 'inert display consumer.py'
        self.consumer.write_text(
            'from pathlib import Path\nimport sys\n'
            'mode, destination = sys.argv[1:]\n'
            'if mode == "closed": raise SystemExit(0)\n'
            'raw = sys.stdin.buffer.read()\n'
            'Path(destination).write_bytes(raw)\n'
            'if mode == "failure":\n'
            '    sys.stderr.buffer.write(b"synthetic-display-failure\\n")\n'
            '    raise SystemExit(7)\n'
            'sys.stdout.buffer.write(b"synthetic-display-output\\n")\n', encoding='utf-8')

    def payload(self, **changes):
        value = {'session_id': SESSION, 'version': '2.1.303',
                 'model': {'id': 'fixture-model', 'display_name': 'Synthetic'},
                 'cost': {'total_cost_usd': .25},
                 'context_window': {'total_input_tokens': 1000, 'total_output_tokens': 80},
                 'private_unknown': 'synthetic $() `echo never` ; & | < > \\ é'}
        value.update(changes)
        return b' \n' + json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\r\n'

    def run_pipeline(self, raw, mode='normal', config=None):
        self.display.write_text('#!/usr/bin/env bash\nexec ' + quoted(sys.executable)
            + ' -B ' + quoted(self.consumer) + ' ' + shlex.quote(mode)
            + ' ' + quoted(self.received) + '\n', encoding='utf-8', newline='\n')
        if config is not None:
            (self.state / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        # Explicit arguments and quoted paths represent the corrected proposal;
        # the existing user display and policy helper are never executed.
        command = (quoted(sys.executable) + ' -B ' + quoted(ROOT / 'scripts/collect_claude_statusline.py')
            + ' --state-dir ' + quoted(self.state) + ' --forward-stdin | '
            + quoted(self.bash) + ' --noprofile --norc ' + quoted(self.display))
        return subprocess.run([str(self.bash), '--noprofile', '--norc', '-c', command],
            input=raw, capture_output=True, timeout=20, cwd=self.root, env=self.env)

    def test_quoted_paths_and_exact_binary_stdin_preserve_display(self):
        raw = self.payload()
        result = self.run_pipeline(raw)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, b'synthetic-display-output\n')
        self.assertEqual(result.stderr, b'')
        self.assertEqual(self.received.read_bytes(), raw)
        with sqlite3.connect(self.state / 'usage.sqlite3') as connection:
            records = connection.execute('SELECT body FROM events').fetchall()
        self.assertEqual(len(records), 1)
        row = json.loads(records[0][0])
        self.assertEqual(row['source'], 'claude_statusline')
        self.assertEqual(row['session_id'], SESSION)
        self.assertNotIn('private_unknown', row)
        self.assertNotIn(b'echo never', (self.state / 'usage.sqlite3').read_bytes())

    def test_disabled_recording_preserves_display_without_ledger(self):
        raw = self.payload()
        result = self.run_pipeline(raw, config={'schema_version': 1, 'observability': {'enabled': False}})
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, b'synthetic-display-output\n', b''))
        self.assertEqual(self.received.read_bytes(), raw)
        self.assertFalse((self.state / 'usage.sqlite3').exists())

    def test_schema_or_config_failure_preserves_display(self):
        for raw, config in ((self.payload(effort={'level': 'future-effort'}), None),
                            (self.payload(), {'schema_version': False})):
            with self.subTest(config=config):
                result = self.run_pipeline(raw, config=config)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, b'synthetic-display-output\n', b''))
                self.assertEqual(self.received.read_bytes(), raw)
                self.assertFalse((self.state / 'usage.sqlite3').exists())

    def test_existing_display_failure_remains_visible(self):
        raw = self.payload()
        result = self.run_pipeline(raw, mode='failure')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'synthetic-display-failure\n')
        self.assertEqual(self.received.read_bytes(), raw)

    def test_closed_display_does_not_emit_collector_shutdown_error(self):
        result = self.run_pipeline(self.payload(private_unknown='x' * 65536), mode='closed')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        self.assertFalse(self.received.exists())

    def test_unquoted_windows_backslashes_are_not_a_compatible_bash_command(self):
        original = r'C:\Example\git\bin\bash.exe C:\Example\.claude\display.sh'
        self.assertEqual(shlex.split(original), ['C:Examplegitbinbash.exe', 'C:Example.claudedisplay.sh'])
        normalized = '"C:/Example/git/bin/bash.exe" "C:/Example/.claude/display.sh"'
        self.assertEqual(shlex.split(normalized), ['C:/Example/git/bin/bash.exe', 'C:/Example/.claude/display.sh'])


if __name__ == '__main__':
    unittest.main()
