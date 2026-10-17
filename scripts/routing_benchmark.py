"""Offline routing plans and paired outcome comparisons. Never executes a task or calls a provider."""
import argparse
import json
import math
from pathlib import Path
import statistics

import routing_decision as core
import routing_state as state

ARMS = ('rules', 'always_sonnet', 'decisions')
MAX_BYTES = 8 * 1024 * 1024


def fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError('invalid_benchmark_fields')
    return value


def name(value):
    if state.label(value) is None: raise ValueError('invalid_benchmark_label')
    return value


def integer(value, maximum=10**12, nullable=False):
    if value is None and nullable: return None
    if type(value) is not int or not 0 <= value <= maximum: raise ValueError('invalid_benchmark_count')
    return value


def load(path):
    if path.stat().st_size > MAX_BYTES: raise ValueError('benchmark_file_too_large')
    return core.read_json(path.read_text(encoding='utf-8-sig'))


def plan(suite):
    fields(suite, ('schema_version', 'suite_id', 'synthetic', 'cases'),
           ('schema_version', 'suite_id', 'synthetic', 'cases'))
    if type(suite['schema_version']) is not int or suite['schema_version'] != 1 or type(suite['synthetic']) is not bool:
        raise ValueError('invalid_benchmark_schema')
    name(suite['suite_id'])
    if not isinstance(suite['cases'], list) or not 1 <= len(suite['cases']) <= 200:
        raise ValueError('invalid_benchmark_cases')
    cases, seen = [], set()
    for case in suite['cases']:
        fields(case, ('id', 'split', 'check_id', 'packet', 'overrides'), ('id', 'split', 'check_id', 'packet'))
        key = name(case['id']); name(case['check_id'])
        if key in seen or case['split'] not in ('development', 'holdout'): raise ValueError('invalid_benchmark_case')
        seen.add(key)
        # Pure inner-contract admission: no saved settings, catalog discovery,
        # credentials, ledger or calls. Synthetic packets are demonstrations.
        # A real harness must freeze verified picker/capacity evidence here;
        # this offline planner makes no live eligibility claim.
        request = core.Request.parse(case['packet'],
            core.Overrides.parse(case.get('overrides', {})))
        sonnet = [c.id for c in request.candidates
               if (c.model, c.effort, c.adapter) == ('sonnet', 'low', 'native')]
        if len(sonnet) > 1: raise ValueError('ambiguous_always_sonnet_route')
        cases.append({'id': key, 'split': case['split'], 'check_id': case['check_id'],
            'rules': request.fallback.id, 'always_sonnet': sonnet[0] if sonnet else None,
            'baseline_exclusion': None if sonnet else 'fixed_sonnet_low_not_admissible',
            'choices': [{k: getattr(c, k) for k in ('id', 'model', 'effort', 'adapter', 'action')}
                        for c in request.candidates]})
    return {'schema_version': 1, 'suite_id': suite['suite_id'], 'synthetic': suite['synthetic'],
            'execution_calls': 0, 'provider_calls': 0, 'cases': cases}


OUTCOME_REQUIRED = ('case_id', 'trial', 'arm', 'selected_candidate_id', 'actual_model', 'actual_effort',
    'execution_status', 'task_passed', 'end_to_end_ms', 'evaluator_ms', 'evaluator_attempts',
    'used_provider', 'evaluator_fallback', 'evaluator_input_tokens', 'evaluator_output_tokens',
    'downstream_input_tokens', 'downstream_output_tokens', 'measured_billed_cost_usd')
OUTCOME_OPTIONAL = ('confidence', 'router_request_id', 'session_id', 'downstream_request_id')


