"""Public synthetic data only; no provider calls or user-home state is needed."""
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import routing_dashboard as dashboard
import routing_state as state

REVIEWED_SOURCE = 'codex-model-routing:e8d686 (Claude adaptation; current working tree captured)'
FIXTURE = ROOT / 'tests/fixtures/dashboard-screenshot.json'
REVIEWED_FILES = (
    'assets/dashboard/index.html', 'assets/dashboard/app.js', 'assets/dashboard/style.css',
    'assets/dashboard/router-mark.svg', 'scripts/routing_dashboard.py', 'scripts/routing_state.py',
    'scripts/routing_runtime.py', 'scripts/routing_decision.py', 'scripts/routing_otel.py',
    'scripts/routing_catalog.py')


def reviewed_assets():
    """Fingerprint exact capture inputs without claiming an upstream byte match."""
    return {name: {'sha256': hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
            for name in REVIEWED_FILES}


def load_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    if fixture.get('synthetic') is not True: raise ValueError('synthetic_fixture_required')
    dashboard.safe_config(fixture['config'])
    if fixture['config']['local']['base_url'] != 'http://127.0.0.1:8000/v1': raise ValueError('synthetic_endpoint_required')
    sessions = {'11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'}
    for index, event in enumerate(fixture['events'], 1):
        state.sanitize(event)
        if event['session_id'] not in sessions or event['request_id'] != f'aaaaaaaa-aaaa-4aaa-8aaa-{int("aaaaaaaaaaa0",16)+index:012x}':
            raise ValueError('demo_identifiers_required')
        if not state.timestamp(event['observed_at']): raise ValueError('fixture_timestamp_required')
        if event.get('selected_candidate_id'):
            selected = next((c for c in event.get('admitted_choices', []) if c['id'] == event['selected_candidate_id']), None)
            if (not selected or any(event.get(k) != selected[k] for k in ('model', 'effort', 'adapter'))
                    or event.get('selected_action') != selected['action']):
                raise ValueError('fixture_selected_route_mismatch')
    return fixture


class ScreenshotStore(dashboard.Store):
    """Use the production backend with generated catalog metadata and observation clock."""
    def __init__(self, root, fixture):
        super().__init__(root)
        self.fixture_clock = fixture['snapshot_time']
        self.catalog_fixture = fixture.get('model_catalog')

    def snapshot(self, session=None):
        result = super().snapshot(session)
        result['observed_at'] = self.fixture_clock
        if self.catalog_fixture: result['model_catalog'] = self.catalog_fixture
        return result


def seed_fixture(root, fixture):
    store = ScreenshotStore(root, fixture)
    _, version = store.config()
    store.save(fixture['config'], version)
    settings = state.LedgerSettings.parse(fixture['config']['observability'])
    ledger = state.Ledger(root, settings)
    seed_time = time.time()
    for index, event in enumerate(fixture['events']):
        ledger.put(event)
        identity = hashlib.sha256((event['source'] + ':' + event['request_id']).encode()).hexdigest()
        with ledger.connect() as connection:
            row = json.loads(connection.execute('SELECT body FROM events WHERE id=?', (identity,)).fetchone()[0])
            row['observed_at'] = row['first_observed_at'] = event['observed_at']
            # Keep fixture rows retained on future CI runs while their displayed
            # demo dates/ordering remain fixed. No production records are used.
            connection.execute('UPDATE events SET updated=?,body=? WHERE id=?',
                               (seed_time + index / 1000, json.dumps(row), identity))
    return store
