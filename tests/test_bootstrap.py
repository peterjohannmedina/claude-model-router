"""Isolated user roots only. No keys, user config, autostart or browser is touched."""
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import hashlib
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import routing_bootstrap as bootstrap
import routing_installation as installation
import routing_platform as platform


class Fixture(unittest.TestCase):
    live_permissions=False
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='routing-otel-setup-bootstrap-',dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        if self.live_permissions:
            try: verified=platform.private(self.root,ROOT/'scripts/Test-RoutingTelemetryAcl.ps1')
            except ValueError as error:
                if os.environ.get('CI'):self.fail('Live privacy gate unavailable: '+str(error))
                self.skipTest('Live privacy gate unavailable: '+str(error))
            if not verified:
                if os.environ.get('CI'):self.fail('Live fixture requires independently verified private permissions')
                self.skipTest('Live fixture requires independently verified private permissions')
        else:
            # Pure transaction tests simulate the privacy boundary. This mock
            # never starts a receiver or changes an ACL or execution policy.
            self.privacy=patch.object(platform,'private',return_value=True)
            self.privacy.start();self.addCleanup(self.privacy.stop)
            def shortcut(path,body,helper,expected=None): path.write_bytes(installation.encoded(body))
            self.shortcuts=patch.object(platform,'windows_shortcut',side_effect=shortcut)
            self.shortcuts.start();self.addCleanup(self.shortcuts.stop)
        self.user=self.root/'user with spaces';self.user.mkdir(mode=0o700)
        self.paths=installation.Paths(self.user)

    def install(self,source=ROOT,**options):
        with installation.lock(self.paths):return installation.install(self.paths,source,**options)

    def archive(self,name='downloaded source'):
        source=self.root/name;source.mkdir()
        for name,body in installation.source_files(ROOT).items():
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        return source

    def backup_file(self,identifier,name):
        backup=self.paths.service/'backups'/identifier
        record=installation.read_json(backup/'originals.json')[str(installation.SKILL/name)]
        return backup/'files'/record['blob']


