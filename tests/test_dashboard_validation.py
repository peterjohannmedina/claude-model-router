"""Synthetic settings validation never handles a real provider credential."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import routing_dashboard as dashboard


class ConfigurationDiagnostics(unittest.TestCase):
    def test_literal_credentials_rejected_without_echo_or_write(self):
        for group in ('decisions', 'local'):
            with self.subTest(group=group), tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
                store = dashboard.Store(directory)
                config, version = store.config()
                fake = 'sk-proj-synthetic-not-a-real-key-123456'
                config[group]['api_key_env'] = fake
                with self.assertRaises(dashboard.ConfigValidationError) as caught:
                    store.save(config, version)
                result = caught.exception.payload()
                self.assertEqual(result['error'], 'invalid_key_environment_name')
                self.assertEqual(result['field'], group + '.api_key_env')
                self.assertNotIn(fake, json.dumps(result))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_other_invalid_fields_report_static_diagnostics(self):
        cases = [('decisions', 'minimum_confidence', 2, 'invalid_confidence_threshold'),
                 ('local', 'timeout_seconds', 0, 'invalid_timeout'),
                 ('local', 'base_url', 'https://user:synthetic-password@example.invalid', 'sensitive_endpoint'),
                 ('local', 'base_url', 'https://example.invalid:bad', 'invalid_local_url'),
                 ('local', 'efforts', ['unsupported'], 'invalid_local_efforts')]
        for group, field, value, code in cases:
            with self.subTest(field=field):
                config = dashboard.safe_config({'schema_version': 1})
                config[group][field] = value
                with self.assertRaises(dashboard.ConfigValidationError) as caught:
                    dashboard.safe_config(config)
                result = caught.exception.payload()
                self.assertEqual((result['error'], result['field']), (code, group + '.' + field))
                self.assertNotIn('synthetic-password', json.dumps(result))

    def test_unknown_configuration_names_never_echo(self):
        with self.assertRaises(dashboard.ConfigValidationError) as caught:
            dashboard.safe_config({'schema_version': 1, 'decisions': {'synthetic-private-field': 'synthetic-private-value'}})
        result = caught.exception.payload()
        self.assertEqual(result['error'], 'invalid_or_sensitive_configuration')
        self.assertNotIn('synthetic-private', json.dumps(result))

    def test_valid_names_save_all_provider_states_without_credentials_or_ledger(self):
        for decisions in (False, True):
            for local in (False, True):
                with self.subTest(decisions=decisions, local=local), tempfile.TemporaryDirectory(dir=ROOT.parent) as directory:
                    store = dashboard.Store(directory)
                    config, version = store.config()
                    config['decisions'].update(enabled=decisions, api_key_env='OPENAI_API_KEY')
                    config['local'].update(enabled=local, api_key_env='LOCAL_ROUTING_KEY')
                    saved, _ = store.save(config, version)
                    self.assertEqual(saved['decisions']['api_key_env'], 'OPENAI_API_KEY')
                    self.assertEqual([p.name for p in Path(directory).iterdir()], ['config.json'])
