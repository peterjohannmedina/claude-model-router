"""Skill-first bootstrap and bounded, capability-authenticated dashboard lifecycle."""
import sys
if sys.version_info < (3,11):
    print('{"error":"python_3_11_or_newer_required","dependency_installed":false}')
    raise SystemExit(2)

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
import webbrowser

import routing_installation as installation
import routing_platform as platform

ERROR_HINTS={
    'recreated_path_rollback_refused':'A removed package path now contains new content. Preserve or move it before retrying rollback; no package files were restored.',
    'owned_package_changed_use_repair_after_review':'Owned package files changed. Review them, then use repair; a private backup is kept.',
    'owned_files_changed_review_then_repair':'Package integrity check failed. Review local edits before repair.',
    'existing_unmanaged_skill_requires_review':'An unmanaged skill exists. Review it before explicitly choosing --adopt-existing; it will be backed up.',
    'private_state_permissions_required':'State permissions are not private. No ACL or security policy was changed; review permissions separately.',
    'private_state_probe_execution_failed':'Windows could not execute the read-only permission checker. Review the launcher execution context and its AuthorizationManager/application-control diagnostics. No ACL or execution-policy setting was changed; do not bypass the checker.',
    'private_state_probe_unavailable':'The read-only permission check is unavailable. This is not a verdict that the directory is unsafe. Review access in an approved execution context; no permission workaround was attempted.',
    'private_state_probe_timeout':'The read-only permission checker timed out. Startup remains blocked; no permissions or security policy were changed.',
    'private_state_probe_invalid_response':'The permission checker did not return its expected verdict. Startup remains blocked; no permissions or security policy were changed.',
    'loopback_port_unavailable_or_policy_blocked':'The loopback port is occupied or blocked. Do not bypass policy or kill an unrelated process; inspect status or choose an explicit port.',
    'approved_telemetry_setup_required':'The optional hook receiver needs a separately provisioned private capability and endpoint receipt. Installation and start never configure a Claude exporter or hooks.',
    'approved_telemetry_target_or_capability_changed':'The receiver endpoint or private capability differs from its provisioning receipt. Review the separate setup before enabling it.',
    'invalid_exporter_capability':'The receiver capability does not have the required token format. Provision it separately; no hooks or exporter settings were changed.',
    'approved_telemetry_scope_changed':'Telemetry differs from its approved setup. Review it; do not replace exporters automatically.',
    'receiver_start_not_confirmed_no_policy_bypass':'Background startup was not confirmed. Check status and loopback permissions; no security workaround was attempted.',
    'receiver_start_failed':'The background receiver could not start. Review dependency, permission and listener diagnostics in an approved context; no policy bypass was attempted.',
    'receiver_start_failed_see_status':'The background receiver could not start. Review dependency, permission and listener diagnostics in an approved context; no policy bypass was attempted.',
    'controller_rejected_or_unmanaged_server':'The listener is not the authenticated managed instance. It was not stopped or replaced.',
    'controller_connection_timeout':'The managed controller timed out. Its state is unverified; no listener was replaced or automatically retried.',
    'controller_connection_denied':'Access to the managed controller was denied. No alternate client or policy bypass was attempted.',
    'controller_connection_reset':'The managed controller connection was reset. Its state is unverified; no listener was replaced or automatically retried.',
    'controller_protocol_failed':'The managed controller did not return a valid HTTP response. No listener was replaced or automatically retried.',
    'controller_connection_failed':'The managed controller connection failed. Its state is unverified; no listener was replaced or automatically retried.',
    'not_installed':'Install the skill with the supported bootstrap first.',
    'install_skill_first':'Install the skill with the supported bootstrap first.',
    'state_must_be_outside_repository':'Choose a Claude config home outside Git; private control state and backups must not be committed.',
}
_children={}
CONTROL_TIMEOUT_SECONDS=3


