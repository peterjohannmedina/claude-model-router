"""Offline status-line metadata snapshots; no Claude, credentials, or network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import collect_claude_statusline as collector
import routing_state as state
import routing_statusline as statusline
from test_claude_telemetry import hook
import routing_otel as hooks

SESSION = 'a73aa531-f48c-4abe-9d88-4bf520852968'
OTHER = 'be0fec5a-3dcd-42e6-907f-591834904788'
PRIVATE = 'sk-synthetic-private-transcript-and-content'


def payload(**changes):
    result = {'session_id': SESSION, 'version': '2.1.303',
        'model': {'id': 'claude-sonnet-4-6', 'display_name': PRIVATE},
        'effort': {'level': 'high', 'private': PRIVATE},
        'cost': {'total_duration_ms': 1200, 'total_api_duration_ms': 450, 'total_cost_usd': .25,
                 'total_lines_added': PRIVATE},
        'context_window': {'total_input_tokens': 1000, 'total_output_tokens': 80,
                           'current_usage': None, 'private': PRIVATE},
        'cwd': PRIVATE, 'workspace': {'current_dir': PRIVATE}, 'transcript_path': PRIVATE,
        'prompt': PRIVATE, 'last_assistant_message': PRIVATE, 'tools': [{'input': PRIVATE}]}
    result.update(changes)
    return result


def encoded(value=None):
    return json.dumps(payload() if value is None else value).encode()


class Streams:
    def __init__(self, data=b''):
        self.buffer = io.BytesIO(data)
    def write(self, text):
        return self.buffer.write(text.encode())
    def flush(self):
        pass


class StatuslineTests(unittest.TestCase):
    def test_allowlist_snapshot_semantics_and_privacy(self):
        event = statusline.metadata(payload(), 'stdin')
        self.assertEqual(event['source'], 'claude_statusline')
        self.assertEqual(event['mode'], 'statusline_snapshot')
        self.assertEqual(event['execution_status'], 'session_snapshot_observed')
        self.assertEqual(event['model_source'], 'statusline_configuration')
        self.assertEqual(event['collection_transport'], 'stdin')
        self.assertEqual(event['contract_validation'], 'documented_statusline_schema')
        self.assertEqual(event['latest_context_usage'], {'input_tokens':1000, 'output_tokens':80})
        self.assertEqual(event['session_cost_estimate_usd'], .25)
        self.assertIsNone(event['downstream_usage'])
        self.assertIsNone(event['evaluator_usage'])
        self.assertNotIn(PRIVATE, json.dumps(event))
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger = state.Ledger(root)
            self.assertEqual(statusline.ingest(encoded(), ledger, 'stdin')['changed'], 1)
            self.assertNotIn(PRIVATE.encode(), (Path(root)/'usage.sqlite3').read_bytes())
            stored = ledger.snapshot()[0]
            self.assertNotIn(PRIVATE, json.dumps(stored))
            self.assertEqual(state.totals([stored])['completed_executions'], 0)

    def test_identical_receipt_heartbeats_change_counter_and_last_seen(self):
        base = dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory(dir=ROOT) as root, patch.object(state.dt, 'datetime', wraps=dt.datetime) as clock:
            clock.now.side_effect = [base,base+dt.timedelta(seconds=1),base+dt.timedelta(seconds=2)]
            ledger = state.Ledger(root)
            first = statusline.ingest(encoded(), ledger)
            repeated = statusline.ingest(encoded(), ledger)
            value = payload(); value['cost']['total_cost_usd'] = .3
            changed = statusline.ingest(encoded(value), ledger)
            rows = ledger.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual((first['changed'],repeated['changed'],repeated['heartbeats'],changed['changed']), (1,0,1,1))
        row=rows[0]
        self.assertEqual((row['snapshot_receipts'],row['snapshot_changes']), (3,2))
        self.assertEqual(row['first_observed_at'],base.isoformat())
        self.assertEqual(row['observed_at'],(base+dt.timedelta(seconds=2)).isoformat())
        self.assertEqual(row['changed_at'],row['observed_at'])
        totals=state.totals(rows)
        self.assertEqual(totals['requests'],0)
        self.assertEqual((totals['statusline']['sessions'],totals['statusline']['receipts'],totals['statusline']['changes']), (1,3,2))
        self.assertEqual(totals['statusline']['session_cost_estimate_usd']['known'], .3)

    def test_heartbeat_only_preserves_last_change_time(self):
        base=dt.datetime.now(dt.timezone.utc)
        with tempfile.TemporaryDirectory(dir=ROOT) as root, patch.object(state.dt,'datetime',wraps=dt.datetime) as clock:
            clock.now.side_effect=[base,base+dt.timedelta(seconds=5)]
            ledger=state.Ledger(root)
            statusline.ingest(encoded(),ledger)
            statusline.ingest(encoded(),ledger)
            row=ledger.snapshot()[0]
        self.assertEqual(row['changed_at'],base.isoformat())
        self.assertEqual(row['observed_at'],(base+dt.timedelta(seconds=5)).isoformat())
        self.assertEqual(row['snapshot_changes'],1)

    def test_concurrent_receipts_preserve_one_snapshot_and_receipt_count(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger=state.Ledger(root)
            # Initialize the schema before the concurrent callback processes.
            statusline.ingest(encoded(),ledger)
            with ThreadPoolExecutor(max_workers=4) as workers:
                results=list(workers.map(lambda _:statusline.ingest(encoded(),ledger),range(12)))
            rows=ledger.snapshot()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['snapshot_receipts'],13)
        self.assertEqual(rows[0]['snapshot_changes'],1)
        self.assertEqual(sum(r['heartbeats'] for r in results),12)

    def test_missing_and_null_values_clear_old_snapshot_data(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger=state.Ledger(root)
            statusline.ingest(encoded(),ledger)
            value=payload(); value.pop('cost'); value.pop('context_window'); value.pop('effort'); value.pop('version')
            statusline.ingest(encoded(value),ledger)
            row=ledger.snapshot()[0]
            for key in ('session_duration_ms','session_api_duration_ms','session_cost_estimate_usd','latest_context_usage','effort','producer_version'):
                self.assertIsNone(row[key])
            value=payload(cost=None,context_window={'total_input_tokens':None,'total_output_tokens':None},effort=None,version=None)
            self.assertEqual(statusline.ingest(encoded(value),ledger)['changed'],0)
            totals=state.totals(ledger.snapshot())['statusline']
            self.assertEqual(totals['sessions_with_context_usage'],0)
            self.assertEqual(totals['session_duration_ms'], {'known':None,'unknown_sessions':1})

    def test_latest_only_cumulative_values_and_no_context_token_sum(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger=state.Ledger(root)
            statusline.ingest(encoded(),ledger)
            older=ledger.snapshot()[0]
            value=payload();value['cost']['total_cost_usd']=.5;value['context_window']['total_input_tokens']=10
            statusline.ingest(encoded(value),ledger)
            statusline.ingest(encoded(payload(session_id=OTHER,cost=None,context_window=None)),ledger)
            rows=ledger.snapshot()
            totals=state.totals(rows+[older])
        status=totals['statusline']
        self.assertEqual(status['sessions'],2)
        self.assertEqual(status['session_cost_estimate_usd'],{'known':.5,'unknown_sessions':1})
        self.assertEqual(status['session_duration_ms']['known'],1200)
        self.assertEqual(status['sessions_with_context_usage'],1)
        self.assertNotIn('input_tokens',json.dumps(status))
        self.assertEqual(totals['downstream']['input_tokens']['known'],0)

    def test_numeric_values_reject_wrong_types_nonfinite_and_negative(self):
        invalid=[True,False,-1,'3',[],{},float('nan'),float('inf'),10**400]
        for key in ('total_cost_usd','total_duration_ms','total_api_duration_ms'):
            for bad in invalid:
                value=payload();value['cost'][key]=bad
                with self.subTest(key=key,bad=type(bad).__name__), self.assertRaises(ValueError):
                    statusline.metadata(value)
        for key in ('total_input_tokens','total_output_tokens'):
            for bad in invalid+[1.5]:
                value=payload();value['context_window'][key]=bad
                with self.subTest(key=key,bad=type(bad).__name__), self.assertRaises(ValueError):
                    statusline.metadata(value)
        value=payload(cost={'total_duration_ms':1.25,'total_api_duration_ms':0,'total_cost_usd':0},
                      context_window={'total_input_tokens':0,'total_output_tokens':None})
        self.assertEqual(statusline.metadata(value)['session_duration_ms'],1.25)

    def test_versions_missing_are_unknown_and_invalid_versions_reject(self):
        for version in (None,'2.1.303','2.1.210','2.2.0-rc.1'):
            self.assertEqual(statusline.metadata(payload(version=version))['producer_version'],version)
        value=payload();value.pop('version')
        self.assertIsNone(statusline.metadata(value)['producer_version'])
        for version in (PRIVATE,303,[],{},'2.1.303+private'):
            with self.subTest(version=version),self.assertRaises(ValueError):statusline.metadata(payload(version=version))

    def test_private_paths_or_secrets_cannot_be_models(self):
        for model in ('C:/Users/Alice/private','C:private','/private/path','../project','C:\\private\\path','https://private.example/id',PRIVATE):
            with self.subTest(model=model),self.assertRaises(ValueError):
                statusline.metadata(payload(model={'id':model}))
        self.assertIsNone(statusline.metadata(payload(model={'id':None}))['model'])

    def test_session_identity_is_stable_unique_and_non_overrideable(self):
        event=statusline.metadata(payload())
        other=statusline.metadata(payload(session_id=OTHER))
        self.assertEqual(event['request_id'],str(uuid.uuid5(uuid.UUID(SESSION),'statusline_snapshot')))
        self.assertNotEqual(event['request_id'],other['request_id'])
        for session in (None,'unassigned','not-uuid',PRIVATE):
            with self.assertRaises(ValueError):statusline.metadata(payload(session_id=session))
        forged={**event,'session_id':OTHER}
        with self.assertRaisesRegex(ValueError,'invalid_statusline_identity'):state.sanitize(forged)

    def test_raw_unknown_execution_claims_cannot_enter_snapshot_schema(self):
        event=statusline.metadata(payload())
        forged={**event,'execution_model':'private-model','downstream_usage':{'input_tokens':999},
                'evaluator_usage':{'input_tokens':999},'downstream_attempts':9,'prompt':PRIVATE,'error':PRIVATE}
        clean=state.sanitize(forged)
        self.assertIsNone(clean['downstream_usage'])
        self.assertIsNone(clean['evaluator_usage'])
        self.assertNotIn('execution_model',clean)
        self.assertNotIn('error',clean)
        self.assertNotIn(PRIVATE,json.dumps(clean))
        with self.assertRaises(ValueError):state.sanitize({**event,'execution_status':'completed'})

    def test_json_byte_shape_duplicate_and_number_limits(self):
        for raw in (b'[]',b'{}',b'{"session_id":"a","session_id":"b"}',b'{"x":NaN}',b'{"x":1e999}',b'\xff',b'X'*(statusline.MAX_BYTES+1)):
            with self.subTest(raw=raw[:30]),self.assertRaises((ValueError,UnicodeError)):
                statusline.metadata(statusline.decode(raw))
        for changes in ({'cost':[]},{'context_window':[]},{'effort':'high'},{'effort':{'level':[]}},
                        {'model':'sonnet'},{'hook_event_name':'SessionStart'},{'resourceLogs':[]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):statusline.metadata(payload(**changes))

    def test_hooks_keep_lifecycle_semantics_and_separate_totals(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger=state.Ledger(root)
            statusline.ingest(encoded(),ledger)
            hooks.ingest(json.dumps(hook('Stop')).encode(),ledger)
            totals=state.totals(ledger.snapshot())
        self.assertEqual(totals['hooks']['observations'],1)
        self.assertEqual(totals['hooks']['stops'],1)
        self.assertEqual(totals['statusline']['sessions'],1)
        self.assertEqual(totals['requests'],0)
        self.assertEqual(totals['completed_executions'],0)

    def test_collector_api_never_opens_transcript_or_calls_network(self):
        class Sink:
            def put(self,event):self.event=event;return True
        sink=Sink()
        with patch.object(Path,'read_text',side_effect=AssertionError('transcript')),patch('urllib.request.build_opener',side_effect=AssertionError('network')):
            result=collector.collect(io.BytesIO(encoded()),sink)
        self.assertEqual(result['changed'],1)
        self.assertEqual(sink.event['collection_transport'],'stdin')
        self.assertNotIn(PRIVATE,json.dumps(sink.event))

    def run_cli(self, raw, argv=(), config=None, failure=None):
        stdin=Streams(raw);stdout=Streams();stderr=io.StringIO()
        with patch.object(sys,'stdin',stdin),patch.object(sys,'stdout',stdout),redirect_stderr(stderr), \
             patch.object(Path,'is_file',return_value=config is not None), \
             patch('routing_decision.load_file',return_value=config),patch.object(state.Ledger,'put',side_effect=failure or [True]):
            code=collector.main(['--state-dir',str(ROOT/'synthetic-never-created'),*argv])
        return code,stdout.buffer.getvalue(),stderr.getvalue()

    def test_cli_default_stdout_silent_and_diagnostics_safe(self):
        code,out,err=self.run_cli(encoded(),['--diagnostics'])
        self.assertEqual(code,0);self.assertEqual(out,b'')
        self.assertNotIn(PRIVATE,err)
        self.assertEqual(json.loads(err)['accepted'],1)

    def test_forward_stdin_exact_bytes_success_disabled_or_storage_config_failure(self):
        raw=b' \n'+encoded()+b'\r\n'
        for config,failure in ((None,None),({'schema_version':1,'observability':{'enabled':False}},None),
                               (None,OSError('private failure')),({'schema_version':False},None)):
            with self.subTest(config=config,failure=failure):
                code,out,err=self.run_cli(raw,['--forward-stdin','--diagnostics'],config,failure)
                self.assertEqual(code,0);self.assertEqual(out,raw);self.assertNotIn(PRIVATE,err)

    def test_forward_stdin_never_forwards_invalid_or_oversized_payload(self):
        for raw in (b'bad',b'[]',b'X'*(statusline.MAX_BYTES+1)):
            code,out,err=self.run_cli(raw,['--forward-stdin','--diagnostics'])
            self.assertEqual(code,0);self.assertEqual(out,b'');self.assertNotIn(PRIVATE,err)

    def test_forward_stdin_preserves_existing_display_during_schema_drift(self):
        for changes in ({'model':{'id':'C:/private/path'}},{'effort':{'level':'future-effort'}},
                        {'version':'future-version'},{'model':['new-schema']}):
            raw=encoded(payload(**changes))
            code,out,err=self.run_cli(raw,['--forward-stdin','--diagnostics'],
                                     failure=AssertionError('unsupported metadata reached storage'))
            self.assertEqual(code,0);self.assertEqual(out,raw)
            self.assertEqual(json.loads(err),{'error':'statusline_collection_failed'})
            self.assertNotIn(PRIVATE,err)

    def test_disabled_recording_never_creates_ledger_or_claims_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            ledger=state.Ledger(root,state.LedgerSettings(enabled=False))
            result=collector.collect(io.BytesIO(encoded()),ledger)
            self.assertEqual(result['accepted'],1)
            self.assertEqual((result['recorded'],result['heartbeats'],result['changed']),(0,0,0))
            self.assertFalse(ledger.path.exists())


if __name__=='__main__':unittest.main()

