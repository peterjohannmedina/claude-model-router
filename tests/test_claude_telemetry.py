"""Synthetic documented hooks: lifecycle observations, no inference or transcript reads."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect_claude_hook as collector
import routing_otel as hooks
import routing_state as state

SESSION = 'a73aa531-f48c-4abe-9d88-4bf520852968'
PROMPT = 'ef96ea58-a22e-426c-bf1b-185e84d99894'
SECRET = 'sk-synthetic-never-persist-raw-content'


def hook(name='SessionStart', **changes):
    value = {'session_id': SESSION, 'hook_event_name': name,
             'transcript_path': SECRET, 'cwd': SECRET, 'last_assistant_message': SECRET,
             'compact_summary': SECRET, 'prompt': SECRET, 'tool_input': {'command': SECRET},
             'source': 'startup', 'model': 'claude-sonnet-5-5', 'version': SECRET,
             'stop_hook_active': False, 'trigger': 'auto', 'reason': 'other', 'agent_id': 'agent-test'}
    value.update(changes)
    return value


class TelemetryTests(unittest.TestCase):
    def test_lifecycle_allowlist_and_summary_privacy(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root)
            for name in hooks.EVENT_CLASSES:
                value = hook(name)
                if name == 'PostModelSwitch':
                    value.update(source='auto', from_model='claude-sonnet-5-5', to_model='claude-opus-5-5')
                with self.subTest(name=name):
                    result = hooks.ingest(json.dumps(value).encode(), ledger)
                    self.assertEqual(result, {'accepted': 1, 'ignored': 0, 'rejected': 0})
            events = ledger.snapshot()
            self.assertEqual(len(events), len(hooks.EVENT_CLASSES))
            self.assertNotIn(SECRET, json.dumps(events))
            self.assertNotIn(SECRET.encode(), (Path(root) / 'usage.sqlite3').read_bytes())
            for event in events:
                self.assertEqual(event['source'], 'claude_hook')
                self.assertNotEqual(event['execution_status'], 'completed')
                self.assertIsNone(event.get('downstream_usage'))
                self.assertIsNone(event.get('downstream_attempts'))
            self.assertEqual(state.totals(events)['completed_executions'], 0)

    def test_only_session_and_switch_report_model(self):
        start = hooks.metadata(hook())
        self.assertEqual(start['model'], 'claude-sonnet-5-5')
        stop = hooks.metadata(hook('Stop', effort={'level': 'high'}))
        self.assertNotIn('model', stop)
        self.assertEqual(stop['effort'], 'high')
        self.assertNotIn('model', hooks.metadata(hook(model=None)))

    def test_model_fields_cannot_persist_private_paths(self):
        private_path = 'C:/Users/Alice/private-project'
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root); diagnostics = {}; producer = {}
            result = hooks.ingest(json.dumps(hook(model=private_path)).encode(), ledger, diagnostics, producer)
            self.assertEqual(result['accepted'], 1)
            self.assertIsNone(ledger.snapshot()[0].get('model'))
            for field in ('from_model', 'to_model'):
                value = hook('PostModelSwitch', source='auto', from_model='claude-sonnet-5-5', to_model='claude-opus-5-5')
                value[field] = private_path
                result = hooks.ingest(json.dumps(value).encode(), ledger, diagnostics, producer)
                self.assertEqual(result['ignored'], 1)
            self.assertNotIn(private_path, json.dumps(ledger.snapshot()) + json.dumps(diagnostics) + json.dumps(producer))
            self.assertNotIn(private_path.encode(), (Path(root) / 'usage.sqlite3').read_bytes())

    def test_versions_are_annotations_not_inferred_from_input(self):
        event = hooks.metadata(hook())
        self.assertNotIn('producer_version', event)
        event = hooks.metadata(hook(), producer_version='2.1.293')
        self.assertEqual(event['producer_version'], '2.1.293')
        self.assertEqual(event['contract_validation'], 'documented_hook_schema')
        self.assertIsNone(hooks.metadata(hook(), producer_version=SECRET))
        value = hook('PostModelSwitch', source='auto', from_model='sonnet', to_model='opus')
        self.assertIsNone(hooks.metadata(value, producer_version='2.1.250'))
        self.assertIsNotNone(hooks.metadata(value, producer_version='2.1.251'))

    def test_replay_dedup_and_distinct_prompt_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root)
            value = hook('Stop', prompt_id=PROMPT)
            hooks.ingest(json.dumps(value).encode(), ledger)
            hooks.ingest(json.dumps(value).encode(), ledger)
            self.assertEqual(len(ledger.snapshot()), 1)
            value['prompt_id'] = str(uuid.uuid4())
            hooks.ingest(json.dumps(value).encode(), ledger)
            self.assertEqual(len(ledger.snapshot()), 2)

    def test_explicit_event_id_is_namespaced_by_session_and_replay_safe(self):
        other_session = 'be0fec5a-3dcd-42e6-907f-591834904788'
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root)
            for session in (SESSION, other_session, SESSION):
                envelope = {'hook': hook(session_id=session), 'event_id': PROMPT}
                self.assertEqual(hooks.ingest(json.dumps(envelope).encode(), ledger)['accepted'], 1)
            events = ledger.snapshot()
            self.assertEqual(len(events), 2)
            self.assertEqual({e['session_id'] for e in events}, {SESSION, other_session})
            self.assertEqual(len({e['request_id'] for e in events}), 2)
            self.assertNotIn(PROMPT, {e['request_id'] for e in events})

    def test_unsupported_payloads_are_not_accepted_as_execution(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root); diagnostics = {}; producer = {}
            result = hooks.ingest(json.dumps({'events': [hook('UserPromptSubmit'), {'resourceLogs': []},
                hook('Stop', stop_hook_active='false'), hook(session_id=SECRET)]}).encode(), ledger, diagnostics, producer)
            self.assertEqual(result, {'accepted': 0, 'ignored': 3, 'rejected': 1})
            self.assertEqual(ledger.snapshot(), [])
            self.assertNotIn(SECRET, json.dumps(diagnostics) + json.dumps(producer))

    def test_collector_never_opens_transcript_and_bounded_input(self):
        class MemoryLedger:
            def put(self, value):
                self.value = value
        ledger = MemoryLedger()
        with patch.object(Path, 'read_text', side_effect=AssertionError('transcript read')):
            result = collector.collect(io.BytesIO(json.dumps(hook()).encode()), ledger)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(ledger.value['collection_transport'], 'stdin')
        self.assertNotIn(SECRET, json.dumps(ledger.value))
        with self.assertRaisesRegex(ValueError, 'body_limit'):
            collector.collect(io.BytesIO(b'X' * (hooks.MAX_BYTES + 1)), ledger)

    def test_json_ambiguity_limits_and_envelope(self):
        for raw in (b'{"session_id":"a","session_id":"b"}', b'{"n":NaN}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                hooks.decode(raw)
        with tempfile.TemporaryDirectory() as root:
            ledger = state.Ledger(root)
            envelope = {'hook': hook(), 'event_id': PROMPT, 'producer_version': '2.1.293'}
            self.assertEqual(hooks.ingest(json.dumps(envelope).encode(), ledger)['accepted'], 1)
            self.assertEqual(ledger.snapshot()[0]['request_id'],
                             str(uuid.uuid5(uuid.UUID(SESSION), 'explicit:' + PROMPT)))


if __name__ == '__main__':
    unittest.main()
