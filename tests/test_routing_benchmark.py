"""Synthetic evidence validation/arithmetic; no inference, sockets or home-state access."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import routing_benchmark as benchmark
import routing_decision as core
import routing_state as state


class Benchmark(unittest.TestCase):
    def setUp(self):
        self.suite = benchmark.load(ROOT/'assets/benchmarks/suite.synthetic.json')
        self.results = benchmark.load(ROOT/'assets/benchmarks/outcomes.synthetic.json')

    def test_offline_cli_and_comparison_never_read_environment_or_call_transport(self):
        with patch.dict('os.environ', {}, clear=True), \
             patch('os.environ.get', side_effect=AssertionError('environment access')), \
             patch.object(core, 'post_decisions', side_effect=AssertionError('provider call')), \
             patch('urllib.request.build_opener', side_effect=AssertionError('HTTP')):
            report = benchmark.compare(self.suite, self.results)
        self.assertEqual(report['provider_calls'], 0)
        self.assertEqual(report['execution_calls'], 0)
        self.assertEqual(report['evidence'], 'synthetic_demonstration_only')
        self.assertNotIn(self.suite['cases'][0]['packet']['summary'], json.dumps(report))

    def test_known_paired_arithmetic_unknown_cost_and_confidence_not_quality(self):
        result = benchmark.compare(self.suite, self.results)['splits']['holdout']
        self.assertEqual(result['complete_three_arm_blocks'], 6)
        decisions = result['arms']['decisions']
        self.assertEqual(decisions['task_passes'], 5)
        self.assertAlmostEqual(decisions['task_pass_rate'], 5/6)
        self.assertEqual(decisions['end_to_end_p50_ms'], 1650)
        self.assertEqual(decisions['end_to_end_p95_ms'], 2100)
        self.assertEqual(decisions['evaluator_input_tokens']['known_sum'], 480)
        self.assertEqual(decisions['measured_billed_cost_usd']['unknown_trials'], 6)
        self.assertIsNone(decisions['measured_cost_per_pass_usd'])
        self.assertAlmostEqual(result['decisions_minus_baseline']['rules']['task_pass_rate_delta'], -1/6)
        self.assertEqual(result['decisions_minus_baseline']['always_sonnet']['mean_end_to_end_delta_ms'], -350)
        for row in self.results['observations']:
            if row['arm'] == 'decisions': row['confidence'] = 0
        self.assertEqual(benchmark.compare(self.suite, self.results)['splits']['holdout'], result)

    def test_missing_trials_excluded_and_empty_quality_unknown(self):
        self.results['observations'].pop()
        result = benchmark.compare(self.suite, self.results)['splits']['holdout']
        self.assertEqual(result['complete_three_arm_blocks'], 5)
        self.assertEqual(result['unpaired_or_excluded_blocks'], 1)
        self.results['observations'] = []
        result = benchmark.compare(self.suite, self.results)['splits']['holdout']
        self.assertEqual(result['cases_without_complete_block'], ['csv','range'])
        self.assertIsNone(result['arms']['decisions']['task_pass_rate'])

    def test_conservation_overrides_and_budget_filter_never_relax_sonnet_baseline(self):
        cases = benchmark.plan(self.suite)['cases']
        self.assertEqual(cases[2]['rules'], 'haiku_default_conserve'); self.assertIsNone(cases[2]['always_sonnet'])
        self.suite['cases'][0]['overrides'] = {'model':'haiku'}
        self.assertIsNone(benchmark.plan(self.suite)['cases'][0]['always_sonnet'])
        packet = self.suite['cases'][1]['packet']
        packet['ceilings'] = {'tokens':100}
        packet['candidates'][1]['token_budget'] = 50
        self.assertEqual(benchmark.plan(self.suite)['cases'][1]['rules'], 'haiku_default')
        self.assertIsNone(benchmark.plan(self.suite)['cases'][1]['always_sonnet'])
        packet['candidates'][1]['token_budget'] = 200
        with self.assertRaises(ValueError): benchmark.plan(self.suite)

    def test_duplicate_unknown_text_nonfinite_and_invalid_routes_rejected(self):
        bad_rows = []
        for change in ({'actual_model':'sonnet-compat'}, {'selected_candidate_id':'missing'},
                       {'arm':'always_sonnet','selected_candidate_id':'haiku_default','actual_model':'haiku','actual_effort':'high'},
                       {'prompt':'private text'}, {'end_to_end_ms':True}, {'measured_billed_cost_usd':float('nan')},
                       {'confidence':float('inf')}, {'confidence':10**400}, {'measured_billed_cost_usd':10**400}, {'evaluator_input_tokens':-1},
                       {'arm':'decisions','used_provider':'decisions','evaluator_attempts':0},
                       {'arm':'decisions','used_provider':'rules','evaluator_fallback':True,
                        'selected_candidate_id':'haiku_default','actual_model':'haiku','actual_effort':'high'},
                       {'execution_status':'timeout','task_passed':True}, {'evaluator_ms':100000}):
            changed = copy.deepcopy(self.results); changed['observations'][0].update(change); bad_rows.append(changed)
        duplicate = copy.deepcopy(self.results); duplicate['observations'].append(duplicate['observations'][0]); bad_rows.append(duplicate)
        for bad in bad_rows:
            with self.subTest(bad=bad['observations'][0]), self.assertRaises(ValueError): benchmark.compare(self.suite, bad)

    def test_suite_identity_splits_and_measured_cost_coverage(self):
        self.results['synthetic'] = False
        with self.assertRaises(ValueError): benchmark.compare(self.suite, self.results)
        self.results['synthetic'] = True
        for row in self.results['observations']: row['measured_billed_cost_usd'] = .01
        holdout = benchmark.compare(self.suite, self.results)['splits']['holdout']
        self.assertAlmostEqual(holdout['arms']['decisions']['measured_cost_per_pass_usd'], .06/5)
        self.suite['cases'][0]['split'] = 'development'
        report = benchmark.compare(self.suite, self.results)
        self.assertEqual(report['splits']['development']['complete_three_arm_blocks'], 3)
        self.assertEqual(report['splits']['holdout']['complete_three_arm_blocks'], 3)

    def test_failure_inclusion_nullable_usage_and_explicit_correlations(self):
        row = self.results['observations'][2]
        row.update(execution_status='failed',task_passed=False,evaluator_fallback=True,used_provider='rules',
            confidence=None,
            selected_candidate_id='sonnet_low',actual_model='sonnet',actual_effort='low',
            downstream_input_tokens=None,router_request_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            session_id='unassigned',downstream_request_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
        result = benchmark.compare(self.suite, self.results)['splits']['holdout']['arms']['decisions']
        self.assertEqual(result['task_passes'], 4)
        self.assertEqual(result['evaluator_fallbacks'], 1)
        self.assertEqual(result['downstream_input_tokens']['unknown_trials'], 1)
        row['router_request_id'] = 'not-a-uuid'
        with self.assertRaises(ValueError): benchmark.compare(self.suite, self.results)

    def test_single_candidate_decisions_arm_without_evaluation_is_explicitly_valid(self):
        row = copy.deepcopy(self.results['observations'][0])
        row.update(case_id='conservation',arm='decisions',used_provider='rules',selected_candidate_id='haiku_default_conserve',
                   actual_model='haiku',actual_effort='default')
        self.results['observations'] = [row]
        report = benchmark.compare(self.suite,self.results)['splits']['development']
        self.assertEqual(report['complete_three_arm_blocks'],0)
        self.assertEqual(report['baseline_exclusions'],['conservation'])
        self.assertEqual(report['unpaired_or_excluded_blocks'],1)

    def test_cli_rejects_without_echoing_sensitive_payload_or_creating_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            file = Path(folder)/'bad.json'; file.write_text('{"secret":"sk-private-do-not-echo"}', encoding='utf-8')
            output = io.StringIO()
            with redirect_stdout(output): code = benchmark.main(['plan',str(file)])
            self.assertEqual(code, 2); self.assertNotIn('sk-private', output.getvalue())
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


class DecisionMetadataPrivacy(unittest.TestCase):
    def test_allowlist_strips_descriptions_arbitrary_text_and_request_digest(self):
        raw = {'source':'router','session_id':'unassigned','request_id':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'confidence':.78,'selected_candidate_id':'sonnet_low','selected_action':'stay','evaluator_model':'gpt-6-luna',
            'policy_version':'v1','selection_elapsed_ms':10,'prompt':'private task','request_sha256':'a'*64,
            'admitted_choices':[{'id':'sonnet_low','model':'sonnet','effort':'low','adapter':'native','action':'stay',
                                 'description':'private candidate text'}], 'choice_probabilities':{'sonnet_low':1}}
        clean = state.sanitize(raw)
        self.assertEqual(clean['confidence'], .78); self.assertEqual(clean['choice_probabilities'], {'sonnet_low':1})
        self.assertEqual(clean['selection_elapsed_ms'], 10)
        for text in ('private task','private candidate text','request_sha256'): self.assertNotIn(text,json.dumps(clean))
        raw['admitted_choices'][0]['model'] = 'sk-private'
        self.assertIsNone(state.sanitize(raw)['admitted_choices'])
        raw['confidence'] = float('nan')
        self.assertIsNone(state.sanitize(raw)['confidence'])
        self.assertIsNone(state.sanitize(raw)['choice_probabilities'])
        raw['confidence'] = 10**400
        self.assertIsNone(state.sanitize(raw)['confidence'])


if __name__ == '__main__': unittest.main()
