"""User-scoped skill files, version receipts, backups and guarded rollback.

No global CLAUDE.md, settings, credential, telemetry setup, browser profile or ACL is
rewritten. Missing profile/prompt templates become user-owned and are preserved.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

import routing_platform as platform

SOURCE = Path(__file__).resolve().parents[1]
SKILL = Path('skills/claude-model-routing')
COMPANION = Path('skills/claude-routing')


def package_owned(name):
    path=Path(name)
    return path.is_relative_to(SKILL) or path.is_relative_to(COMPANION)


def sha(raw): return hashlib.sha256(raw).hexdigest()
def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def encoded(value): return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


class Paths:
    def __init__(self, user=None, claude=None, state=None):
        self.user = Path(user or Path.home()).expanduser().absolute()
        self.claude = Path(claude or (os.environ.get('CLAUDE_CONFIG_DIR') if user is None else None) or self.user/'.claude').expanduser().absolute()
        self.skill = self.claude / SKILL
        self.state = Path(state or (os.environ.get('CLAUDE_ROUTER_STATE_DIR') if user is None else None) or self.claude/'routing').expanduser().absolute()
        self.service = self.state/'dashboard-service'
        self.receipt = self.service/'installation.json'
        self.run = self.service/'run.json'
        self.cap = self.service/'control.capability'
        self.helper = SOURCE/'scripts/Test-RoutingTelemetryAcl.ps1'

    def target(self, relative): return contained(self.claude, relative)


def contained(base, relative):
    base, name = Path(base).absolute(), Path(relative)
    if name.is_absolute() or '..' in name.parts or not name.parts: raise ValueError('unsafe_owned_path')
    target = base/name
    for path in (base, target, *target.parents):
        if path == base.parent: break
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400: raise ValueError('linked_install_destination')
    if not target.resolve().is_relative_to(base.resolve()): raise ValueError('owned_path_escape')
    return target


def atomic(path, raw, executable=False):
    path = Path(path)
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('linked_write_target')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.routing-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream: stream.write(raw)
        if os.name != 'nt': os.chmod(name, 0o700 if executable else 0o600)
        os.replace(name, path)
    finally: Path(name).unlink(missing_ok=True)


def prepare(paths):
    contained(paths.claude, 'skills/claude-model-routing')
    contained(paths.state, 'dashboard-service/installation.json')
    resolved=paths.service.resolve()
    if any((parent/'.git').exists() for parent in (resolved,*resolved.parents)):
        raise ValueError('state_must_be_outside_repository')
    paths.service.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not platform.private(paths.service, paths.helper): raise ValueError('private_state_permissions_required')


@contextmanager
def lock(paths):
    prepare(paths)
    with (paths.service/'.operation.lock').open('a+b') as stream:
        stream.seek(0)
        if stream.read(1) == b'': stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise ValueError('another_lifecycle_operation_is_running') from None
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


def receipt(paths):
    if not paths.receipt.is_file(): return None
    result = read_json(paths.receipt)
    if result.get('schema_version') != 1 or result.get('claude_home') != str(paths.claude): raise ValueError('invalid_installation_receipt')
    for name, digest in result.get('files', {}).items():
        paths.target(name)
        if not isinstance(digest, str) or len(digest) != 64: raise ValueError('invalid_owned_manifest')
    return result


def integrity(paths, installed):
    changed = []
    for name, expected in installed['files'].items():
        path = paths.target(name)
        if not path.is_file() or sha(path.read_bytes()) != expected: changed.append(name)
    return changed


def source_files(source):
    source = Path(source).resolve()
    if not (source/'VERSION').is_file():raise ValueError('incomplete_skill_source')
    # Repository and downloaded archive layouts are both supported. Enumerate
    # public roots directly so uncommitted release files receive the same checks.
    roots = {'scripts','assets','agents','references','docs','tests','skills','mcp','.github'}
    top_files = {'SKILL.md','README.md','CHANGELOG.md','LICENSE','VERSION','package.json','package-lock.json','.gitignore','.gitattributes','setup-prompt_IMPORTANT_1ST_STEP_AGENT_READ_FIRST.md'}
    names = [name for name in top_files if (source/name).is_file()]
    for root in sorted(roots):
        if (source/root).is_dir():
            names.extend(str(p.relative_to(source)).replace('\\','/') for p in (source/root).rglob('*') if p.is_file())
    result = {}
    for name in filter(None, names):
        parts = Path(name).parts
        if (parts[0] not in roots and name not in top_files) or any(p.startswith('.env') or p in ('.git','__pycache__','node_modules') or
            p.endswith(('.pyc','.dpapi','.capability')) or p.startswith(('usage.sqlite3','otel-config-before-','.dashboard-launch-')) or
            p in ('otel-setup-receipt.json','.otel-setup.lock') for p in parts): continue
        path = contained(source, name)
        if path.stat().st_size > 4194304: raise ValueError('source_file_too_large')
        result[name] = path.read_bytes()
        if len(result)>1024 or sum(map(len,result.values()))>33554432:raise ValueError('source_package_too_large')
    # Flatten the primary skill's existing helpers into its installed root while
    # retaining the complete source package, including the other Claude skill.
    prefix='skills/claude-model-routing/'
    for name, body in list(result.items()):
        if name.startswith(prefix):
            flat=name[len(prefix):]
            if flat in result and result[flat]!=body:raise ValueError('source_package_path_conflict')
            result[flat]=body
    required = ('SKILL.md','VERSION','scripts/routing_bootstrap.py','scripts/routing_dashboard.py','scripts/routing_platform.py')
    if any(name not in result for name in required) or b'name: claude-model-routing' not in result['SKILL.md']:
        raise ValueError('incomplete_skill_source')
    return result


def shortcut_allowed(paths, installed):
    return {str(path.absolute()) for path, _kind, _body in platform.launcher_specs(paths.user, paths.claude, installed['python'], True, True, state=paths.state)}


def shortcut_spec(kind, body):
    return sha(encoded([kind, body.hex() if isinstance(body, bytes) else body]))


def install(paths, source=SOURCE, repair=False, shortcuts=None, login=None, port=None, adopt=False, plan_only=False):
    before = receipt(paths)
    raw = source_files(source)
    version = raw['VERSION'].decode().strip()
    revision = version + '+' + sha(encoded({name:sha(body) for name,body in raw.items()}))[:16]
    settings = {'shortcuts': bool(shortcuts if shortcuts is not None else (before or {}).get('shortcuts_enabled',False)),
                'login': bool(login if login is not None else (before or {}).get('login_startup',False))}
    planned = {str(SKILL/name):body for name,body in raw.items()}
    for name,body in raw.items():
        if name.startswith('skills/claude-routing/'):
            planned[str(Path(name))]=body
    preserved = []
    for name in ('haiku-efficient','sonnet-general','opus-expert','ganglion-worker'):
        relative = 'agents/' + name + '.md'; path = paths.target(relative)
        if not path.exists():
            planned[relative] = raw['assets/agents/'+name+'.md']
        else: preserved.append(relative)
    if before and not repair:
        if any(package_owned(name) for name in integrity(paths,before)):
            raise ValueError('owned_package_changed_use_repair_after_review')
    for name in planned:
        path = paths.target(name)
        if path.exists() and (not before or name not in before['files']) and package_owned(name) and not adopt:
            raise ValueError('existing_unmanaged_skill_requires_review')
    desired = {'schema_version':1,'version':version,'revision':revision,'claude_home':str(paths.claude),
               'user_root':str(paths.user),'python':sys.executable,'port':port if port is not None else (before or {}).get('port',8765),
               'files':{name:sha(body) for name,body in planned.items() if package_owned(name)},'preserved_user_files':preserved,
               'shortcuts_enabled':settings['shortcuts'],'login_startup':settings['login'],
               'otel_receiver':(before or {}).get('otel_receiver',False),'shortcut_files':(before or {}).get('shortcut_files',{}),
               'shortcut_specs':(before or {}).get('shortcut_specs',{})}
    if not 0 <= desired['port'] <= 65535: raise ValueError('invalid_port')
    shortcuts_healthy = before and all(Path(n).is_file() and sha(Path(n).read_bytes())==v for n,v in before.get('shortcut_files',{}).items())
    shortcuts_healthy = shortcuts_healthy and all(p.is_file() for p,_,_ in platform.launcher_specs(paths.user,paths.claude,sys.executable,settings['shortcuts'],settings['login'],state=paths.state))
    same = before and all(before.get(k)==v for k,v in desired.items() if k not in ('shortcut_files','preserved_user_files')) and not integrity(paths,before) and shortcuts_healthy and not before.get('shortcut_errors')
    if plan_only:return {'changed':not bool(same),'revision':revision}
    if same: return {'installed':True,'changed':False,'revision':revision,'preserved_user_files':preserved,**launcher_state(paths,before)}
    backup_id = str(time.time_ns()) + '-' + secrets.token_hex(4)
    backup = paths.service/'backups'/backup_id
    originals = {}
    for name in set(planned) | set((before or {}).get('files',{})):
        path = paths.target(name)
        originals[name] = path.read_bytes() if path.is_file() else None
    backup.mkdir(parents=True, mode=0o700)
    atomic(backup/'before.json',encoded(before))
    # Flat names avoid duplicating long installed paths inside Windows backups.
    saved={name:{'sha256':sha(body) if body is not None else None,'blob':format(index,'06x')}
           for index,(name,body) in enumerate(sorted(originals.items()))}
    atomic(backup/'originals.json',encoded(saved))
    for name, body in originals.items():
        if body is not None: atomic(backup/'files'/saved[name]['blob'],body)
    try:
        for name, body in planned.items():
            path = paths.target(name)
            if not path.is_file() or path.read_bytes()!=body: atomic(path,body,name.endswith('.sh'))
        # Remove only unchanged formerly-owned public files absent in the new source.
        for name, expected in (before or {}).get('files',{}).items():
            if name not in planned and package_owned(name):
                path=paths.target(name)
                if path.is_file() and sha(path.read_bytes())==expected: path.unlink()
        desired['backup_id']=backup_id
        atomic(paths.receipt,encoded(desired))
        sync_shortcuts(paths,desired)
    except BaseException:
        for name, body in originals.items():
            path=paths.target(name)
            if body is None: path.unlink(missing_ok=True)
            else: atomic(path,body,name.endswith('.sh'))
        if before: atomic(paths.receipt,encoded(before))
        else: paths.receipt.unlink(missing_ok=True)
        raise
    return {'installed':True,'changed':True,'revision':revision,'backup_id':backup_id,'preserved_user_files':preserved,
            'shortcuts_requested':desired['shortcuts_enabled'],**launcher_state(paths,desired),
            'discovery':'installed SKILL.md; start a new Claude Code session if its skill catalog has not refreshed'}


def sync_shortcuts(paths, installed):
    desired = platform.launcher_specs(paths.user,paths.claude,installed['python'],installed['shortcuts_enabled'],installed['login_startup'],state=paths.state)
    previous = dict(installed.get('shortcut_files',{})); previous_specs=installed.get('shortcut_specs',{})
    kept={}; specs={}; errors=[]; conflicts=[]
    allowed = shortcut_allowed(paths,installed)
    for path, kind, body in desired:
        path=path.absolute(); name=str(path)
        if name not in allowed or path.resolve()!=path.absolute(): raise ValueError('unsafe_shortcut_destination')
        expected=None; fingerprint=shortcut_spec(kind,body)
        if path.is_file():
            if name not in previous or sha(path.read_bytes())!=previous[name]:conflicts.append(path.name);continue
            expected=previous[name]
            if previous_specs.get(name)==fingerprint:
                kept[name]=expected;specs[name]=fingerprint;continue
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            if kind=='windows': platform.windows_shortcut(path,body,paths.skill/'scripts/New-RoutingShortcut.ps1',expected)
            else: atomic(path,body,kind=='executable')
            kept[name]=sha(path.read_bytes())
            specs[name]=fingerprint
        except (OSError,subprocess.SubprocessError):
            errors.append(path.name)
            if expected is not None:
                kept[name]=expected
                if name in previous_specs:specs[name]=previous_specs[name]
    for name, expected in previous.items():
        path=Path(name)
        if name not in kept and name in allowed and path.is_file() and sha(path.read_bytes())==expected: path.unlink()
    installed['shortcut_files']=kept
    installed['shortcut_specs']=specs
    installed['shortcut_errors']=errors
    installed['shortcut_conflicts']=conflicts
    atomic(paths.receipt,encoded(installed))


def launcher_state(paths,installed):
    requested=installed['login_startup']
    specs=platform.launcher_specs(paths.user,paths.claude,installed['python'],False,requested,state=paths.state)
    owned=installed.get('shortcut_files',{})
    configured=bool(specs) and all(str(p.absolute()) in owned and p.is_file() and
        sha(p.read_bytes())==owned[str(p.absolute())] and
        installed.get('shortcut_specs',{}).get(str(p.absolute()))==shortcut_spec(kind,body) for p,kind,body in specs)
    return {'login_startup_requested':requested,'login_startup_configured':configured,
            'shortcut_errors':installed.get('shortcut_errors',[]),'shortcut_conflicts':installed.get('shortcut_conflicts',[])}


def rollback(paths):
    current=receipt(paths)
    if not current: raise ValueError('not_installed')
    backup=contained(paths.service/'backups',current['backup_id'])
    before=read_json(backup/'before.json')
    if integrity(paths,current): raise ValueError('owned_files_changed_rollback_refused')
    originals=read_json(backup/'originals.json')
    restored={}
    for name,record in originals.items():
        expected=record['sha256']
        if expected is not None:
            if len(record['blob'])!=6 or any(c not in '0123456789abcdef' for c in record['blob']):raise ValueError('invalid_backup_blob')
            data=contained(backup/'files',record['blob']).read_bytes()
            if sha(data)!=expected: raise ValueError('rollback_backup_changed')
            restored[name]=data
    # A path removed by the update is no longer owned. A user may have recreated
    # it since; validate every restore destination before changing any files.
    for name in restored:
        path=paths.target(name)
        if name not in current['files'] and path.exists():
            raise ValueError('recreated_path_rollback_refused')
    for name,expected in current['files'].items():
        if name not in restored:
            path=paths.target(name)
            if path.is_file() and sha(path.read_bytes())==expected:path.unlink()
    for name,body in restored.items():atomic(paths.target(name),body,name.endswith('.sh'))
    if before:
        before['shortcut_files']=current.get('shortcut_files',{})
        before['shortcut_specs']=current.get('shortcut_specs',{})
        atomic(paths.receipt,encoded(before));sync_shortcuts(paths,before)
    else:
        for name,expected in current.get('shortcut_files',{}).items():
            path=Path(name)
            if name in shortcut_allowed(paths,current) and path.is_file() and sha(path.read_bytes())==expected:path.unlink()
        paths.receipt.unlink()
    return {'rolled_back':True,'revision':before['revision'] if before else None,'backups_retained':True}


def prune_empty(path):
    if not path.is_dir() or path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400:return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink() and not getattr(child.lstat(),'st_file_attributes',0)&0x400:prune_empty(child)
    try:path.rmdir()
    except OSError:pass


def uninstall(paths):
    installed=receipt(paths)
    if not installed:return {'uninstalled':True,'changed':False}
    kept=[]
    for name,expected in installed['files'].items():
        path=paths.target(name)
        if path.is_file() and sha(path.read_bytes())==expected:path.unlink()
        elif path.exists():kept.append(name)
    allowed=shortcut_allowed(paths,installed)
    for name,expected in installed.get('shortcut_files',{}).items():
        path=Path(name)
        if name in allowed and path.resolve()==path.absolute() and path.is_file() and sha(path.read_bytes())==expected:path.unlink()
    # Remove empty folders only; never follow links or recursively delete files.
    prune_empty(paths.skill)
    prune_empty(paths.claude/COMPANION)
    paths.receipt.unlink()
    return {'uninstalled':True,'preserved_modified_files':kept,'history_credentials_config_and_backups_preserved':True,
            'telemetry_exporter_unchanged':'If configured, use approved telemetry undo separately; uninstall never rewrites Claude settings.'}