def outcomes(raw, planned):
    fields(raw, ('schema_version', 'suite_id', 'synthetic', 'observations'),
           ('schema_version', 'suite_id', 'synthetic', 'observations'))
    if (type(raw['schema_version']) is not int or raw['schema_version'] != 1
            or type(raw['synthetic']) is not bool or raw['synthetic'] != planned['synthetic']
            or raw['suite_id'] != planned['suite_id']): raise ValueError('benchmark_suite_mismatch')
    if not isinstance(raw['observations'], list) or len(raw['observations']) > 30000:
        raise ValueError('invalid_benchmark_observations')
    cases = {c['id']: c for c in planned['cases']}
    result, seen = [], set()
    for row in raw['observations']:
        fields(row, OUTCOME_REQUIRED + OUTCOME_OPTIONAL, OUTCOME_REQUIRED)
        if row['case_id'] not in cases or row['arm'] not in ARMS: raise ValueError('unknown_benchmark_case_or_arm')
        integer(row['trial'], 100)
        if row['trial'] == 0: raise ValueError('invalid_benchmark_trial')
        key = (row['case_id'], row['trial'], row['arm'])
        if key in seen: raise ValueError('duplicate_benchmark_observation')
        seen.add(key)
        case = cases[row['case_id']]
        choice = next((c for c in case['choices'] if c['id'] == row['selected_candidate_id']), None)
        if (choice is None or (row['actual_model'], row['actual_effort']) != (choice['model'], choice['effort'])
                or (row['arm'] != 'decisions' and row['selected_candidate_id'] != case[row['arm']])):
            raise ValueError('inadmissible_or_wrong_baseline_route')
        if (row['execution_status'] not in ('completed', 'failed', 'timeout') or type(row['task_passed']) is not bool
                or (row['task_passed'] and row['execution_status'] != 'completed')):
            raise ValueError('invalid_task_outcome')
        for key in ('end_to_end_ms', 'evaluator_attempts'): integer(row[key])
        integer(row['evaluator_ms'], nullable=True)
        for key in ('evaluator_input_tokens', 'evaluator_output_tokens', 'downstream_input_tokens', 'downstream_output_tokens'):
            integer(row[key], nullable=True)
        if row['evaluator_ms'] is not None and row['evaluator_ms'] > row['end_to_end_ms']:
            raise ValueError('evaluator_latency_exceeds_total')
        cost = row['measured_billed_cost_usd']
        if cost is not None and (type(cost) not in (int, float) or not 0 <= cost <= 10**6 or not math.isfinite(cost)):
            raise ValueError('invalid_measured_cost')
        if type(row['evaluator_fallback']) is not bool: raise ValueError('invalid_fallback_flag')
        if ((row['arm'] == 'decisions' and row['used_provider'] not in ('decisions', 'rules'))
                or (row['arm'] == 'rules' and row['used_provider'] != 'rules')
                or (row['arm'] == 'always_sonnet' and row['used_provider'] != 'fixed')):
            raise ValueError('invalid_benchmark_provider')
        if row['arm'] == 'decisions':
            if row['used_provider'] == 'decisions':
                if row['evaluator_attempts'] != 1 or row['evaluator_fallback']:
                    raise ValueError('inconsistent_evaluator_outcome')
            elif (row['selected_candidate_id'] != case['rules'] or row['evaluator_attempts'] > 1
                    or (not row['evaluator_fallback'] and not (len(case['choices']) == 1 and row['evaluator_attempts'] == 0))):
                raise ValueError('inconsistent_rules_fallback')
        elif (row['evaluator_attempts'] != 0 or row['evaluator_fallback']
                or row['evaluator_ms'] not in (None, 0) or row.get('confidence') is not None):
            raise ValueError('baseline_cannot_evaluate')
        if row['evaluator_attempts'] == 0 and any(row[k] not in (0, None) for k in ('evaluator_input_tokens', 'evaluator_output_tokens')):
            raise ValueError('usage_without_evaluation')
        if 'confidence' in row and row['confidence'] is not None and state.probability(row['confidence']) is None:
            raise ValueError('invalid_benchmark_confidence')
        if row.get('confidence') is not None and row['used_provider'] != 'decisions':
            raise ValueError('confidence_without_evaluation_result')
        for key in ('router_request_id', 'session_id', 'downstream_request_id'):
            if row.get(key) is not None: state.identifier(row[key], allow_unassigned=key == 'session_id')
        result.append(dict(row))
    return result