class InstallTransactions(Fixture):
    def test_clean_install_idempotent_and_user_state_preserved(self):
        home=self.paths.claude
        (home/'agents').mkdir(parents=True)
        (home/'agents/haiku-efficient.md').write_text('# personal profile\n')
        (home/'settings.json').write_text('# unrelated synthetic config\n')
        (home/'CLAUDE.md').write_text('Personal instructions\n')
        self.paths.state.mkdir()
        cipher=self.paths.state/'openai-key.dpapi';cipher.write_bytes(b'synthetic-unread-credential')
        original={p:p.read_bytes() for p in (home/'agents/haiku-efficient.md',home/'settings.json',home/'CLAUDE.md',cipher)}
        source=self.archive()
        result=self.install(source);self.assertTrue(result['changed'])
        receipt=installation.receipt(self.paths);backup=receipt['backup_id']
        self.assertTrue((self.paths.skill/'SKILL.md').is_file())
        self.assertFalse(self.paths.cap.exists());self.assertFalse(receipt['login_startup'])
        self.assertFalse(self.install(source)['changed'])
        self.assertEqual(installation.receipt(self.paths)['backup_id'],backup)
        self.assertTrue(all(p.read_bytes()==body for p,body in original.items()))
        self.assertTrue(all(installation.package_owned(name) for name in receipt['files']))
        self.assertTrue((home/installation.COMPANION/'SKILL.md').is_file())

    def test_update_rollback_and_uninstall_preserve_user_profiles_and_history(self):
        self.install();original=(self.paths.skill/'README.md').read_bytes()
        profile=self.paths.claude/'agents/sonnet-general.md';profile.write_text('# customized after install\n')
        history=self.paths.state/'usage.sqlite3';history.write_bytes(b'synthetic-history-no-open')
        source=self.archive();(source/'VERSION').write_text('2099.1\n');(source/'README.md').write_bytes(original+b'\nnew fixture release\n')
        self.assertTrue(self.install(source)['changed'])
        self.assertNotEqual((self.paths.skill/'README.md').read_bytes(),original)
        with installation.lock(self.paths):self.assertTrue(installation.rollback(self.paths)['rolled_back'])
        self.assertEqual((self.paths.skill/'README.md').read_bytes(),original)
        with installation.lock(self.paths):result=installation.uninstall(self.paths)
        self.assertTrue(result['uninstalled']);self.assertFalse(self.paths.receipt.exists())
        self.assertEqual(profile.read_text(),'# customized after install\n')
        self.assertEqual(history.read_bytes(),b'synthetic-history-no-open')
        self.assertFalse(installation.uninstall(self.paths)['changed'])

    def test_modified_package_requires_repair_and_keeps_recovery_copy(self):
        self.install();readme=self.paths.skill/'README.md';readme.write_bytes(b'synthetic customized package file')
        with self.assertRaises(ValueError):self.install()
        result=self.install(repair=True)
        backup=self.backup_file(result['backup_id'],'README.md')
        self.assertEqual(backup.read_bytes(),b'synthetic customized package file')
        self.assertEqual(readme.read_bytes(),(ROOT/'README.md').read_bytes())

    def test_uninstall_keeps_modified_and_untracked_package_files(self):
        self.install();readme=self.paths.skill/'README.md';readme.write_bytes(b'user edit')
        extra=self.paths.skill/'personal-notes.txt';extra.write_text('user data')
        with installation.lock(self.paths):result=installation.uninstall(self.paths)
        self.assertIn(str(installation.SKILL/'README.md'),result['preserved_modified_files'])
        self.assertEqual(readme.read_bytes(),b'user edit');self.assertEqual(extra.read_text(),'user data')

    def test_companion_skill_is_guarded_owned_and_removed_with_package(self):
        companion=self.paths.claude/installation.COMPANION/'SKILL.md'
        companion.parent.mkdir(parents=True);companion.write_bytes(b'personal existing companion')
        with self.assertRaisesRegex(ValueError,'existing_unmanaged_skill_requires_review'):self.install()
        source=self.archive();self.install(source,adopt=True)
        installed=installation.receipt(self.paths)
        self.assertIn(str(installation.COMPANION/'SKILL.md'),installed['files'])
        self.assertEqual(companion.read_bytes(),(ROOT/'skills/claude-routing/SKILL.md').read_bytes())
        with installation.lock(self.paths):installation.rollback(self.paths)
        self.assertEqual(companion.read_bytes(),b'personal existing companion')
        self.install(source,adopt=True)
        with installation.lock(self.paths):installation.uninstall(self.paths)
        self.assertFalse(companion.exists())

    def test_archive_excludes_credentials_and_runtime_state(self):
        source=self.archive()
        for name in ('scripts/synthetic.dpapi','scripts/.env','scripts/usage.sqlite3','assets/.dashboard-launch-fixture.html'):
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'private fixture')
        self.install(source)
        for name in ('scripts/synthetic.dpapi','scripts/.env','scripts/usage.sqlite3','assets/.dashboard-launch-fixture.html'):
            self.assertFalse((self.paths.skill/name).exists())

    def test_custom_config_and_state_paths_are_independent(self):
        config=self.root/'custom config';state=self.root/'private service state'
        paths=installation.Paths(self.user,config,state)
        with installation.lock(paths):installation.install(paths,ROOT)
        self.assertEqual(paths.skill,config/installation.SKILL)
        self.assertEqual(paths.service,state/'dashboard-service')
        self.assertFalse((self.user/'.claude').exists())

    def test_downloaded_archive_and_explicit_legacy_adoption_rollback(self):
        source=self.archive();self.paths.skill.mkdir(parents=True)
        old=self.paths.skill/'README.md';old.write_bytes(b'previous unmanaged public package')
        with self.assertRaises(ValueError):self.install(source)
        self.install(source,adopt=True)
        with installation.lock(self.paths):installation.rollback(self.paths)
        self.assertEqual(old.read_bytes(),b'previous unmanaged public package')
        self.assertFalse(self.paths.receipt.exists())

    def test_changed_backup_refuses_rollback(self):
        self.install();source=self.archive();(source/'VERSION').write_text('2099.2\n')
        self.install(source);receipt=installation.receipt(self.paths)
        backup=self.backup_file(receipt['backup_id'],'README.md')
        backup.write_bytes(b'synthetic tampered backup')
        with self.assertRaises(ValueError):installation.rollback(self.paths)
        self.assertEqual(installation.receipt(self.paths)['version'],'2099.2')

    def test_rollback_refuses_recreated_removed_package_path_before_any_changes(self):
        source=self.archive();retired=source/'scripts/retired_fixture.py'
        retired.write_bytes(b'old release bytes');self.install(source)
        original=(self.paths.skill/'README.md').read_bytes()
        retired.unlink();(source/'README.md').write_bytes(original+b'new release bytes')
        self.install(source)
        recreated=self.paths.skill/'scripts/retired_fixture.py'
        self.assertFalse(recreated.exists());recreated.write_bytes(b'user-created bytes after update')
        before=self.paths.receipt.read_bytes()
        with self.assertRaisesRegex(ValueError,'recreated_path_rollback_refused'):
            installation.rollback(self.paths)
        self.assertEqual(recreated.read_bytes(),b'user-created bytes after update')
        self.assertEqual(self.paths.receipt.read_bytes(),before)
        self.assertEqual((self.paths.skill/'README.md').read_bytes(),original+b'new release bytes')
        recreated.unlink();self.assertTrue(installation.rollback(self.paths)['rolled_back'])
        self.assertEqual(recreated.read_bytes(),b'old release bytes')

    def test_unchanged_owned_launchers_refresh_after_interpreter_change(self):
        self.install(shortcuts=True,login=True)
        before=installation.receipt(self.paths)
        bodies={name:Path(name).read_bytes() for name in before['shortcut_files']}
        # The alternate interpreter is only a launcher target, never executed.
        alternate=str(self.root/'alternate python'/'python.exe')
        with patch.object(installation.sys,'executable',alternate):
            result=self.install()
            self.assertTrue(result['changed']);self.assertTrue(result['login_startup_configured'])
            self.assertFalse(self.install()['changed'])
        installed=installation.receipt(self.paths)
        self.assertEqual(installed['python'],alternate)
        self.assertTrue(all(Path(name).read_bytes()!=body for name,body in bodies.items()))
        for path,kind,body in platform.launcher_specs(self.user,self.paths.claude,alternate,True,True,state=self.paths.state):
            self.assertEqual(installed['shortcut_specs'][str(path.absolute())],installation.shortcut_spec(kind,body))
        # Edits still win over an interpreter change, and startup is not claimed.
        login_path=platform.launcher_specs(self.user,self.paths.claude,alternate,False,True,state=self.paths.state)[0][0]
        login_path.write_bytes(b'personal launcher edit')
        self.assertFalse(self.install()['login_startup_configured'])
        self.assertEqual(login_path.read_bytes(),b'personal launcher edit')

    def test_shortcuts_and_login_are_explicit_fixture_only_and_removed(self):
        self.install(shortcuts=True,login=True)
        receipt=installation.receipt(self.paths)
        self.assertEqual(receipt.get('shortcut_errors'),[])
        self.assertTrue(receipt['shortcut_files'])
        self.assertTrue(all(Path(name).is_relative_to(self.user) for name in receipt['shortcut_files']))
        owned=list(receipt['shortcut_files'])
        self.install(shortcuts=False,login=False)
        self.assertTrue(all(not Path(name).exists() for name in owned))

    def test_conflicting_login_launcher_is_preserved_and_not_reported_configured(self):
        specs=platform.launcher_specs(self.user,self.paths.claude,sys.executable,False,True,state=self.paths.state)
        path=specs[0][0];path.parent.mkdir(parents=True);path.write_bytes(b'synthetic unrelated launcher')
        result=self.install(login=True)
        self.assertTrue(result['login_startup_requested']);self.assertFalse(result['login_startup_configured'])
        self.assertTrue(result['shortcut_conflicts']);self.assertEqual(path.read_bytes(),b'synthetic unrelated launcher')
        self.assertFalse(bootstrap.status(self.paths)['login_startup_configured'])

    def test_unverified_private_state_blocks_install(self):
        with patch.object(platform,'private',return_value=False):
            with self.assertRaises(ValueError):self.install()
        self.assertFalse(self.paths.receipt.exists());self.assertFalse(self.paths.cap.exists())

    def test_repository_state_is_refused_before_creating_control_material(self):
        (self.user/'.git').mkdir()
        with self.assertRaises(ValueError):self.install()
        self.assertFalse(self.paths.cap.exists());self.assertFalse(self.paths.receipt.exists())

    def test_owned_path_escape_and_reparse_are_refused(self):
        for name in ('../outside','/outside'):
            with self.assertRaises(ValueError):self.paths.target(name)
        if os.name!='nt':
            self.paths.claude.mkdir();(self.paths.claude/'skills').symlink_to(self.root,target_is_directory=True)
            with self.assertRaises(ValueError):self.paths.target('skills/claude-model-routing/file')

    def test_platform_adapter_arguments_and_browser_bookmarks_are_not_edited(self):
        arguments=['/path with spaces/python','literal%file','$HOME','quote"here']
        value=platform.desktop_exec(arguments)
        self.assertIn('%%',value);self.assertIn('\\\\$',value)
        for target in ('darwin','linux','win32'):
            specs=platform.launcher_specs(self.user,self.paths.claude,sys.executable,True,True,platform=target)
            self.assertTrue(specs)
            self.assertTrue(all(Path(p).is_relative_to(self.user) for p,_,_ in specs))

    def _assert_supported_platform_wrapper_installs_in_isolated_root(self):
        if os.name=='nt':
            command=['powershell.exe','-NoProfile','-File',str(ROOT/'scripts/bootstrap.ps1'),
                '-Command','install','-PythonPath',sys.executable,'-UserRoot',str(self.user),'-SourceRoot',str(ROOT)]
        else:command=['sh',str(ROOT/'scripts/bootstrap.sh'),'install','--user-root',str(self.user),'--source-root',str(ROOT)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=30,
                              env={**os.environ,'CLAUDE_ROUTING_PYTHON':sys.executable})
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(json.loads(result.stdout)['installed'])
        self.assertFalse(self.paths.run.exists());self.assertFalse(self.paths.cap.exists())

    def test_receiver_provisioning_never_reads_or_edits_claude_settings(self):
        installation.prepare(self.paths)
        token='synthetic-private-exporter-only-for-fixture-123456789'
        cap=self.paths.state/'otel-receiver.capability'
        installation.atomic(cap,(token+'\n').encode())
        config=self.paths.claude/'settings.json';installation.atomic(config,b'{"unrelated":true}')
        prepared={'endpoint':'http://127.0.0.1:8765/v1/logs',
            'capability_sha256':hashlib.sha256(cap.read_bytes()).hexdigest()}
        installation.atomic(self.paths.state/'otel-setup-receipt.json',installation.encoded(prepared))
        self.assertEqual(bootstrap.approved_otel(self.paths,8765),token)
        self.assertEqual(config.read_bytes(),b'{"unrelated":true}')
        with self.assertRaises(ValueError):bootstrap.approved_otel(self.paths,8766)
        installation.atomic(cap,b'changed')
        with self.assertRaises(ValueError):bootstrap.approved_otel(self.paths,8765)

    def test_blocked_control_does_not_report_stopped_or_bypass_permissions(self):
        self.install()
        with patch.object(bootstrap,'controller',side_effect=PermissionError('synthetic blocked')):
            result=bootstrap.status(self.paths)
        self.assertIsNone(result['running']);self.assertIn('blocked',result['lifecycle_access'])


