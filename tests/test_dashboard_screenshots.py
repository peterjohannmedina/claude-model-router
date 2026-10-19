"""Synthetic-only capture inputs, validated without a browser or providers."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from dashboard_screenshot_fixture import ROOT, load_fixture, reviewed_assets, seed_fixture


class ScreenshotFixture(unittest.TestCase):
    def test_fixture_selected_route_mismatch_is_rejected(self):
        fixture = load_fixture()
        event = next(e for e in fixture['events'] if e.get('selected_candidate_id'))
        event['model'] = 'different-model'
        with patch('dashboard_screenshot_fixture.json.loads', return_value=fixture):
            with self.assertRaisesRegex(ValueError, 'fixture_selected_route_mismatch'): load_fixture()

    @unittest.skipUnless((ROOT / 'docs/images/provenance.json').exists(), 'Screenshot capture is optional')
    def test_published_capture_matches_current_inputs_and_images(self):
        provenance = json.loads((ROOT / 'docs/images/provenance.json').read_text())
        self.assertTrue(provenance['synthetic'])
        self.assertFalse(provenance['real_user_data'])
        self.assertEqual(provenance['provider_calls'], 0)
        self.assertEqual(provenance['external_browser_requests'], 0)
        for name, metadata in provenance['assets'].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), metadata['sha256'], name)
        for name, metadata in provenance['images'].items():
            self.assertEqual(Path(name).name, name)
            self.assertEqual(hashlib.sha256((ROOT / 'docs/images' / name).read_bytes()).hexdigest(), metadata['sha256'], name)

    def test_current_capture_inputs_are_fingerprinted(self):
        assets = reviewed_assets()
        self.assertEqual(len(assets), 10)
        self.assertTrue(all(len(meta['sha256']) == 64 for meta in assets.values()))

    def test_fixture_uses_only_synthetic_metadata_without_providers(self):
        fixture = load_fixture()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = seed_fixture(Path(directory), fixture).snapshot()
            self.assertEqual(snapshot['observed_at'], fixture['snapshot_time'])
            self.assertEqual(snapshot['totals']['requests'], 8)
            self.assertEqual(snapshot['totals']['completed_executions'], 4)
            self.assertEqual(snapshot['totals']['evaluator']['input_tokens']['known'], 1070)
            self.assertEqual(snapshot['totals']['downstream']['input_tokens']['known'], 23320)
            self.assertEqual(len(snapshot['sessions']), 2)
            self.assertTrue(fixture['synthetic'])
            self.assertEqual(snapshot['collector_health']['hooks']['receipt_status'], 'recent')
            self.assertEqual(snapshot['collector_health']['statusline']['receipt_status'], 'recent')
            self.assertEqual(snapshot['totals']['statusline']['sessions'], 1)
            self.assertEqual(snapshot['session_observations'][1]['configuration']['model'], 'haiku')


if __name__ == '__main__': unittest.main()
