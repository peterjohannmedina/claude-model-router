"""Metadata-only Claude hook adapter, retained under the shared telemetry API.

This is not an OTLP decoder. It accepts documented lifecycle hook payloads only.
No prompt, summary, assistant text, path, tool payload, or transcript is retained.
"""
import json
import re
import uuid

import routing_catalog as catalog
import routing_state as state

MAX_BYTES = 1048576
MAX_RECORDS = 128
VERSION_TALLY_LIMIT = 16
SUPPORTED_VERSIONS = ()  # No live producer capture has been verified in this package.
SCHEMA_SERIES = ()
HEADER = 'X-Routing-OTel'
IGNORE_REASONS = ('unsupported_event', 'missing_event', 'unsupported_transport', 'unsupported_version',
                  'invalid_version', 'schema_mismatch')
EVENT_CLASSES = {
    'SessionStart': ('hook_session', 'session_observed'),
    'Stop': ('hook_stop', 'stop_observed'),
    'PostCompact': ('hook_compact', 'compaction_observed'),
    'SessionEnd': ('hook_end', 'session_end_observed'),
    'SubagentStart': ('hook_subagent_start', 'subagent_start_observed'),
    'SubagentStop': ('hook_subagent_stop', 'subagent_stop_observed'),
    'PostModelSwitch': ('hook_model_switch', 'model_switch_observed'),
}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}


def capability(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value):
        raise ValueError('invalid_local_capability')
    return value


def version_label(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', value) else 'invalid'


def model_label(value):
    # The generic ledger label permits paths for other metadata uses. Model
    # fields must satisfy the narrower public model-ID grammar first.
    return state.label(value) if catalog.model_id(value) else None


def tally(producer, kind, label):
    if producer is None:
        return
    counts = producer.setdefault(kind, {})
    if label not in counts and len(counts) >= VERSION_TALLY_LIMIT:
        label = 'other'
    counts[label] = counts.get(label, 0) + 1


def metadata(record, diagnostics=None, producer=None, producer_version=None, event_id=None):
    def ignore(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1
        return None
    if not isinstance(record, dict):
        raise ValueError('invalid_record')
    if 'resourceLogs' in record:
        return ignore('unsupported_transport')
    name = record.get('hook_event_name')
    tally(producer, 'events', name if name in EVENT_CLASSES else 'other')
    if name is None:
        return ignore('missing_event')
    if name not in EVENT_CLASSES:
        return ignore('unsupported_event')
    # Official hook input has no required producer version. Do not infer it from
    # a model ID, environment, transcript, arbitrary version field, or app.version.
    if producer_version is not None:
        label = version_label(producer_version)
        tally(producer, 'versions', 'operator:' + label)
        if label == 'invalid':
            return ignore('invalid_version')
        if name == 'PostModelSwitch' and tuple(map(int, producer_version.split('.'))) < (2, 1, 251):
            return ignore('unsupported_version')
    else:
        tally(producer, 'versions', 'unreported')
    session = state.identifier(record.get('session_id'), allow_unassigned=False)
    mode, status = EVENT_CLASSES[name]
    identity = [name]
    # The prompt UUID is metadata, not content. Never read transcript_path to
    # fill a missing prompt/model. Older payloads may lack this correlation ID.
    if record.get('prompt_id') is not None:
        identity.append(state.identifier(record['prompt_id'], allow_unassigned=False))
    if name in ('SubagentStart', 'SubagentStop'):
        agent = record.get('agent_id')
        if not isinstance(agent, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', agent):
            return ignore('schema_mismatch')
        identity.append(agent)
    if name == 'SessionStart':
        source = record.get('source')
        if source not in ('startup', 'resume', 'clear', 'compact', 'fork'):
            return ignore('schema_mismatch')
        identity.append(source)
    if name in ('Stop', 'SubagentStop') and type(record.get('stop_hook_active')) is not bool:
        return ignore('schema_mismatch')
    if name == 'PostCompact' and record.get('trigger') not in ('manual', 'auto'):
        return ignore('schema_mismatch')
    if name == 'SessionEnd' and record.get('reason') not in ('clear', 'resume', 'logout', 'prompt_input_exit', 'other'):
        return ignore('schema_mismatch')
    model = None
    if name == 'SessionStart':
        model = model_label(record.get('model'))
    elif name == 'PostModelSwitch':
        if record.get('source') not in ('command', 'picker', 'sdk', 'auto', 'resume'):
            return ignore('schema_mismatch')
        model = model_label(record.get('to_model'))
        previous = model_label(record.get('from_model'))
        if model is None or previous is None:
            return ignore('schema_mismatch')
        identity += [previous, model]
    # The ledger keys by source/request, so an operator event ID must never be
    # globally authoritative. Namespace it under the native session identity.
    request_identity = ('explicit:' + state.identifier(event_id, allow_unassigned=False)
                        if event_id is not None else ':'.join(identity))
    request = str(uuid.uuid5(uuid.UUID(session), request_identity))
    event = {'source': 'claude_hook', 'session_id': session, 'session_source': 'claude_thread',
             'request_id': request, 'stage': 2, 'mode': mode, 'adapter': 'native',
             'execution_status': status, 'model_source': 'hook_metadata',
             'contract_validation': 'documented_hook_schema',
             'evaluator_usage': {'input_tokens': 0, 'output_tokens': 0}, 'evaluator_attempts': 0}
    if model is not None:
        event['model'] = model
    effort = record.get('effort')
    if isinstance(effort, dict) and effort.get('level') in EFFORTS:
        event['effort'] = effort['level']
    if producer_version is not None:
        event['producer_version'] = producer_version
    # sanitize performs the same final allowlist as the persistent ledger.
    normalized = state.sanitize(event)
    return {key: value for key, value in normalized.items() if key in event}


def _json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('body_limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('invalid_number')
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique, parse_constant=invalid)


def decode(raw):
    value = _json(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid_hook_payload')
    if set(value) == {'events'}:
        result = value['events']
        if not isinstance(result, list) or not 1 <= len(result) <= MAX_RECORDS:
            raise ValueError('record_limit')
        return result
    return [value]


def ingest(raw, ledger, diagnostics=None, producer=None):
    accepted = ignored = rejected = 0
    events = []
    for item in decode(raw):
        try:
            # This wrapper belongs to this adapter, not Anthropic's hook schema.
            if isinstance(item, dict) and 'hook' in item:
                if set(item) - {'hook', 'producer_version', 'event_id'}:
                    raise ValueError('invalid_hook_envelope')
                event = metadata(item['hook'], diagnostics, producer, item.get('producer_version'), item.get('event_id'))
            else:
                event = metadata(item, diagnostics, producer)
        except (ValueError, TypeError, OverflowError, OSError):
            rejected += 1
            continue
        if event is None:
            ignored += 1
        else:
            events.append(event)
    for event in events:
        ledger.put(event)
        accepted += 1
    return {'accepted': accepted, 'ignored': ignored, 'rejected': rejected}