@unittest.skipIf(os.environ.get('ROUTING_SKIP_SOCKET_TESTS')=='1' and not os.environ.get('CI'),'Restricted local HTTP; CI exercises detached lifecycle')
class RealLifecycle(Fixture):
    live_permissions=True
    def setUp(self):
        super().setUp();self.release=self.archive('fixture release');self.install(self.release,port=0)
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        if bootstrap.controller(self.paths) is None:return
        if os.name!='nt':bootstrap.stop(self.paths);return
        # Wait-only process handle: authenticated stop never kills a stale PID.
        # Windows can retain a child's cwd briefly after run.json is removed.
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.OpenProcess(0x00100000,False,installation.read_json(self.paths.run)['pid'])
        if not handle:raise ctypes.WinError(ctypes.get_last_error())
        try:
            bootstrap.stop(self.paths)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0,'Fixture receiver must exit before root cleanup')
        finally:kernel.CloseHandle(handle)

    def test_clean_detached_start_reopen_identity_stop_and_update(self):
        with installation.lock(self.paths):first=bootstrap.start(self.paths)
        self.assertTrue(first['running'])
        with installation.lock(self.paths):again=bootstrap.start(self.paths)
        self.assertEqual(first['instance_id'],again['instance_id']);self.assertFalse(again['changed'])
        with installation.lock(self.paths):opened=bootstrap.open_dashboard(self.paths,browser=False)
        launch=Path(opened['private_launch_file']);self.assertTrue(launch.is_file())
        nonce=launch.read_text().split('/#authorize=',1)[1].split('"',1)[0]
        port=installation.read_json(self.paths.run)['port'];connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        origin='http://127.0.0.1:'+str(port)
        connection.request('POST','/api/auth','{}',{'Origin':origin,'Content-Type':'application/json','X-Routing-Bootstrap':nonce})
        response=connection.getresponse();response.read();connection.close()
        self.assertEqual(response.status,200);self.assertFalse(launch.exists())
        with installation.lock(self.paths):reopened=bootstrap.open_dashboard(self.paths,browser=False)
        self.assertEqual(first['instance_id'],reopened['instance_id'])
        self.assertTrue(Path(reopened['private_launch_file']).is_file())
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(bootstrap.main(['install','--user-root',str(self.user),'--source-root',str(self.release)]),0)
        self.assertEqual(bootstrap.status(self.paths)['instance_id'],first['instance_id'])
        source=self.archive();(source/'VERSION').write_text('2099.3\n')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(['update','--user-root',str(self.user),'--source-root',str(source)]),0)
        self.assertNotEqual(bootstrap.status(self.paths)['instance_id'],first['instance_id'])
        with installation.lock(self.paths):self.assertTrue(bootstrap.stop(self.paths)['stopped'])
        self.assertFalse(self.paths.run.exists());self.assertFalse(self.paths.cap.exists())

    def test_supported_wrapper_installs_without_starting(self):
        InstallTransactions._assert_supported_platform_wrapper_installs_in_isolated_root(self)

    def test_stale_closed_port_restarts_without_replacing_unverified_listener(self):
        first=bootstrap.start(self.paths)
        run=self.paths.run.read_bytes();cap=self.paths.cap.read_bytes()
        self.cleanup_server()
        installation.atomic(self.paths.run,run);installation.atomic(self.paths.cap,cap)
        restarted=bootstrap.start(self.paths)
        self.assertTrue(restarted['running'])
        self.assertNotEqual(first['instance_id'],restarted['instance_id'])
        self.assertEqual(bootstrap.status(self.paths)['instance_id'],restarted['instance_id'])

    def test_unknown_port_process_never_killed(self):
        import socket
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();self.addCleanup(sock.close)
        receipt=installation.receipt(self.paths);receipt['port']=sock.getsockname()[1]
        installation.atomic(self.paths.receipt,installation.encoded(receipt))
        with installation.lock(self.paths):
            with self.assertRaises(ValueError):bootstrap.start(self.paths)
        self.assertFalse(self.paths.cap.exists())

    def test_command_exit_does_not_stop_background_server(self):
        result=subprocess.run([sys.executable,'-B',str(self.paths.skill/'scripts/routing_bootstrap.py'),
            'start','--user-root',str(self.user)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(json.loads(result.stdout)['running'])
        self.assertTrue(bootstrap.status(self.paths)['running'])

    def test_native_login_job_hosts_foreground_worker_until_authenticated_stop(self):
        process=subprocess.Popen([sys.executable,'-B',str(self.paths.skill/'scripts/routing_bootstrap.py'),
            '_login','--user-root',str(self.user)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,**platform.detached())
        import time
        deadline=time.monotonic()+15;live=None
        while time.monotonic()<deadline:
            if self.paths.run.exists():
                live=bootstrap.controller(self.paths)
                if live:break
            time.sleep(.05)
        self.assertIsNotNone(live);self.assertIsNone(process.poll())
        bootstrap.stop(self.paths)
        self.assertEqual(process.wait(timeout=5),0)


if __name__=='__main__':unittest.main()