def summary(rows):
    n = len(rows)
    latencies = sorted(r['end_to_end_ms'] for r in rows)
    successes = sum(r['task_passed'] for r in rows)
    result = {'trials': n, 'task_passes': successes, 'task_pass_rate': successes / n if n else None,
        'end_to_end_p50_ms': statistics.median(latencies) if n else None,
        'end_to_end_p95_ms': latencies[math.ceil(.95*n)-1] if n else None,
        'evaluator_attempts': sum(r['evaluator_attempts'] for r in rows),
        'evaluator_fallbacks': sum(r['evaluator_fallback'] for r in rows)}
    for key in ('evaluator_ms', 'evaluator_input_tokens', 'evaluator_output_tokens',
                'downstream_input_tokens', 'downstream_output_tokens', 'measured_billed_cost_usd'):
        known = [r[key] for r in rows if r[key] is not None]
        result[key] = {'known_sum': sum(known) if known else None, 'unknown_trials': n-len(known)}
    costs = result['measured_billed_cost_usd']
    result['measured_cost_per_pass_usd'] = (costs['known_sum']/successes
        if successes and costs['unknown_trials'] == 0 else None)
    return result


def compare(suite, raw):
    planned = plan(suite)
    rows = outcomes(raw, planned)
    groups = {}
    for split in ('development', 'holdout'):
        cases = {c['id']: c for c in planned['cases'] if c['split'] == split}
        blocks = {}
        for row in rows:
            if row['case_id'] in cases:
                blocks.setdefault((row['case_id'], row['trial']), {})[row['arm']] = row
        matched = [b for b in blocks.values() if set(b) == set(ARMS)]
        summaries = {arm: summary([b[arm] for b in matched]) for arm in ARMS}
        comparisons = {}
        for baseline in ('rules', 'always_sonnet'):
            differences = [int(b['decisions']['task_passed'])-int(b[baseline]['task_passed']) for b in matched]
            comparisons[baseline] = {'paired_trials': len(matched),
                'task_pass_rate_delta': statistics.mean(differences) if differences else None,
                'mean_end_to_end_delta_ms': statistics.mean(
                    b['decisions']['end_to_end_ms']-b[baseline]['end_to_end_ms'] for b in matched) if matched else None}
        groups[split] = {'complete_three_arm_blocks': len(matched),
            'unpaired_or_excluded_blocks': len(blocks)-len(matched),
            'cases_without_complete_block': [c for c in cases if not any(c == k[0] and set(b) == set(ARMS) for k,b in blocks.items())],
            'baseline_exclusions': [c['id'] for c in cases.values() if c['always_sonnet'] is None],
            'arms': summaries, 'decisions_minus_baseline': comparisons}
    return {'schema_version': 1, 'suite_id': planned['suite_id'], 'synthetic': planned['synthetic'],
        'evidence': 'synthetic_demonstration_only' if planned['synthetic'] else 'caller_supplied_outcomes_not_independently_verified',
        'provider_calls': 0, 'execution_calls': 0, 'observations_supplied': len(rows), 'splits': groups,
        'limits': ['Only complete case/trial blocks enter comparisons; missing/excluded blocks are reported.',
                   'Confidence and selection agreement are not task quality. No statistical significance claim.',
                   'Measured cost excludes unknown billing; token totals keep unknown counts.',
                   'End-to-end latency must include evaluator, dispatch, tools and retries; never add evaluator time twice.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'compare'))
    parser.add_argument('suite', type=Path)
    parser.add_argument('outcomes', type=Path, nargs='?')
    args = parser.parse_args(argv)
    try:
        if (args.command == 'compare') != (args.outcomes is not None): raise ValueError('outcomes_required_only_for_compare')
        suite = load(args.suite)
        result = compare(suite, load(args.outcomes)) if args.command == 'compare' else plan(suite)
        print(json.dumps(result, indent=2, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError):
        # Never echo source text, paths, answers or arbitrary exception strings.
        print(json.dumps({'error': 'invalid_benchmark_input', 'provider_calls': 0, 'execution_calls': 0})); return 2


if __name__ == '__main__': raise SystemExit(main())
