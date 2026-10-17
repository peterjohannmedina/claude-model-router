from dataclasses import asdict, replace
from contextlib import closing
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import routing_dashboard as dashboard
import routing_runtime as runtime
import routing_state as state


def event(**kwargs):
    return {'source': 'router', 'session_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()), **kwargs}


class LocalState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.root = Path(self.temp.name)
        self.ledger = state.Ledger(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_numeric_loopback_bind_never_resolves_host_dns(self):
        server=object.__new__(dashboard.Server)
        def bind_only(instance):instance.server_address=('127.0.0.1',49152)
        with patch.object(dashboard.TCPServer,'server_bind',bind_only), patch('socket.getfqdn',side_effect=AssertionError('DNS forbidden')):
            server.server_bind()
        self.assertEqual((server.server_name,server.server_port),('localhost',49152))

    def test_receiver_diagnostics_persist_only_fixed_counters_without_socket(self):
        store=dashboard.Store(self.root)
        server=object.__new__(dashboard.Server)
        server.store=store;server.otel_lock=threading.Lock();server.last_status_write=0
        server.record_otel(received=True,validated_batches=1,unsupported_version_records=2)
        persisted=json.loads((self.root/'receiver-status.json').read_text())
        self.assertEqual(persisted['unsupported_version_records'],2)
        self.assertEqual(persisted['accepted_records'],0)
        self.assertIsNone(persisted['last_observation_at'])
        self.assertEqual(persisted['supported_cli_versions'],[])
        self.assertEqual(store.snapshot()['sessions'],[])
        for private in ('conversation.id','session_id','capability','headers','prompt','body',str(self.root)):
            self.assertNotIn(private,json.dumps(persisted))
        with self.assertRaises(ValueError):server.record_otel(unknown_counter=1)

    def test_dedup_late_usage_and_unknown_are_distinct_from_zero(self):
        item = event(stage=1)
        self.assertTrue(self.ledger.put(item))
        initial = self.ledger.snapshot()[0]
        self.assertFalse(self.ledger.put(item))
        self.assertEqual(state.totals(self.ledger.snapshot())['downstream']['input_tokens']['unknown_requests'], 1)
        item.update(stage=2, execution_status='completed', downstream_usage={'input_tokens': 0})
        self.ledger.put(item)
        item['downstream_usage'] = {'output_tokens': 9}
        self.ledger.put(item)
        rows = self.ledger.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['downstream_usage'], {'input_tokens': 0, 'output_tokens': 9, 'cached_input_tokens': None})
        self.assertEqual(rows[0]['first_observed_at'], initial['first_observed_at'])
        self.assertEqual(state.totals(rows)['downstream']['input_tokens'], {'known': 0, 'unknown_requests': 0})
        self.assertFalse(self.ledger.put({**item, 'stage': 0, 'downstream_usage': {'input_tokens': 50}}))

    def test_running_revision_compares_content_identity_not_receipt_mtime(self):
        store=dashboard.Store(self.root)
        installed=self.root/'dashboard-service'/'installation.json';installed.parent.mkdir()
        revision='2026.10.16.2+0123456789abcdef'
        store.otel_status['running_revision']=revision
        installed.write_text(json.dumps({'revision':revision}))
        self.assertFalse(store.runtime_identity()['restart_required'])
        installed.write_text(json.dumps({'revision':'2026.10.16.2+fedcba9876543210'}))
        self.assertTrue(store.runtime_identity()['restart_required'])
        installed.unlink()
        self.assertIsNone(store.runtime_identity()['restart_required'])

    def test_metadata_allowlist_excludes_sensitive_payloads(self):
        secret = 'sk-proj-' + 'A' * 30
        self.ledger.put(event(prompt=secret, text=secret, endpoint='http://user:' + secret + '@host',
                              model=secret, override_model=secret, fallback_reason=secret, usage={'secret': secret}))
        raw = self.ledger.path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        stored = self.ledger.snapshot()[0]
        self.assertIsNone(stored['model'])
        self.assertNotIn('prompt', stored)
        self.assertNotIn('endpoint', stored)

    def test_final_admission_error_clears_stale_recommendation(self):
        item = event(stage=0, model='sonnet', fallback_reason='offline')
        self.ledger.put(item)
        self.ledger.put({**item, 'stage':2, 'model':None, 'fallback_reason':None, 'error':'require_local_unavailable'})
        stored = self.ledger.snapshot()[0]
        self.assertIsNone(stored['model'])
        self.assertIsNone(stored['fallback_reason'])
        self.assertEqual(stored['error'],'require_local_unavailable')

    def test_read_path_reapplies_allowlist(self):
        self.ledger.put(event())
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            identity, body = connection.execute('SELECT id,body FROM events').fetchone()
            value = json.loads(body)
            value['arbitrary_prompt'] = 'never serve this'
            connection.execute('UPDATE events SET body=? WHERE id=?', (json.dumps(value), identity))
            connection.commit()
        self.assertNotIn('arbitrary_prompt', self.ledger.snapshot()[0])

    def test_retention_disable_clear_and_cost_evidence(self):
        settings = state.LedgerSettings(max_events=10)
        self.ledger.settings = settings
        for _ in range(12): self.ledger.put(event())
        self.assertEqual(len(self.ledger.snapshot()), 10)
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            connection.execute('UPDATE events SET updated=0 WHERE id=(SELECT id FROM events LIMIT 1)')
            connection.commit()
        self.assertEqual(len(self.ledger.snapshot()), 9)
        self.ledger.settings = replace(settings, enabled=False)
        self.assertFalse(self.ledger.put(event()))
        sample = event(adapter='local', evaluator_usage={'input_tokens': 100, 'output_tokens': 0},
                       downstream_usage={'input_tokens': 20, 'output_tokens': 10})
        totals = state.totals([sample], state.LedgerSettings(local_input_usd_per_million=1, local_output_usd_per_million=2))
        self.assertAlmostEqual(totals['evaluator_base_cost_estimate_usd'], 0.00001)
        self.assertAlmostEqual(totals['local_cost_estimate_usd'], 0.00004)
        self.assertIsNone(totals['measured_billed_cost_usd'])
        self.assertIsNone(state.totals([sample])['local_cost_estimate_usd'])
        self.ledger.clear()
        self.assertEqual(self.ledger.snapshot(), [])

    def test_hook_metadata_never_becomes_execution_or_usage(self):
        hook = event(source='claude_hook', session_source='claude_thread', mode='hook_stop',
            execution_status='stop_observed', model_source='hook_metadata', model='sonnet[1m]',
            contract_validation='documented_hook_schema', downstream_attempts=1,
            downstream_usage={'input_tokens': 1000}, evaluator_usage={'input_tokens': 500},
            transcript_path='private-transcript', last_assistant_message='private-response')
        self.ledger.put(hook)
        row = self.ledger.snapshot()[0]
        self.assertEqual(row['model'], 'sonnet[1m]')
        self.assertIsNone(row['downstream_usage'])
        self.assertIsNone(row['evaluator_usage'])
        self.assertIsNone(row['downstream_attempts'])
        self.assertNotIn('private-response', self.ledger.path.read_bytes().decode(errors='ignore'))
        totals = state.totals([row])
        self.assertEqual(totals['requests'], 0)
        self.assertEqual(totals['completed_executions'], 0)
        self.assertEqual(totals['hooks'], {'sessions': 1, 'observations': 1, 'stops': 1})
        invalid = {**hook, 'execution_status': 'completed'}
        with self.assertRaises(ValueError): state.sanitize(invalid)

    def test_state_directory_override_is_independent_of_native_config(self):
        with patch.dict(os.environ, {'CLAUDE_ROUTER_STATE_DIR': str(self.root)}):
            self.assertEqual(state.state_root(), self.root)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root)}, clear=True):
            self.assertEqual(state.state_root(), self.root / 'routing')

    def test_settings_validation(self):
        for bad in ({'enabled': 1}, {'retention_days': 0}, {'max_events': 100000}, {'api_key': 'no'},
                    {'local_input_usd_per_million': float('nan')}):
            with self.assertRaises(ValueError): state.LedgerSettings.parse(bad)

    def test_request_and_stream_ids_require_uuid_and_malformed_rows_fail_closed(self):
        for malformed in (None, [], {'source':'router'}, event(request_id='unassigned')):
            with self.assertRaises(ValueError): state.sanitize(malformed)
        self.assertEqual(state.identifier('unassigned'),'unassigned')

    def test_config_cas_and_toggles_do_not_contact_providers(self):
        store = dashboard.Store(self.root)
        config, version = store.config()
        config['decisions']['enabled'] = True
        config['local'].update(enabled=True, base_url='http://127.0.0.1:9/v1', model='example-model')
        with patch.object(runtime, 'local_request', side_effect=AssertionError('toggle probed')), \
             patch.object(runtime.core.DecisionsProvider, 'choose', side_effect=AssertionError('toggle evaluated')):
            saved, new_version = store.save(config, version)
        self.assertTrue(saved['decisions']['enabled'])
        self.assertNotEqual(version, new_version)
        with self.assertRaises(FileExistsError): store.save(config, version)
        for field, value in (('base_url', 'http://user:password@127.0.0.1/v1'), ('model', 'sk-proj-' + 'A' * 30)):
            bad = json.loads(json.dumps(config)); bad['local'][field] = value
            with self.assertRaises(ValueError): store.save(bad, new_version)
        self.assertFalse((self.root / 'usage.sqlite3').exists())

    def test_store_rejects_repository_state(self):
        with self.assertRaises(ValueError): dashboard.Store(ROOT / 'routing-test-state')

    def test_browser_bootstrap_is_single_use_and_expires(self):
        server = object.__new__(dashboard.Server)  # Authorization logic needs no socket.
        server.bootstrap='synthetic-bootstrap'; server.bootstrap_lock=threading.Lock()
        server.bootstrap_used=False; server.bootstrap_until=state.time.monotonic()+10; server.launch_path=None
        self.assertFalse(server.authorize('wrong'))
        self.assertTrue(server.authorize('synthetic-bootstrap'))
        self.assertFalse(server.authorize('synthetic-bootstrap'))
        server.bootstrap_used=False; server.bootstrap_until=0
        self.assertFalse(server.authorize('synthetic-bootstrap'))



