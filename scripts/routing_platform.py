"""Small platform adapters; no service, registry, shell-policy or ACL changes."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile


def private(path, helper):
    path = Path(path)
    if os.name == 'nt':
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(helper),
                                     '-LiteralPath', str(path)], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            raise ValueError('private_state_probe_timeout') from None
        except OSError:
            raise ValueError('private_state_probe_unavailable') from None
        if result.returncode != 0:
            raise ValueError('private_state_probe_execution_failed')
        try: verdict = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ValueError('private_state_probe_invalid_response') from None
        if isinstance(verdict, dict) and set(verdict) == {'private'} and verdict['private'] is True: return True
        if isinstance(verdict, dict) and verdict.get('private') is False:
            if verdict.get('reason') == 'acl_probe_unavailable':
                raise ValueError('private_state_probe_unavailable')
            return False
        raise ValueError('private_state_probe_invalid_response')
    info = path.stat()
    return info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & (0o022 if path.is_dir() else 0o077)


def detached():
    if os.name == 'nt':
        return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def desktop_exec(arguments):
    # Desktop Entry quoting, not shell quoting. No field-code substitutions.
    def quote(value):
        if any(c in value for c in '\n\r\x00'): raise ValueError('invalid_launcher_argument')
        value = value.replace('%', '%%').replace('\\', '\\\\\\\\')
        for character in ('"', '`', '$'): value = value.replace(character, '\\\\' + character)
        return '"' + value + '"'
    return ' '.join(quote(str(value)) for value in arguments)


def launcher_specs(user, claude, python, shortcuts=False, login=False, platform=sys.platform, state=None):
    """Return only known per-user destinations and exact launch arguments."""
    user, claude = Path(user), Path(claude)
    script = claude / 'skills/claude-model-routing/scripts/routing_bootstrap.py'
    arguments = [str(python), '-B', str(script)]
    tail = ['--user-root', str(user), '--claude-home', str(claude)]
    if state is not None: tail += ['--state-dir', str(state)]
    result = []
    if not shortcuts and not login:return result
    if platform == 'win32':
        # GetFolderPath honors redirected user folders; fixtures never touch them.
        if user.resolve() == Path.home().resolve():
            command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); @{desktop=[Environment]::GetFolderPath('Desktop');programs=[Environment]::GetFolderPath('Programs');startup=[Environment]::GetFolderPath('Startup')}|ConvertTo-Json -Compress"
            folders = json.loads(subprocess.check_output(['powershell.exe', '-NoProfile', '-Command', command], text=True, encoding='utf-8', timeout=5))
        else: folders = {'desktop': str(user/'Desktop'), 'programs': str(user/'StartMenu'), 'startup': str(user/'Startup')}
        for folder, action in ([('desktop', 'open'), ('programs', 'open')] if shortcuts else []) + ([('startup', 'start')] if login else []):
            result.append((Path(folders[folder])/'Claude Routing Dashboard.lnk', 'windows', arguments + [action] + tail))
    elif platform == 'darwin':
        if shortcuts:
            result.append((user/'Desktop/Claude Routing Dashboard.command','executable',
                ('#!/bin/sh\nexec ' + shlex.join(arguments + ['open'] + tail) + '\n').encode()))
        if login:
            result.append((user/'Library/LaunchAgents/org.claude-model-routing.dashboard.plist', 'bytes',
                plistlib.dumps({'Label':'org.claude-model-routing.dashboard', 'ProgramArguments':arguments+['_login']+tail,
                                'RunAtLoad':True})))
    else:
        def entry(browser): return ('[Desktop Entry]\nType=Application\nName=Claude Routing Dashboard\nTerminal=false\nExec=' + desktop_exec(arguments+['_login']+(['--open-browser'] if browser else [])+tail) + '\n').encode()
        if shortcuts: result.append((user/'.local/share/applications/claude-routing-dashboard.desktop','executable',entry(True)))
        if login: result.append((user/'.config/autostart/claude-routing-dashboard.desktop','bytes',entry(False)))
    return result


def windows_shortcut(path, arguments, helper, expected=None):
    python = Path(arguments[0])
    pythonw = python.with_name('pythonw.exe')
    if pythonw.is_file(): target, args = str(pythonw), arguments[1:]
    else:
        target = 'powershell.exe'
        # The supported bootstrap wrapper hides the console while Python runs.
        script = Path(arguments[2]).with_name('bootstrap.ps1')
        command = arguments[3]
        user = arguments[arguments.index('--user-root') + 1]
        claude = arguments[arguments.index('--claude-home') + 1]
        args = ['-NoProfile', '-WindowStyle', 'Hidden', '-File', str(script), '-Command', command,
                '-PythonPath', str(python), '-UserRoot', user, '-ClaudeHome', claude]
        if '--state-dir' in arguments: args += ['-StateDir', arguments[arguments.index('--state-dir') + 1]]
    descriptor,name=tempfile.mkstemp(prefix='.routing-shortcut-',suffix='.lnk',dir=path.parent)
    os.close(descriptor);temporary=Path(name);temporary.unlink()
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(helper),
                        '-Path', str(temporary), '-Target', target, '-Arguments', subprocess.list2cmdline(args)],
                       capture_output=True, text=True, timeout=10, check=True)
        if expected is None:
            # Windows rename refuses to overwrite a file created concurrently.
            os.rename(temporary,path)
        else:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise OSError('owned_shortcut_changed')
            os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
