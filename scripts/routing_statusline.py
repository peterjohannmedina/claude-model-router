"""Content-free Claude status-line snapshots; no request/execution attribution.

Context token fields describe the latest response/current context, not cumulative
session or billed usage. Session cost is a client-side estimate. Nothing here
opens a transcript, probes a provider, or changes Claude settings.
"""
import json

import routing_catalog as catalog
import routing_state as state

MAX_BYTES = 1048576
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}


def decode(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise ValueError('statusline_body_limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('invalid_number')
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('invalid_statusline_payload')
    return value


def metadata(record, collection_transport=None):
    if (not isinstance(record, dict) or 'hook_event_name' in record or 'resourceLogs' in record
            or not isinstance(record.get('model'), dict)):
        raise ValueError('invalid_statusline_payload')
    session = state.identifier(record.get('session_id'), allow_unassigned=False)
    model = record['model'].get('id')
    if model is not None and (not catalog.model_id(model) or state.label(model) is None):
        raise ValueError('invalid_statusline_model')
    event = {'source': 'claude_statusline', 'session_id': session,
             'request_id': state.statusline_request_id(session), 'session_source': 'claude_thread',
             'stage': 2, 'adapter': 'native', 'mode': 'statusline_snapshot',
             'execution_status': 'session_snapshot_observed', 'model_source': 'statusline_configuration',
             'contract_validation': 'documented_statusline_schema', 'model': model,
             'producer_version': record.get('version'), 'collection_transport': collection_transport}
    effort = record.get('effort')
    if effort is not None and not isinstance(effort, dict):
        raise ValueError('invalid_statusline_effort')
    level = effort.get('level') if effort is not None else None
    if level is not None and (not isinstance(level, str) or level not in EFFORTS):
        raise ValueError('invalid_statusline_effort')
    event['effort'] = level
    cost = record.get('cost')
    context = record.get('context_window')
    if cost is not None and not isinstance(cost, dict) or context is not None and not isinstance(context, dict):
        raise ValueError('invalid_statusline_snapshot')
    for source, target in (('total_duration_ms', 'session_duration_ms'),
                           ('total_api_duration_ms', 'session_api_duration_ms'),
                           ('total_cost_usd', 'session_cost_estimate_usd')):
        event[target] = state.snapshot_number((cost or {}).get(source))
    event['latest_context_usage'] = ({'input_tokens': state.snapshot_number(context.get('total_input_tokens'), integral=True),
                                     'output_tokens': state.snapshot_number(context.get('total_output_tokens'), integral=True)}
                                    if context is not None else None)
    return state.sanitize(event)


def record(event, ledger):
    enabled = getattr(getattr(ledger, 'settings', None), 'enabled', True)
    changed = ledger.put(event)
    return {'accepted': 1, 'ignored': 0, 'rejected': 0, 'recorded': int(enabled),
            'changed': int(bool(changed)), 'heartbeats': int(enabled and not changed)}


def ingest(raw, ledger, collection_transport=None):
    return record(metadata(decode(raw), collection_transport), ledger)