@unittest.skipIf(os.environ.get('ROUTING_SKIP_SOCKET_TESTS') == '1', 'Local socket restrictions; CI runs HTTP integration')
class DashboardHTTP(unittest.TestCase):
    def test_catalog_refresh_requires_mutating_authorization_and_never_runs_on_poll(self):
        with patch.object(dashboard.catalog,'refresh',return_value={'status':'queried','models':[],'entitlement':'unverified'}) as refresh:
            self.assertEqual(self.authenticated('GET','/api/state')[0].status,200)
            refresh.assert_not_called()
            self.assertEqual(self.request('POST','/api/models/refresh','{}',{'Content-Type':'application/json'})[0].status,403)
            self.assertEqual(self.authenticated('POST','/api/models/refresh',{},**{'Origin':'null'})[0].status,403)
            refresh.assert_not_called()
            response,body=self.authenticated('POST','/api/models/refresh',{})
            self.assertEqual(response.status,200);self.assertEqual(json.loads(body)['model_catalog']['entitlement'],'unverified')
            self.assertEqual(refresh.call_count,1)
            self.assertEqual(self.authenticated('POST','/api/models/refresh',{'command':'not accepted'})[0].status,400)
            self.assertEqual(refresh.call_count,1)
        with patch.object(dashboard.catalog,'refresh',side_effect=dashboard.catalog.CatalogError('catalog_refresh_timeout')):
            response,body=self.authenticated('POST','/api/models/refresh',{})
            self.assertEqual(response.status,503);self.assertEqual(json.loads(body)['reason'],'catalog_refresh_timeout')
            _,body=self.authenticated('GET','/api/state')
            self.assertEqual(json.loads(body)['model_catalog']['status'],'error')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        try: self.server = dashboard.Server(0, dashboard.Store(self.root))
        except PermissionError:
            if os.environ.get('ROUTING_REQUIRE_HTTP_TESTS') == '1': raise
            self.skipTest('Loopback sockets unavailable in this environment')
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 1)
        self.addCleanup(self.server.shutdown)
        try: response, body = self.request('GET', '/')
        except PermissionError:
            if os.environ.get('ROUTING_REQUIRE_HTTP_TESTS') == '1': raise
            self.skipTest('Loopback connections denied; CI verifies real HTTP')
        self.assertIsNone(response.getheader('Set-Cookie'))
        self.assertIn(b'Routing desk', body)
        self.assertNotIn(self.server.bootstrap.encode(),body)
        self.assertEqual(self.request('GET','/api/session')[0].status,403)
        launch = self.server.create_launch_file()
        self.assertTrue(launch.is_file())
        self.assertIn('/#authorize=',launch.read_text())
        if os.name != 'nt': self.assertEqual(launch.stat().st_mode & 0o777, 0o600)
        response, body = self.request('POST','/api/auth','{}',{'Origin':self.server.origin,
            'Content-Type':'application/json','X-Routing-Bootstrap':self.server.bootstrap})
        self.assertEqual(response.status,200)
        self.assertFalse(launch.exists())
        self.csrf = json.loads(body)['csrf']
        self.cookie = response.getheader('Set-Cookie').split(';')[0]
        self.assertIn('HttpOnly; SameSite=Strict', response.getheader('Set-Cookie'))
        response, body = self.request('GET', '/api/session', headers={'Cookie':self.cookie,'X-Routing-CSRF':self.csrf})
        self.csrf = json.loads(body)['csrf']

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse(); data = response.read(); connection.close()
        return response, data

    def authenticated(self, method, path, body=None, **extra):
        headers={'Cookie':self.cookie, 'Origin':self.server.origin, 'X-Routing-CSRF':self.csrf, 'Content-Type':'application/json', **extra}
        return self.request(method, path, json.dumps(body) if body is not None else None, headers)

    def test_picker_import_requires_browser_authorization_and_explicit_confirmation(self):
        payload = {'confirmed': True, 'source': 'claude_code_model_picker', 'provider': 'claude_ai',
                   'models': [{'id': 'sonnet', 'efforts': ['low', 'medium', 'high'], 'default_effort': 'medium'}]}
        self.assertEqual(self.request('POST', '/api/catalog/import', json.dumps(payload),
            {'Content-Type': 'application/json'})[0].status, 403)
        self.assertEqual(self.authenticated('POST', '/api/catalog/import', payload,
            **{'X-Routing-CSRF': ''})[0].status, 403)
        self.assertEqual(self.authenticated('POST', '/api/catalog/import', {**payload, 'confirmed': False})[0].status, 400)
        response, raw = self.authenticated('POST', '/api/catalog/import', payload)
        self.assertEqual(response.status, 200)
        catalog = json.loads(raw)['model_catalog']
        self.assertEqual(catalog['entitlement'], 'user_confirmed')
        self.assertFalse(catalog['scope_verified'])
        self.assertEqual(catalog['models'][0]['id'], 'sonnet')
        self.assertFalse((self.root / 'usage.sqlite3').exists())
        self.assertEqual(self.authenticated('POST', '/api/models/refresh', {})[0].status, 503)
        self.assertEqual(self.authenticated('POST', '/api/catalog/import',
            {**payload, 'api_key': 'sk-synthetic-do-not-echo'})[0].status, 400)
        self.assertNotIn(b'sk-synthetic', raw)

    def test_hook_endpoint_uses_separate_capability_and_separate_totals(self):
        self.server.otel_capability = 'A' * 43
        self.server.store.otel_status['enabled'] = True
        payload = {'hook_event_name': 'Stop', 'session_id': str(uuid.uuid4()), 'stop_hook_active': False,
                   'last_assistant_message': 'private assistant message', 'transcript_path': 'private-path'}
        headers = {'Content-Type': 'application/json', dashboard.otel.HEADER: self.server.otel_capability}
        self.assertEqual(self.authenticated('POST', '/api/hooks', payload)[0].status, 403)
        self.assertEqual(self.request('POST', '/api/hooks', json.dumps(payload),
            {**headers, 'Origin': self.server.origin})[0].status, 403)
        response, raw = self.request('POST', '/api/hooks', json.dumps(payload), headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {})
        snapshot = json.loads(self.authenticated('GET', '/api/state')[1])
        self.assertEqual(snapshot['totals']['requests'], 0)
        self.assertEqual(snapshot['totals']['completed_executions'], 0)
        self.assertEqual(snapshot['totals']['hooks']['stops'], 1)
        self.assertEqual(snapshot['events'][0]['execution_status'], 'stop_observed')
        self.assertNotIn('private assistant message', json.dumps(snapshot))
        self.assertNotIn('private-path', json.dumps(snapshot))

    def test_origin_host_session_csrf_and_size_guards(self):
        self.assertEqual(self.request('GET','/api/state')[0].status,403)
        self.assertEqual(self.request('GET','/api/session',headers={'Cookie':self.cookie})[0].status,403)
        self.assertEqual(self.request('GET','/api/state',headers={'Cookie':self.cookie})[0].status,403)
        for headers in ({'Host':'evil.test'}, {'Origin':'https://evil.test'}):
            self.assertEqual(self.request('GET','/',headers=headers)[0].status,403)
        self.assertEqual(self.authenticated('GET','/api/state',**{'Sec-Fetch-Site':'cross-site'})[0].status,403)
        # Static root navigation is public but grants no session/data access.
        self.assertEqual(self.request('GET','/',headers={'Sec-Fetch-Site':'cross-site'})[0].status,200)
        self.assertEqual(self.request('POST','/api/auth','{}',{'Origin':self.server.origin,
            'Content-Type':'application/json','X-Routing-Bootstrap':self.server.bootstrap})[0].status,403)
        response, data = self.authenticated('GET','/api/state')
        self.assertEqual(response.status,200)
        payload={'config':json.loads(data)['config'], 'version':json.loads(data)['config_version']}
        for extra in ({'Origin':'null'}, {'X-Routing-CSRF':''}, {'Host':'localhost:'+str(self.server.server_port)}):
            self.assertEqual(self.authenticated('POST','/api/config',payload,**extra)[0].status,403)
        payload['config']['local']['enabled'] = True
        response, saved = self.authenticated('POST','/api/config',payload)
        self.assertEqual(response.status,200)
        saved_version = json.loads(saved)['config_version']
        self.assertNotEqual(saved_version,payload['version'])
        self.assertEqual(saved_version,json.loads(self.authenticated('GET','/api/state')[1])['config_version'])
        self.assertEqual(self.authenticated('POST','/api/config',{'config':payload['config'],'version':'stale'})[0].status,409)
        self.assertEqual(self.authenticated('POST','/api/config',{'large':'x'*33000})[0].status,413)
        self.assertEqual(self.authenticated('GET','/../../config.json')[0].status,404)
        self.assertEqual(self.authenticated('GET','/api/execute')[0].status,404)

    def test_validation_errors_identify_fields_without_echoing_credentials(self):
        current = json.loads(self.authenticated('GET', '/api/state')[1])
        payload = {'config': current['config'], 'version': current['config_version']}
        fake = 'sk-proj-synthetic-not-a-real-key-123456'
        payload['config']['decisions']['api_key_env'] = fake
        response, raw = self.authenticated('POST', '/api/config', payload)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw)['field'], 'decisions.api_key_env')
        self.assertNotIn(fake.encode(), raw)
        self.assertFalse((self.root / 'config.json').exists())
        payload['config']['decisions']['api_key_env'] = 'OPENAI_API_KEY'
        payload['config']['decisions']['minimum_confidence'] = 2
        response, raw = self.authenticated('POST', '/api/config', payload)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw)['field'], 'decisions.minimum_confidence')
        payload['config']['decisions']['minimum_confidence'] = 0.7
        self.assertEqual(self.authenticated('POST', '/api/config', payload)[0].status, 200)