def show_windows_error(code, hint):
    """A hidden pythonw launcher must not fail silently. Fixed text only."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            'Claude Routing Dashboard could not launch.\n\n'+code+'\n\n'+hint,
            'Claude Routing Dashboard', 0x10)
        return True
    except (OSError, AttributeError): return False


def emit(result):
    # pythonw normally has no standard streams; never display successful output
    # containing a private browser handoff path or any capability.
    if sys.stdout is not None:
        print(json.dumps(result))
    elif os.name == 'nt' and result.get('error'):
        code = result['error'] if result['error'] in ERROR_HINTS else 'bootstrap_operation_failed'
        show_windows_error(code, ERROR_HINTS.get(code,
            'Run the installed bootstrap in a visible terminal for a fixed diagnostic code. No policy bypass or paid call was attempted.'))


def controller(paths, action='status'):
    if not paths.run.is_file(): return None
    if not platform.private(paths.service,paths.helper) or not platform.private(paths.cap,paths.helper):
        raise ValueError('private_control_state_required')
    record=installation.read_json(paths.run)
    port=record.get('port')
    if type(port) is not int or not 1<=port<=65535:raise ValueError('invalid_receiver_state')
    token=paths.cap.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token):raise ValueError('invalid_control_capability')
    # Windows can take more than one second to return an explicit refusal for
    # a closed local port. Each socket operation has a three-second timeout;
    # timeout is still an unverified failure, never permission to replace a listener.
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=CONTROL_TIMEOUT_SECONDS)
    try:
        connection.request('POST','/api/lifecycle',json.dumps({'action':action}),
            {'Content-Type':'application/json','X-Routing-Control':token})
        response=connection.getresponse();raw=response.read(4097)
        if response.status!=200 or len(raw)>4096:raise ValueError('controller_rejected_or_unmanaged_server')
        result=json.loads(raw)
        if result.get('instance_id')!=record.get('instance_id'):raise ValueError('receiver_identity_mismatch')
        return result
    except ConnectionRefusedError:return None
    except TimeoutError:raise ValueError('controller_connection_timeout') from None
    except PermissionError:raise ValueError('controller_connection_denied') from None
    except ConnectionResetError:raise ValueError('controller_connection_reset') from None
    except http.client.HTTPException:raise ValueError('controller_protocol_failed') from None
    except OSError:raise ValueError('controller_connection_failed') from None
    finally:connection.close()


def status(paths):
    installed=installation.receipt(paths)
    if not installed:return {'installed':False,'running':False}
    result={'installed':True,'revision':installed['revision'],'version':installed['version'],
        'skill_path':str(paths.skill),'skill_discovery_file_present':(paths.skill/'SKILL.md').is_file(),
        'integrity_ok':not installation.integrity(paths,installed),**installation.launcher_state(paths,installed),
        'hook_receiver_opt_in':installed['otel_receiver'],'otel_receiver_opt_in':installed['otel_receiver'],'running':False,'url':None}
    try:
        live=controller(paths)
        if live:result.update(running=True,url=live['url'],instance_id=live['instance_id'],hook_receiver_enabled=live['otel_receiver_enabled'],otel_receiver_enabled=live['otel_receiver_enabled'])
    except (OSError,ValueError,KeyError,UnicodeError,http.client.HTTPException):
        result['running']=None
        result['lifecycle_access']='blocked_or_unverified; no network-policy bypass attempted'
    return result


def approved_otel(paths, port):
    # Optional receiver provisioning is manual and separate from install/start.
    # No Claude exporter, settings.json or hook configuration is read or changed.
    root=paths.state;cap=root/'otel-receiver.capability';receipt=root/'otel-setup-receipt.json'
    if not all(p.is_file() for p in (cap,receipt)):raise ValueError('approved_telemetry_setup_required')
    for path in (cap,receipt):
        if path.resolve()!=path.absolute() or not platform.private(path,paths.helper):raise ValueError('private_approved_telemetry_required')
    prepared=installation.read_json(receipt);raw=cap.read_bytes();token=raw.decode('ascii').strip()
    if prepared['endpoint']!='http://127.0.0.1:'+str(port)+'/v1/logs' or installation.sha(raw)!=prepared['capability_sha256']:
        raise ValueError('approved_telemetry_target_or_capability_changed')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token):raise ValueError('invalid_exporter_capability')
    return token


def stop(paths):
    current=controller(paths)
    if current is None:return {'stopped':True,'changed':False}
    controller(paths,'stop')
    deadline=time.monotonic()+5
    while paths.run.exists() and time.monotonic()<deadline:time.sleep(.05)
    if paths.run.exists():raise ValueError('receiver_stop_unconfirmed_no_process_was_killed')
    child=_children.pop(current['instance_id'],None)
    if child is not None:
        try:child.wait(timeout=1)
        except subprocess.TimeoutExpired:pass
    return {'stopped':True,'changed':True}


def start(paths, otel=None):
    installed=installation.receipt(paths)
    if not installed:raise ValueError('install_skill_first')
    if installation.integrity(paths,installed):raise ValueError('owned_files_changed_review_then_repair')
    live=controller(paths)
    if otel is not None and otel!=installed['otel_receiver']:
        if live:stop(paths);live=None
        installed['otel_receiver']=otel
        installation.atomic(paths.receipt,installation.encoded(installed))
    if live and live['revision']!=installed['revision']:stop(paths);live=None
    if live:return {'running':True,'changed':False,**live}
    # Refuse occupied unmanaged ports; never kill a PID from a stale file.
    probe=socket.socket()
    try:probe.bind(('127.0.0.1',installed['port']))
    except OSError:raise ValueError('loopback_port_unavailable_or_policy_blocked') from None
    finally:probe.close()
    if installed['otel_receiver']:approved_otel(paths,installed['port'])
    token=secrets.token_urlsafe(32);identity=str(uuid.uuid4())
    installation.atomic(paths.cap,(token+'\n').encode('ascii'))
    ready=paths.service/'ready.json';ready.unlink(missing_ok=True)
    command=[installed['python'],'-B',str(paths.skill/'scripts/routing_bootstrap.py'),'_serve',
        '--user-root',str(paths.user),'--claude-home',str(paths.claude),'--state-dir',str(paths.state),'--instance-id',identity]
    _children[identity]=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     cwd=str(paths.user),close_fds=True,**platform.detached())
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if ready.is_file():
            value=installation.read_json(ready)
            if value.get('instance_id')==identity:
                if value.get('error'):
                    child=_children.pop(identity,None)
                    if child is not None:
                        try:child.wait(timeout=1)
                        except subprocess.TimeoutExpired:pass
                    raise ValueError(value['error'] if value['error'] in ERROR_HINTS else 'receiver_start_failed_see_status')
                live=controller(paths)
                if live:return {'running':True,'changed':True,**live}
        time.sleep(.05)
    raise ValueError('receiver_start_not_confirmed_no_policy_bypass')


def open_dashboard(paths, otel=None, browser=True):
    started=start(paths,otel)
    result=controller(paths,'open')
    launch=Path(result.pop('launch_file'))
    if launch.parent!=paths.state or not launch.name.startswith('.dashboard-launch-') or not launch.is_file():
        raise ValueError('invalid_private_launch_file')
    try:opened=webbrowser.open(launch.as_uri()) if browser else False
    except (OSError,webbrowser.Error):opened=False
    return {'running':True,'url':result['url'],'browser_open_requested':bool(opened),
            'private_launch_file':str(launch),'expires_in_seconds':300,'instance_id':started['instance_id']}


def build_server(paths,identity):
    installed=installation.receipt(paths)
    if not installed or installation.integrity(paths,installed):raise ValueError('invalid_install')
    if not platform.private(paths.service,paths.helper) or not platform.private(paths.cap,paths.helper):raise ValueError('private_control_state_required')
    import routing_dashboard as dashboard
    token=paths.cap.read_text(encoding='ascii').strip()
    otel=approved_otel(paths,installed['port']) if installed['otel_receiver'] else None
    server=dashboard.Server(installed['port'],dashboard.Store(paths.state),otel,token,identity,installed['revision'])
    installation.atomic(paths.run,installation.encoded({'instance_id':identity,'port':server.server_port,'pid':os.getpid(),'revision':installed['revision']}))
    installation.atomic(paths.service/'ready.json',installation.encoded({'instance_id':identity,'ready':True}))
    return server


def cleanup_server(paths,identity,server):
    if server:server.server_close()
    if paths.run.is_file() and installation.read_json(paths.run).get('instance_id')==identity:
        # run.json is the stop-completion marker. Remove it last so a caller
        # cannot observe "stopped", restart, then lose its new capability to
        # the old receiver's remaining cleanup.
        paths.cap.unlink(missing_ok=True);paths.run.unlink()


def serve(paths, identity):
    """Internal detached child. Fixed-code failures only; never raw logs."""
    server=None
    try:
        server=build_server(paths,identity)
        server.serve_forever(poll_interval=.1)
    except (OSError,ValueError,KeyError,TypeError) as error:
        code=str(error) if str(error) in ERROR_HINTS else 'receiver_start_failed'
        installation.atomic(paths.service/'ready.json',installation.encoded({'instance_id':identity,'error':code}))
        return 2
    finally:
        cleanup_server(paths,identity,server)
    return 0


def login_serve(paths,browser=False):
    """Native login/desktop jobs keep their main process alive; no daemon fork."""
    identity=str(uuid.uuid4());server=None
    try:
        with installation.lock(paths):
            if controller(paths):
                if browser:open_dashboard(paths)
                return 0
            installed=installation.receipt(paths)
            if not installed or installation.integrity(paths,installed):raise ValueError('invalid_install')
            probe=socket.socket()
            try:probe.bind(('127.0.0.1',installed['port']))
            except OSError:raise ValueError('loopback_port_unavailable_or_policy_blocked') from None
            finally:probe.close()
            if installed['otel_receiver']:approved_otel(paths,installed['port'])
            installation.atomic(paths.cap,(secrets.token_urlsafe(32)+'\n').encode('ascii'))
            server=build_server(paths,identity)
        if browser:
            launch=server.renew_launch_file()
            try:webbrowser.open(launch.as_uri())
            except (OSError,webbrowser.Error):pass
        server.serve_forever(poll_interval=.1)
        return 0
    finally:cleanup_server(paths,identity,server)


def doctor(paths):
    result=status(paths)
    result.update(python='.'.join(map(str,sys.version_info[:3])),python_supported=sys.version_info>=(3,11),
                  claude_cli_found=shutil.which('claude') is not None,credentials_checked=False,model_calls=0,
                  telemetry_configuration_modified=False,
                  discovery='Files are checked; restart Claude Code if skill discovery has not refreshed.')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('install','update','repair','rollback','uninstall','status','doctor','start','open','stop','_serve','_login'))
    parser.add_argument('--user-root',type=Path)
    parser.add_argument('--claude-home',type=Path)
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--source-root',type=Path,default=installation.SOURCE)
    parser.add_argument('--port',type=int)
    parser.add_argument('--shortcuts',action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument('--login-startup',action=argparse.BooleanOptionalAction,default=None)
    parser.add_argument('--hook-receiver','--otel-receiver',dest='otel_receiver',action=argparse.BooleanOptionalAction,default=None,
                        help='Enable only a separately provisioned hook receiver; never configures an exporter or hooks')
    parser.add_argument('--start',action='store_true',help='Start after install; never implies login startup or telemetry activation')
    parser.add_argument('--open-browser',action='store_true')
    parser.add_argument('--no-browser',action='store_true',help='Return a private expiring handoff path without launching a browser')
    parser.add_argument('--adopt-existing',action='store_true',help='Explicitly back up and adopt an unmanaged existing skill; preserve unrelated files/profiles')
    parser.add_argument('--instance-id',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    try:
        if sys.version_info<(3,11):raise ValueError('python_3_11_or_newer_required')
        paths=installation.Paths(args.user_root,args.claude_home,args.state_dir)
        if args.command in ('install','update','repair'):
            source=Path(args.source_root).resolve()
            if not (source/'SKILL.md').is_file() and not (source/'skills/claude-model-routing/SKILL.md').is_file():raise ValueError('incomplete_skill_source')
            paths.helper=source/'scripts/Test-RoutingTelemetryAcl.ps1'
        if args.command=='_serve':
            uuid.UUID(args.instance_id)
            return serve(paths,args.instance_id)
        if args.command=='_login':return login_serve(paths,args.open_browser)
        if args.command in ('status','doctor'):
            result=status(paths) if args.command=='status' else doctor(paths)
        else:
            with installation.lock(paths):
                if args.command in ('install','update','repair'):
                    was=controller(paths) if installation.receipt(paths) else None
                    options=(paths,args.source_root,args.command=='repair',args.shortcuts,args.login_startup,args.port,args.adopt_existing)
                    change=installation.install(*options,plan_only=True)['changed']
                    if was and change:stop(paths)
                    try:result=installation.install(*options)
                    except BaseException:
                        if was and change:start(paths)
                        raise
                    if was or args.start:result['dashboard']=start(paths,args.otel_receiver)
                    if args.open_browser:result['dashboard']=open_dashboard(paths,args.otel_receiver,not args.no_browser)
                elif args.command=='rollback':
                    was=controller(paths);stop(paths)
                    result=installation.rollback(paths)
                    if was and installation.receipt(paths):result['dashboard']=start(paths)
                elif args.command=='uninstall':stop(paths);result=installation.uninstall(paths)
                elif args.command=='start':result=start(paths,args.otel_receiver)
                elif args.command=='open':result=open_dashboard(paths,args.otel_receiver,not args.no_browser)
                else:result=stop(paths)
        result.setdefault('claude_cli_found',shutil.which('claude') is not None)
        emit(result);return 0
    except (OSError,ValueError,KeyError,TypeError,UnicodeError,subprocess.SubprocessError,http.client.HTTPException) as error:
        # Raw exceptions may contain private paths or capability headers.
        code=str(error) if str(error) in ERROR_HINTS else 'bootstrap_operation_failed'
        emit({'error':code,'hint':ERROR_HINTS.get(code,'Check dependencies, private state permissions, owned-file integrity, explicit choices and loopback availability; no policy bypass or paid call was attempted.')});return 2


if __name__=='__main__':raise SystemExit(main())
