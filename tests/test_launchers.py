"""Fixed diagnostics only; no host ACL scripts, GUI, sockets or credentials."""
from contextlib import redirect_stdout
import io
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import routing_bootstrap as bootstrap
import routing_platform as platform
import routing_installation as installation


class LauncherDiagnostics(unittest.TestCase):
    def test_reopen_accepts_canonical_parent_alias_but_refuses_linked_launch_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            root=Path(directory).resolve()
            state=root/'routing';state.mkdir()
            launch=state/'.dashboard-launch-synthetic.html';launch.write_text('synthetic handoff')
            alias=root/'alias'
            if os.name=='nt':
                import _winapi
                _winapi.CreateJunction(str(root/'routing'),str(alias))
            else:alias.symlink_to(state,target_is_directory=True)
            paths=SimpleNamespace(state=alias)
            with patch.object(bootstrap,'start',return_value={'instance_id':'synthetic'}), \
                 patch.object(bootstrap,'controller',side_effect=lambda *_:{'launch_file':str(launch),'url':'http://127.0.0.1:1'}), \
                 patch.object(bootstrap.webbrowser,'open') as browser:
                result=bootstrap.open_dashboard(paths,browser=False)
                self.assertEqual(result['private_launch_file'],str(launch));browser.assert_not_called()
                with patch.object(Path,'is_symlink',return_value=True),self.assertRaisesRegex(ValueError,'invalid_private_launch_file'):
                    bootstrap.open_dashboard(paths,browser=False)
                paths.state=root/'elsewhere'
                with self.assertRaisesRegex(ValueError,'invalid_private_launch_file'):
                    bootstrap.open_dashboard(paths,browser=False)

    def probe(self, result=None, error=None):
        with patch.object(platform,'os',SimpleNamespace(name='nt')), \
             patch.object(platform.subprocess,'run',return_value=result,side_effect=error) as run:
            try: return platform.private(ROOT,ROOT/'scripts/Test-RoutingTelemetryAcl.ps1')
            finally:
                args,kwargs=run.call_args
                self.assertNotIn('-ExecutionPolicy',args[0]);self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)

    def test_private_verdict_is_required_and_unsafe_still_refused(self):
        self.assertTrue(self.probe(SimpleNamespace(returncode=0,stdout='{"private":true}')))
        self.assertFalse(self.probe(SimpleNamespace(returncode=0,stdout='{"private":false,"reason":"other_principal_allow_rule"}')))
        for value in ('', 'not json', '{"private":true,"extra":"untrusted"}', '{"private":1}'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'private_state_probe_invalid_response'):
                self.probe(SimpleNamespace(returncode=0,stdout=value))

    def test_denial_timeout_and_unavailable_are_not_unsafe_acl_verdicts(self):
        failures=[(SimpleNamespace(returncode=1,stdout='',stderr='AuthorizationManager private/path secret'),None,'execution_failed'),
                  (None,subprocess.TimeoutExpired(['private/path'],5),'timeout'),
                  (None,PermissionError('private/path'),'unavailable'),
                  (SimpleNamespace(returncode=0,stdout='{"private":false,"reason":"acl_probe_unavailable"}'),None,'unavailable')]
        for result,error,code in failures:
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,'^private_state_probe_'+code+'$'):
                self.probe(result,error)

    def test_pythonw_error_surfaces_only_fixed_code_and_hint_without_private_output(self):
        with patch.object(bootstrap.sys,'stdout',None),patch.object(bootstrap,'os',SimpleNamespace(name='nt')), \
             patch.object(bootstrap,'show_windows_error') as show:
            bootstrap.emit({'error':'private_state_probe_execution_failed','hint':'untrusted sk-private/path'})
            args=show.call_args.args
            self.assertEqual(args[0],'private_state_probe_execution_failed')
            self.assertNotIn('sk-private',json.dumps(args))
            self.assertIn('do not bypass',args[1])
            show.reset_mock();bootstrap.emit({'running':True,'private_launch_file':'private/path','token':'sk-private'})
            show.assert_not_called()
            bootstrap.emit({'error':'private/path sk-private'})
            self.assertEqual(show.call_args.args[0],'bootstrap_operation_failed')

    def test_cli_error_code_is_specific_and_never_calls_lifecycle_when_lock_check_denied(self):
        output=io.StringIO()
        with patch.object(bootstrap.installation,'lock',side_effect=ValueError('private_state_probe_execution_failed')), \
             patch.object(bootstrap,'open_dashboard') as open_dashboard,redirect_stdout(output):
            code=bootstrap.main(['open','--user-root',str(ROOT.parent/'synthetic-user')])
        self.assertEqual(code,2);open_dashboard.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['error'],'private_state_probe_execution_failed')

    def test_detached_child_preserves_only_fixed_permission_failure_codes(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            paths=installation.Paths(Path(directory)/'user')
            for message,expected in [('private_state_probe_execution_failed','private_state_probe_execution_failed'),
                                     ('sk-private/path must not escape','receiver_start_failed')]:
                with patch.object(bootstrap,'build_server',side_effect=ValueError(message)):
                    self.assertEqual(bootstrap.serve(paths,'synthetic-instance'),2)
                self.assertEqual(installation.read_json(paths.service/'ready.json')['error'],expected)
                self.assertNotIn('sk-private',(paths.service/'ready.json').read_text())

    def test_parent_start_preserves_fixed_child_denial_without_network_or_real_child(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            paths=installation.Paths(Path(directory)/'user')
            installed={'python':sys.executable,'port':8765,'otel_receiver':False,'revision':'synthetic'}
            def child(*args,**kwargs):
                identity=args[0][-1]
                installation.atomic(paths.service/'ready.json',installation.encoded(
                    {'instance_id':identity,'error':'private_state_probe_execution_failed'}))
                return SimpleNamespace(wait=lambda **kwargs:0)
            with patch.object(installation,'receipt',return_value=installed),patch.object(installation,'integrity',return_value=[]), \
                 patch.object(bootstrap,'controller',return_value=None),patch.object(bootstrap.socket,'socket'), \
                 patch.object(bootstrap.platform,'detached',return_value={}),patch.object(bootstrap.subprocess,'Popen',side_effect=child):
                with self.assertRaisesRegex(ValueError,'^private_state_probe_execution_failed$'):
                    bootstrap.start(paths)

    def test_stop_marker_is_removed_after_old_control_capability(self):
        order=[]
        run=SimpleNamespace(is_file=lambda:True,unlink=lambda:order.append('run'))
        cap=SimpleNamespace(unlink=lambda **kwargs:order.append('cap'))
        with patch.object(installation,'read_json',return_value={'instance_id':'owned'}):
            bootstrap.cleanup_server(SimpleNamespace(run=run,cap=cap),'owned',None)
        self.assertEqual(order,['cap','run'])

    def test_controller_only_explicit_refusal_is_stale_other_failures_stay_closed(self):
        paths=SimpleNamespace(run=SimpleNamespace(is_file=lambda:True),service=object(),helper=object(),
                              cap=SimpleNamespace(read_text=lambda **kwargs:'a'*43))
        record={'port':8765,'instance_id':'synthetic'}
        cases=[(ConnectionRefusedError(),None),(TimeoutError(),'controller_connection_timeout'),
               (PermissionError(),'controller_connection_denied'),(ConnectionResetError(),'controller_connection_reset'),
               (http.client.BadStatusLine('private data'),'controller_protocol_failed'),(OSError('private data'),'controller_connection_failed')]
        for error,expected in cases:
            with patch.object(bootstrap.platform,'private',return_value=True),patch.object(installation,'read_json',return_value=record), \
                 patch.object(bootstrap.http.client,'HTTPConnection') as connection:
                connection.return_value.request.side_effect=error
                if expected:
                    with self.assertRaisesRegex(ValueError,'^'+expected+'$'):bootstrap.controller(paths)
                else:self.assertIsNone(bootstrap.controller(paths))
                connection.assert_called_once_with('127.0.0.1',8765,timeout=3)
                self.assertEqual(connection.return_value.request.call_count,1)
                connection.return_value.close.assert_called_once()

    def test_detached_start_passes_explicit_claude_and_state_roots(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            paths=installation.Paths(Path(directory)/'user',Path(directory)/'config',Path(directory)/'state')
            installed={'python':sys.executable,'port':8765,'otel_receiver':False,'revision':'fixture'}
            live={'instance_id':'fixture','url':'http://127.0.0.1:8765','revision':'fixture'}
            def child(command,**kwargs):
                identity=command[-1];live['instance_id']=identity
                installation.atomic(paths.service/'ready.json',installation.encoded({'instance_id':identity,'ready':True}))
                self.assertEqual(command[command.index('--claude-home')+1],str(paths.claude))
                self.assertEqual(command[command.index('--state-dir')+1],str(paths.state))
                self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)
                self.assertTrue(kwargs.get('start_new_session') or kwargs.get('creationflags'))
                return SimpleNamespace(wait=lambda **kwargs:0)
            with patch.object(installation,'receipt',return_value=installed),patch.object(installation,'integrity',return_value=[]), \
                 patch.object(bootstrap,'controller',side_effect=[None,live]),patch.object(bootstrap.socket,'socket'), \
                 patch.object(bootstrap.subprocess,'Popen',side_effect=child):
                result=bootstrap.start(paths)
                self.assertTrue(result['running']);self.assertTrue(result['changed'])
            bootstrap._children.pop(live['instance_id'],None)

    def test_open_without_browser_returns_only_expiring_private_handoff(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            paths=installation.Paths(Path(directory)/'user');paths.state.mkdir(parents=True)
            launch=paths.state/'.dashboard-launch-fixture.html';launch.write_text('synthetic')
            live={'instance_id':'fixture','url':'http://127.0.0.1:8765','launch_file':str(launch)}
            with patch.object(bootstrap,'start',return_value=live),patch.object(bootstrap,'controller',return_value=dict(live)), \
                 patch.object(bootstrap.webbrowser,'open') as browser:
                result=bootstrap.open_dashboard(paths,browser=False)
                self.assertEqual(result['expires_in_seconds'],300)
                self.assertFalse(result['browser_open_requested']);browser.assert_not_called()

    def test_user_home_and_state_overrides_are_fixture_scoped(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            base=Path(directory)
            with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(base/'config'),'CLAUDE_ROUTER_STATE_DIR':str(base/'state')}):
                paths=installation.Paths()
                self.assertEqual(paths.claude,base/'config');self.assertEqual(paths.state,base/'state')
                fixture=installation.Paths(base/'fixture-user')
                self.assertEqual(fixture.state,base/'fixture-user/.claude/routing')

    def test_doctor_does_not_read_credentials_or_run_model_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
            result=bootstrap.doctor(installation.Paths(Path(directory)/'user'))
        self.assertEqual(result['model_calls'],0);self.assertFalse(result['credentials_checked'])
        self.assertFalse(result['telemetry_configuration_modified'])


@unittest.skipUnless(sys.platform=='win32','Windows PowerShell entrypoint')
class WindowsRoutingEntrypoint(unittest.TestCase):
    def setUp(self):
        from test_routing_decision import packet, native_catalog
        self.temp=tempfile.TemporaryDirectory(prefix='routing-launcher-',dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.packet=self.root/'packet.json';self.packet.write_text(json.dumps(packet()))
        self.config=self.root/'config.json';self.config.write_text(json.dumps({'schema_version':1}))
        (self.root/'model-catalog.json').write_text(json.dumps(native_catalog()))

    def launch(self,*arguments):
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',
            str(ROOT/'scripts/Invoke-Routing.ps1'),'-PacketPath',str(self.packet),
            '-ConfigPath',str(self.config),'-PythonPath',sys.executable,*arguments],
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)
        if result.returncode and ('AuthorizationManager' in result.stderr or 'PSSecurityException' in result.stderr):
            if os.environ.get('CI'):self.fail('PowerShell file execution denied; no execution-policy bypass')
            self.skipTest('PowerShell file execution denied; no execution-policy bypass')
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_offline_route_uses_fixture_config_and_no_credentials(self):
        result=self.launch('-Offline')
        self.assertEqual(result['provenance']['used_provider'],'rules')
        self.assertEqual(result['recommendation']['id'],'sonnet_medium')

    def test_preflight_caps_do_not_load_an_explicit_invalid_credential(self):
        credential=self.root/'synthetic.dpapi';credential.write_text('invalid synthetic cipher')
        result=self.launch('-Provider','decisions','-AllowApi','-PreflightOnly',
            '-CredentialPath',str(credential),'-MaxDecisionInputTokens','1000')
        self.assertFalse(result['preflight']['may_load_credential'])
        self.assertEqual(result['preflight']['block_reason'],'verified_decisions_token_counter_unavailable')

    def test_single_candidate_does_not_decrypt_an_explicit_invalid_credential(self):
        credential=self.root/'synthetic.dpapi';credential.write_text('invalid synthetic cipher')
        override=self.root/'override.json';override.write_text(json.dumps({'model':'haiku','effort':'default'}))
        result=self.launch('-Provider','decisions','-AllowApi','-CredentialPath',str(credential),
            '-OverridesPath',str(override))
        self.assertEqual(result['provenance']['resolution'],'single_admissible_route')
        self.assertEqual(credential.read_text(),'invalid synthetic cipher')


if __name__=='__main__':unittest.main()
