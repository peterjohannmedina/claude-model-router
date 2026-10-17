import copy
import itertools
from dataclasses import asdict, replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import routing_decision as core
import routing_runtime as runtime
from test_routing_decision import packet, native_catalog


def native_route(*args, **kwargs):
    kwargs.setdefault('native_catalog', native_catalog())
    return runtime.route(*args, **kwargs)


def native_execute(*args, **kwargs):
    kwargs.setdefault('native_catalog', native_catalog())
    return runtime.execute_local(*args, **kwargs)


def local_packet():
    value = packet()
    value['local_work_eligible'] = True
    value['candidates'].append({'id': 'custom_local', 'action': 'delegate', 'adapter': 'local',
                                'model': 'example-model', 'effort': 'default'})
    return value


def configuration(local=False, decisions=False):
    return runtime.Config.parse({'schema_version': 1, 'decisions': {'enabled': decisions},
        'local': {'enabled': local, 'base_url': 'http://127.0.0.1:8000/v1', 'model': 'example-model',
                  'prefer_when_eligible': True}})


def catalog(*_):
    return b'{"data":[{"id":"example-model"}]}'


class OptionalProviders(unittest.TestCase):
    def setUp(self):
        fake = patch.object(runtime.catalog, 'load', side_effect=lambda *_: native_catalog())
        fake.start()
        self.addCleanup(fake.stop)

    def test_local_execution_keeps_selection_timing_separate_from_execution(self):
        clock = [0.0]
        def worker(method, *_):
            if method == 'GET': return catalog()
            clock[0] = .2
            return b'{"choices":[{"message":{"content":"synthetic completed result"}}]}'
        with patch('time.monotonic',side_effect=lambda:clock[0]):
            result = native_execute(local_packet(),configuration(True),environment={},
                transport=worker,allow_local_inference=True)
        self.assertEqual(result['execution']['status'],'completed')
        self.assertEqual(result['provenance']['elapsed_ms'],200)
        self.assertEqual(result['provenance']['selection_elapsed_ms'],0)

    def test_all_four_provider_combinations(self):
        for local in (False, True):
            for decisions in (False, True):
                with self.subTest(local=local, decisions=decisions):
                    calls = {'local': 0, 'decisions': 0}
                    def local_wire(*args):
                        calls['local'] += 1
                        self.assertEqual(args[0], 'GET')
                        return catalog()
                    def decision_wire(body, *_):
                        calls['decisions'] += 1
                        choices = json.loads(body)['questions'][0]['choices']
                        return json.dumps({'answers': [{'type': 'choice', 'name': 'route',
                            'choice': 'haiku_default', 'confidence': 0.9, 'probabilities': [
                                {'value': c['value'], 'probability': int(c['value'] == 'haiku_default')}
                                for c in choices]}]}).encode()
                    result = native_route(local_packet(), configuration(local, decisions),
                        environment={'OPENAI_API_KEY': 'offline-placeholder'}, allow_api=True,
                        local_transport=local_wire, decision_transport=decision_wire)
                    self.assertEqual(calls, {'local': int(local), 'decisions': int(decisions)})
                    self.assertEqual(result['recommendation']['id'],
                                     'haiku_default' if decisions else ('custom_local' if local else 'sonnet_medium'))

    def test_no_extra_provider_is_a_complete_baseline(self):
        def forbidden(*_): self.fail('baseline contacted an optional provider')
        result = native_route(packet(), environment={}, local_transport=forbidden, decision_transport=forbidden)
        self.assertEqual(result['recommendation']['model'], 'sonnet')
        self.assertEqual(result['provenance']['used_provider'], 'rules')

    def test_unconfigured_local_is_optional(self):
        config = runtime.Config.parse({'schema_version': 1, 'local': {'enabled': True}})
        result = native_route(local_packet(), config, environment={}, local_transport=lambda *_: self.fail('unconfigured probe'))
        self.assertEqual(result['recommendation']['id'], 'sonnet_medium')
        self.assertEqual(result['provenance']['local_status'], 'unconfigured')

    def test_local_failures_preserve_native_baseline(self):
        for reason in ('network', 'http_401', 'http_403', 'http_500', 'timeout', 'response_too_large'):
            def failed(*_): raise core.ProviderFailure(reason)
            result = native_route(local_packet(), configuration(True), environment={}, local_transport=failed)
            self.assertEqual(result['recommendation']['id'], 'sonnet_medium')
            self.assertEqual(result['provenance']['local_status'], reason)
        for data in (b'bad JSON', b'{}', b'{"data":[null]}', b'{"data":[{"id":"other-model"}]}'):
            result = native_route(local_packet(), configuration(True), environment={}, local_transport=lambda *_: data)
            self.assertEqual(result['recommendation']['adapter'], 'native')

    def test_decisions_failures_and_missing_access_preserve_native_baseline(self):
        for reason in ('network', 'timeout', 'http_401', 'http_429', 'http_500'):
            def failed(*_): raise core.ProviderFailure(reason)
            result = native_route(packet(), configuration(False, True), allow_api=True,
                                   environment={'OPENAI_API_KEY': 'offline-placeholder'}, decision_transport=failed)
            self.assertEqual(result['recommendation']['id'], 'sonnet_medium')
            self.assertEqual(result['provenance']['fallback_reason'], reason)
        for data, expected in ((b'bad JSON', 'invalid_response'),
                               (b'{"answers":[{"type":"refusal","name":"route"}]}', 'refusal')):
            result = native_route(packet(), configuration(False, True), allow_api=True,
                environment={'OPENAI_API_KEY': 'offline-placeholder'}, decision_transport=lambda *_: data)
            self.assertEqual(result['provenance']['fallback_reason'], expected)
        for consent, environment, reason in ((False, {}, 'api_not_authorized'), (True, {}, 'missing_api_key')):
            result = native_route(packet(), configuration(False, True), allow_api=consent, environment=environment,
                                    decision_transport=lambda *_: self.fail('unapproved decision call'))
            self.assertEqual(result['provenance']['fallback_reason'], reason)

    def test_require_local_never_degrades_to_native(self):
        value = local_packet()
        value['require_local'] = True
        for config, transport in ((configuration(False), catalog),
                                  (configuration(True), lambda *_: b'bad JSON')):
            with self.assertRaises(core.RoutingError):
                native_route(value, config, environment={}, local_transport=transport,
                              decision_transport=lambda *_: self.fail('paid fallback'))
        result = native_route(value, configuration(True), environment={}, local_transport=catalog)
        self.assertEqual(result['recommendation']['adapter'], 'local')

    def test_generic_local_override_and_incompatible_effort(self):
        result = native_route(local_packet(), configuration(True), environment={}, local_transport=catalog,
                                overrides=core.Overrides.parse({'model': 'example-model', 'effort': 'default'}))
        self.assertEqual(result['recommendation']['id'], 'custom_local')
        with self.assertRaises(core.RoutingError):
            native_route(local_packet(), configuration(True), environment={}, local_transport=catalog,
                          overrides=core.Overrides.parse({'model': 'example-model', 'effort': 'high'}))

    def test_native_override_skips_unneeded_local_probe(self):
        result = native_route(local_packet(), configuration(True), environment={},
            overrides=core.Overrides.parse({'model': 'opus'}),
            local_transport=lambda *_: self.fail('unnecessary local probe'))
        self.assertEqual(result['recommendation']['model'], 'opus')

    def test_hard_caps_and_offline_mode_stop_decisions(self):
        base = configuration(False, True)
        for settings, reason in ((replace(base.decisions, max_input_tokens=1000), 'verified_decisions_token_counter_unavailable'),
                                  (replace(base.decisions, max_cost_usd=0.01), 'verified_decisions_cost_bound_unavailable')):
            config = replace(base, decisions=settings)
            result = native_route(packet(), config, allow_api=True, environment={}, preflight_only=True,
                                  decision_transport=lambda *_: self.fail('unverified budget call'))
            self.assertFalse(result['preflight']['may_load_credential'])
            self.assertEqual(result['preflight']['block_reason'], reason)
        result = native_route(local_packet(), configuration(True, True), environment={}, offline=True, allow_api=True,
            local_transport=lambda *_: self.fail('offline local probe'), decision_transport=lambda *_: self.fail('offline API'))
        self.assertEqual(result['recommendation']['adapter'], 'native')
        self.assertEqual(result['provenance']['fallback_reason'], 'offline')

    def test_worker_requires_separate_authority_and_has_no_paid_fallback(self):
        calls = []
        def wire(method, _, body, key, timeout):
            calls.append(method)
            if method == 'GET': return catalog()
            self.assertEqual(key, '')
            data = json.loads(body)
            self.assertEqual(data['max_tokens'], 512)
            self.assertNotIn('reasoning_effort', data)
            self.assertNotIn('tools', data)
            return b'{"choices":[{"message":{"content":"bounded worker result"}}]}'
        result = native_execute(local_packet(), configuration(True), environment={}, transport=wire)
        self.assertNotIn('POST', calls)
        self.assertEqual(result['execution']['status'], 'not_started')
        result = native_execute(local_packet(), configuration(True), environment={}, transport=wire, allow_local_inference=True)
        self.assertEqual(result['execution']['status'], 'completed')
        self.assertEqual(calls.count('POST'), 1)
        def failing_worker(method, *_):
            if method == 'GET': return catalog()
            raise core.ProviderFailure('network')
        result = native_execute(local_packet(), configuration(True), environment={}, transport=failing_worker, allow_local_inference=True)
        self.assertEqual(result['recommendation']['adapter'], 'native')
        self.assertFalse(result['execution']['native_execution_started'])
        value = local_packet()
        value['require_local'] = True
        result = native_execute(value, configuration(True), environment={}, transport=failing_worker, allow_local_inference=True)
        self.assertEqual(result['error'], 'required_route_failed')
        self.assertIsNone(result['recommendation'])

    def test_local_output_and_execution_ceilings_fail_closed(self):
        def malformed_worker(method, *_):
            return catalog() if method == 'GET' else b'{"choices":[]}'
        result = native_execute(local_packet(), configuration(True), environment={}, transport=malformed_worker, allow_local_inference=True)
        self.assertEqual(result['execution']['reason'], 'malformed_output')
        value = local_packet()
        for c in value['candidates']: c['token_budget'] = 1000
        value['ceilings'] = {'tokens': 1000}
        calls = []
        def no_execution(method, *_): calls.append(method); return catalog()
        result = native_execute(value, configuration(True), environment={}, transport=no_execution, allow_local_inference=True)
        self.assertEqual(result['execution']['reason'], 'execution_budget_unverifiable')
        self.assertNotIn('POST', calls)

    def test_private_address_and_secret_configuration_are_not_assumed(self):
        base = configuration(True)
        config = replace(base, local=replace(base.local, base_url='http://192.0.2.1:8000/v1'))
        result = native_route(local_packet(), config, environment={}, local_transport=lambda *_: self.fail('unapproved remote probe'))
        self.assertEqual(result['provenance']['local_status'], 'remote_not_enabled')
        with self.assertRaises(core.RoutingError):
            runtime.Config.parse({'schema_version': 1, 'decisions': {'api_key': 'not allowed'}})

    def test_real_wait_deadline_for_stalled_local_probe(self):
        release = threading.Event()
        config = configuration(True)
        config = replace(config, local=replace(config.local, timeout_seconds=0.05))
        def stalled(*_):
            release.wait(2)
            return catalog()
        try:
            start = time.monotonic()
            result = native_route(local_packet(), config, environment={}, local_transport=stalled)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(result['provenance']['local_status'], 'timeout')
            self.assertEqual(result['recommendation']['adapter'], 'native')
        finally:
            release.set()
            self.assertTrue(core._transport_slot.acquire(timeout=1))
            core._transport_slot.release()

    def test_missing_local_auth_and_explicit_dated_native_id(self):
        config = configuration(True)
        config = replace(config, local=replace(config.local, api_key_env='LOCAL_KEY'))
        result = native_route(local_packet(), config, environment={},
                               local_transport=lambda *_: self.fail('missing local authentication'))
        self.assertEqual(result['provenance']['local_status'], 'missing_key')
        value = packet()
        value['available_models']['claude-sonnet-4-5'] = ['medium']
        evidence = native_catalog()
        evidence['models'].append({'id': 'claude-sonnet-4-5', 'efforts': ['medium']})
        value['candidates'][0]['model'] = 'claude-sonnet-4-5'
        result = native_route(value, environment={}, native_catalog=evidence)
        self.assertEqual(result['recommendation']['model'], 'claude-sonnet-4-5')

    def test_budget_excluded_local_candidate_is_not_probed(self):
        value = local_packet()
        for c in value['candidates']:
            c['cost_upper_bound_usd'] = 0 if c.get('adapter') != 'local' else 2
        value['ceilings'] = {'cost_usd': 1}
        result = native_route(value, configuration(True), environment={},
                               local_transport=lambda *_: self.fail('budget excluded probe'))
        self.assertEqual(result['recommendation']['adapter'], 'native')

    def test_execution_deadline_includes_readiness_probe(self):
        value = local_packet()
        for c in value['candidates']: c['latency_budget_ms'] = 100
        value['ceilings'] = {'latency_ms': 100}
        calls = []
        def wire(method, *_):
            calls.append(method)
            return catalog()
        # The clock advances past the entire task deadline during readiness.
        with patch.object(runtime.time, 'monotonic', side_effect=itertools.chain([0], itertools.repeat(0.2))):
            result = native_execute(value, configuration(True), environment={},
                                           transport=wire, allow_local_inference=True)
        self.assertEqual(calls, ['GET'])
        self.assertEqual(result['execution']['reason'], 'deadline_exhausted')
        self.assertFalse(result['execution']['native_execution_started'])

    def test_offline_routing_survives_initial_and_final_ledger_failures(self):
        for writes in ([OSError('synthetic storage unavailable')], [True,OSError('synthetic storage unavailable')]):
            with self.subTest(writes=len(writes)):
                output=io.StringIO()
                with (patch.object(Path,'is_file',return_value=True),
                      patch.object(core,'load_file',side_effect=[{'schema_version':1},packet()]),
                      patch.object(runtime.state,'Ledger') as ledger,
                      patch.object(core.DecisionsProvider,'choose',side_effect=AssertionError('offline provider call')),
                      patch.object(core,'bounded_call',side_effect=AssertionError('network request')),
                      redirect_stdout(output)):
                    ledger.return_value.put.side_effect=writes
                    self.assertEqual(runtime.main(['packet.json','--config','config.json','--offline']),0)
                result=json.loads(output.getvalue())
                self.assertEqual(result['ledger']['status'],'unavailable')
                self.assertEqual(result['recommendation']['adapter'],'native')

    def test_cli_preflight_dominates_execution_and_consent(self):
        raw = {'schema_version': 1, **asdict(configuration(True, True))}
        for offline in (False, True):
            with self.subTest(offline=offline):
                methods = []
                def readiness_only(_config, _environment, _transport, method='GET', body=None):
                    methods.append(method)
                    self.assertEqual(method, 'GET', 'preflight attempted inference')
                    self.assertIsNone(body)
                    return {'data': [{'id': 'example-model'}]}
                output = io.StringIO()
                argv = ['packet.json', '--config', 'config.json', '--preflight-only',
                        '--execute-local', '--allow-local-inference', '--allow-api', '--no-ledger']
                if offline: argv.append('--offline')
                with (patch.object(Path, 'is_file', return_value=True),
                      patch.object(core, 'load_file', side_effect=[raw, local_packet()]),
                      patch.object(runtime, 'local_request', side_effect=readiness_only),
                      patch.object(runtime, 'execute_local', side_effect=AssertionError('preflight executed work')),
                      patch.object(core.DecisionsProvider, 'choose', side_effect=AssertionError('preflight called Decisions')),
                      patch.object(core, 'bounded_call', side_effect=AssertionError('unmocked transport')),
                      redirect_stdout(output)):
                    self.assertEqual(runtime.main(argv), 0)
                result = json.loads(output.getvalue())
                self.assertIn('preflight', result)
                self.assertEqual(result['preflight']['decisions_api_calls'], 0)
                self.assertEqual(methods, [] if offline else ['GET'])


if __name__ == '__main__':
    unittest.main()
