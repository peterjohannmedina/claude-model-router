"""Render the reviewed UI in isolated CI Chromium, using only public synthetic data."""
import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit
from unittest.mock import patch

from dashboard_screenshot_fixture import ROOT, FIXTURE, REVIEWED_SOURCE, reviewed_assets, load_fixture, seed_fixture
import routing_dashboard as dashboard
import routing_runtime as runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--chromium-executable', type=Path,
                        help='Optional existing Chromium executable; uses a fresh isolated profile')
    parser.add_argument('--validate-fixture-only', action='store_true')
    args = parser.parse_args(argv)
    stage = 'validate-fixture'
    try:
        assets, fixture = reviewed_assets(), load_fixture()
        if args.validate_fixture_only:
            print(json.dumps({'synthetic_fixture_valid': True, 'reviewed_source': REVIEWED_SOURCE,
                              'reviewed_files_verified': len(assets), 'events': len(fixture['events']), 'provider_calls': 0}))
            return 0
        if args.output_dir is None: parser.error('--output-dir is required for capture')
        from playwright.sync_api import sync_playwright, expect
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='routing-screenshot-') as directory:
            stage = 'seed-synthetic-state'
            store = seed_fixture(Path(directory), fixture)
            server = dashboard.Server(0, store)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
            thread.start()
            blocked, paths, errors, provider_attempts = [], set(), [], []
            config_posts, credential_transmissions, invalid_save_checks = [], [], []
            synthetic_keys = ('sk-synthetic-decisions-do-not-transmit', 'sk-synthetic-local-do-not-transmit')
            def forbidden_provider(*_args, **_kwargs):
                provider_attempts.append(True)
                raise AssertionError('providers_are_forbidden_in_capture')
            try:
                with (patch.object(runtime, 'local_request', side_effect=forbidden_provider),
                      patch.object(runtime.core.DecisionsProvider, 'choose', side_effect=forbidden_provider),
                      sync_playwright() as playwright):
                    stage = 'launch-chromium'
                    browser = playwright.chromium.launch(**({'executable_path': str(args.chromium_executable)}
                        if args.chromium_executable else {}))
                    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1,
                                                  locale='en-US', timezone_id='UTC', color_scheme='light', service_workers='block')
                    def only_dashboard(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        method, path = request.method, parsed.path
                        if method == 'POST' and path == '/api/config': config_posts.append(True)
                        if any(key in request.url or key in (request.post_data or '') for key in synthetic_keys):
                            credential_transmissions.append(True)
                            route.abort()
                            return
                        allowed = ((method == 'GET' and path in ('/', '/app.js', '/style.css', '/router-mark.svg', '/favicon.ico', '/api/session', '/api/state'))
                                   or (method == 'POST' and path == '/api/auth'))
                        if parsed.scheme != 'http' or parsed.netloc != urlsplit(server.origin).netloc or not allowed:
                            blocked.append(True)
                            route.abort()
                            return
                        paths.add(method + ' ' + path)  # Never headers, query, fragment or tokens.
                        route.continue_()
                    context.route('**/*', only_dashboard)
                    page = context.new_page()
                    page.on('pageerror', lambda _error: errors.append(True))
                    page.set_default_timeout(15000)
                    stage = 'authorize-and-render'
                    # HTTP entry matches the private-file handoff, without a file/data
                    # URL or user profile. The nonce is temporary CI process state only.
                    page.goto(server.origin + '/#authorize=' + server.bootstrap, wait_until='domcontentloaded')
                    expect(page.locator('#requests')).to_have_text('8')
                    expect(page.locator('#executions')).to_have_text('4')
                    expect(page.locator('#connection')).to_have_text('Dashboard connected · 2s')
                    expect(page.locator('#otel-receiver')).to_have_text('Receiver disabled')
                    expect(page.locator('#hooks-health')).to_contain_text('Declared configured; Recent receipt')
                    expect(page.locator('#statusline-health')).to_contain_text('Declared configured; Recent receipt')
                    expect(page.locator('#statusline-summary')).to_contain_text('not summed usage')
                    expect(page.locator('#otel-summary')).to_contain_text('1 sessions with retained hook metadata; 2 lifecycle observations;')
                    expect(page).to_have_url(server.origin + '/')
                    expect(page.locator('#message')).to_be_empty()
                    expect(page.locator('#save')).to_be_enabled()
                    expect(page.get_by_role('heading',name='Claude routing desk',exact=True)).to_be_visible()
                    expect(page.locator('.router-mark')).to_be_visible()
                    assert page.locator('.router-mark').evaluate('(image)=>image.complete && image.naturalWidth > 0')
                    expect(page.locator('#native-managed')).to_be_checked()
                    expect(page.locator('#catalog-models input')).to_have_count(3)
                    expect(page.locator('#catalog-status')).to_contain_text('Current access, scope and remote freshness remain unverified')
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')

                    stage = 'capture-overview'
                    overview = page.locator('#overview').bounding_box()
                    height = math.ceil(overview['y'] + overview['height'] + 20)
                    if height > 1100: page.set_viewport_size({'width': 1440, 'height': height})
                    page.screenshot(path=str(output / 'dashboard-overview.png'), clip={'x': 0, 'y': 0, 'width': 1440, 'height': height}, animations='disabled')

                    stage = 'providers-navigation-refresh'
                    page.set_viewport_size({'width': 1440, 'height': 2000})
                    page.get_by_role('link', name='Models & providers', exact=True).click()
                    page.reload(wait_until='domcontentloaded')
                    expect(page.locator('#requests')).to_have_text('8')
                    expect(page.locator('#save')).to_be_enabled()
                    expect(page.locator('#decisions-enabled')).to_be_checked()
                    expect(page.locator('#local-enabled')).to_be_checked()
                    expect(page.locator('#local-model')).to_have_value('synthetic-local-model')
                    page.locator('#providers').screenshot(path=str(output / 'dashboard-providers.png'), animations='disabled')

                    stage = 'native-invalid-save-regression'
                    original_config = store.path.read_bytes()
                    cases = [('decisions-key-env', synthetic_keys[0], 'OPENAI_API_KEY', 'invalid_key_environment_name'),
                             ('decisions-key-env', '', 'OPENAI_API_KEY', 'missing_key_environment_name'),
                             ('local-key-env', synthetic_keys[1], 'LOCAL_LLM_API_KEY', 'invalid_key_environment_name')]
                    for field, value, corrected, code in cases:
                        control = page.locator('#' + field)
                        control.fill(value)
                        # An actual button click exercises native required/pattern
                        # validation, which fires invalid without firing submit.
                        page.locator('#save').click()
                        expect(control).to_have_attribute('aria-invalid', 'true')
                        expect(page.locator('#save-status')).to_contain_text(dashboard.CONFIG_HINTS[code])
                        assert control.evaluate('(node) => node.validationMessage') == dashboard.CONFIG_HINTS[code]
                        displayed = page.locator('#save-status').inner_text()
                        assert all(key not in displayed for key in synthetic_keys)
                        assert not config_posts and not credential_transmissions
                        assert store.path.read_bytes() == original_config
                        control.fill(corrected)
                        expect(control).to_have_attribute('aria-invalid', 'false')
                        assert control.evaluate('(node) => node.validationMessage') == ''
                        assert control.evaluate('(node) => node.checkValidity()')
                        invalid_save_checks.append(field + ':' + code)
                    assert page.locator('#config-form').evaluate('(node) => node.checkValidity()')

                    stage = 'activity-navigation-refresh'
                    page.get_by_role('link', name='Activity', exact=True).click()
                    page.reload(wait_until='domcontentloaded')
                    expect(page.locator('#requests')).to_have_text('8')
                    expect(page.locator('#events details')).to_have_count(11)
                    decision = next(e for e in fixture['events'] if e.get('confidence') is not None)
                    decision_row = page.locator('#events details').filter(has=page.get_by_text(decision['request_id'], exact=True))
                    decision_row.locator('summary').click()
                    expect(decision_row).to_contain_text('Evaluator confidence (not task quality)')
                    expect(decision_row).to_contain_text('0.780')
                    expect(decision_row).to_contain_text('probability 0.220')
                    expect(decision_row).to_contain_text('110 / 125')
                    page.locator('#activity').screenshot(path=str(output / 'dashboard-activity.png'), animations='disabled')
                    stage = 'expanded-observation-polling-regression'
                    observation_key = decision_row.evaluate('(node) => node.routingObservationKey')
                    request_id = json.loads(observation_key)[2]
                    expanded = page.locator('#events details').filter(has=page.get_by_text(request_id, exact=True))
                    summary = expanded.locator('summary')
                    summary.focus()
                    for _ in range(2):
                        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/state'):
                            pass
                        expect(expanded).to_have_attribute('open', '')
                        expect(summary).to_be_focused()
                    # A new generated row enters through the real local backend,
                    # so the existing observation moves without DOM substitution.
                    extra = dict(fixture['events'][0], request_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
                    dashboard.state.Ledger(store.root).put(extra)
                    expect(page.locator('#events details')).to_have_count(12)
                    expect(expanded).to_have_attribute('open', '')
                    expect(summary).to_be_focused()
                    summary.press('Enter')
                    for _ in range(2):
                        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/state'):
                            pass
                        expect(expanded).not_to_have_attribute('open', '')
                        expect(summary).to_be_focused()
                    assert not blocked and not errors and not provider_attempts
                    assert not config_posts and not credential_transmissions
                    browser_version = browser.version
                    context.close()
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
        stage = 'write-provenance'
        images = {}
        for name in ('dashboard-overview.png', 'dashboard-providers.png', 'dashboard-activity.png'):
            raw = (output / name).read_bytes()
            assert raw[:8] == b'\x89PNG\r\n\x1a\n'
            width, height = struct.unpack('>II', raw[16:24])
            images[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'width': width, 'height': height, 'bytes': len(raw)}
        provenance = {'synthetic': True, 'reviewed_source': REVIEWED_SOURCE,
            'capture_source_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
            'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), 'assets': assets, 'images': images,
            'playwright_version': importlib.metadata.version('playwright'), 'browser': 'Chromium ' + browser_version,
            'locale': 'en-US', 'timezone': 'UTC', 'device_scale_factor': 1, 'viewport_width': 1440,
            'fixture_clock': fixture['snapshot_time'], 'network_paths': sorted(paths),
            'provider_calls': 0, 'external_browser_requests': 0, 'real_user_data': False,
            'browser_invalid_save_cases': invalid_save_checks, 'configuration_posts': len(config_posts),
            'credential_transmissions': len(credential_transmissions), 'invalid_save_config_unchanged': True,
            'browser_expansion_polling_regression': True,
            'note': 'Current fingerprinted HTML/CSS/JS and backend; only generated fixtures and the displayed observation clock are synthetic. No image editing or UI DOM substitution.'}
        (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'captured': list(images), 'reviewed_source': REVIEWED_SOURCE, 'provider_calls': 0}))
        return 0
    except Exception as error:
        # Playwright error messages can include the temporary bootstrap URL.
        # Never upload traces/storage or print raw browser exceptions.
        print(json.dumps({'capture_failed': True, 'stage': stage, 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
