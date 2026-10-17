"""Synthetic picker observations only; no Claude launch, login, or API requests."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import routing_catalog as catalog


def confirmation():
    return {'source': 'claude_code_model_picker', 'confirmed': True,
            'provider': 'claude_ai', 'producer_version': '2.1.293',
            'models': [{'id': 'haiku', 'efforts': ['default'], 'default_effort': 'default'},
                       {'id': 'sonnet', 'efforts': ['low', 'medium', 'high'], 'default_effort': 'medium'}]}


class CatalogTests(unittest.TestCase):
    def test_alias_baseline_has_no_eligibility(self):
        value = catalog.public(None)
        self.assertEqual(value['status'], 'unverified')
        self.assertEqual(value['entitlement'], 'unverified')
        self.assertEqual([m['id'] for m in value['models']], ['haiku', 'sonnet', 'opus'])
        self.assertTrue(all(not m['policy_supported'] and not m['efforts'] for m in value['models']))

    def test_import_records_source_clock_and_roundtrips_only_safe_fields(self):
        with tempfile.TemporaryDirectory() as root:
            result = catalog.import_picker(root, confirmation())
            self.assertEqual(result['status'], 'queried')
            self.assertEqual(result['source'], catalog.SOURCE)
            self.assertEqual(result['entitlement'], 'user_confirmed')
            self.assertFalse(result['scope_verified'])
            self.assertEqual(catalog.public(catalog.load(root))['models'], result['models'])
            stored = json.loads((Path(root) / 'model-catalog.json').read_text())
            stored['private_text'] = 'not for display'
            (Path(root) / 'model-catalog.json').write_text(json.dumps(stored))
            self.assertNotIn('private_text', catalog.load(root))

    def test_refresh_never_reconfirms_cached_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            catalog.import_picker(root, confirmation())
            before = (Path(root) / 'model-catalog.json').read_bytes()
            with self.assertRaisesRegex(catalog.CatalogError, 'native_picker_confirmation_required'):
                catalog.refresh(root, root)
            self.assertEqual(before, (Path(root) / 'model-catalog.json').read_bytes())

    def test_explicit_ineligible_row_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as root:
            payload = confirmation()
            payload['models'][1]['policy_supported'] = False
            with self.assertRaisesRegex(catalog.CatalogError, 'invalid_catalog'):
                catalog.import_picker(root, payload)
            self.assertFalse((Path(root) / 'model-catalog.json').exists())
            catalog.import_picker(root, confirmation())
            value = catalog.load(root)
            value['models'][1]['policy_supported'] = False
            self.assertEqual(catalog.public(value)['status'], 'unverified')
            packet = {'available_models': {'sonnet': ['high']}}
            self.assertEqual(catalog.restrict(packet, None, value)['available_models'], {})
            (Path(root) / 'model-catalog.json').write_text(json.dumps(value))
            self.assertIsNone(catalog.load(root))

    def test_rejects_unconfirmed_api_catalog_unknown_effort_and_freeform_fields(self):
        variants = []
        for changes in ({'confirmed': False}, {'source': 'anthropic_api_models'}, {'note': 'private'},
                        {'producer_version': 'my-secret'}, {'provider': 'invented'}):
            item = confirmation(); item.update(changes); variants.append(item)
        for efforts in (['ultra'], ['high', 'high'], [], ['high', 'secret']):
            item = confirmation(); item['models'][1]['efforts'] = efforts; variants.append(item)
        item = confirmation(); item['models'][1]['description'] = 'private'; variants.append(item)
        with tempfile.TemporaryDirectory() as root:
            for item in variants:
                with self.subTest(item=item), self.assertRaises(catalog.CatalogError):
                    catalog.import_picker(root, item)
            self.assertFalse((Path(root) / 'model-catalog.json').exists())

    def test_stale_future_and_forged_sources_cannot_admit(self):
        with tempfile.TemporaryDirectory() as root:
            catalog.import_picker(root, confirmation())
            value = catalog.load(root)
            for when in ('2000-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00'):
                raw = deepcopy(value); raw['queried_at'] = when
                self.assertEqual(catalog.public(raw)['status'], 'stale')
            for source in ('codex_app_server_model_list', 'anthropic_api_models', 'documented_alias_baseline'):
                raw = deepcopy(value); raw['source'] = source
                self.assertEqual(catalog.public(raw)['status'], 'unverified')

    def test_intersection_requires_fresh_confirmation_even_without_saved_selection(self):
        import routing_decision as core
        packet = {'available_models': {'haiku': ['default'], 'sonnet': ['low', 'max'], 'opus': ['high']}}
        self.assertEqual(catalog.restrict(packet, None, None)['available_models'], {})
        with tempfile.TemporaryDirectory() as root:
            catalog.import_picker(root, confirmation()); value = catalog.load(root)
            self.assertEqual(catalog.restrict(packet, None, value)['available_models'],
                             {'haiku': ['default'], 'sonnet': ['low']})
            self.assertEqual(catalog.restrict(packet, ['sonnet'], value)['available_models'], {'sonnet': ['low']})
            self.assertEqual(catalog.restrict({**packet, 'required_modalities': ['image']}, None, value)['available_models'], {})
        self.assertEqual(packet['available_models']['opus'], ['high'])
        with self.assertRaises(core.RoutingError):
            catalog.restrict({**packet, 'required_modalities': []}, None, None)

    def test_limits_duplicate_models_and_file_guard(self):
        for items in ([], [confirmation()['models'][0]] * 2,
                      [{'id': 'sk-credential', 'efforts': ['high']}],
                      [{'id': 'sonnet', 'efforts': ['high'], 'default_effort': 'max'}]):
            with self.subTest(items=items), self.assertRaises(catalog.CatalogError):
                catalog.normalize_models(items)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'model-catalog.json').write_text('{}')
            self.assertIsNone(catalog.load(root))
            (Path(root) / 'model-catalog.json').write_bytes(b'X' * (catalog.MAX_BYTES + 1))
            self.assertIsNone(catalog.load(root))


if __name__ == '__main__':
    unittest.main()
