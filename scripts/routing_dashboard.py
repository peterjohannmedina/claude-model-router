"""One loopback dashboard for the local routing config and redacted usage ledger."""
import argparse
from dataclasses import asdict
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
import json
import os
import re
from pathlib import Path
import secrets
import tempfile
import threading
import time
import webbrowser
from urllib.parse import urlsplit, parse_qs

import routing_decision as core
import routing_runtime as runtime
import routing_state as state
import routing_otel as otel
import routing_catalog as catalog

ASSETS = Path(__file__).resolve().parents[1] / 'assets/dashboard'


def package_version():
    try:
        value = (ASSETS.parents[1] / 'VERSION').read_text(encoding='ascii').strip()
        return value if re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{1,3}', value) else 'unverified'
    except (OSError, UnicodeError): return 'unverified'

CONFIG_HINTS = {
    'invalid_key_environment_name': 'Enter an environment-variable name such as OPENAI_API_KEY, not an API key value.',
    'missing_key_environment_name': 'Enter OPENAI_API_KEY or another environment-variable name; store the key separately.',
    'invalid_timeout': 'Use a timeout between 0.05 and 10 seconds.',
    'invalid_confidence_threshold': 'Use a minimum confidence between 0 and 1.',
    'invalid_evaluator_token_cap': 'Use a non-negative whole number or leave the input-token cap blank.',
    'invalid_evaluator_cost_cap': 'Use a cost cap between 0 and 1000000 USD or leave it blank.',
    'invalid_output_budget': 'Use a whole-number output limit between 1 and 4096.',
    'invalid_local_efforts': 'Use supported effort names, separated by commas, such as default.',
    'invalid_local_model': 'Use a nonsecret model ID of at most 256 characters.',
    'invalid_local_url': 'Use an HTTP(S) base URL without credentials, query strings or fragments.',
    'sensitive_model_label': 'Use a nonsecret model ID; do not include credential-like values.',
    'sensitive_endpoint': 'Use a nonsecret HTTP(S) base URL without credentials, query strings or fragments.',
    'invalid_observability_configuration': 'Check history retention (1–30 days), maximum requests (10–5000), and non-negative rates.',
    'invalid_config_flag': 'Provider and history toggles must be enabled or disabled.',
    'invalid_native_models': 'Select distinct model IDs from your confirmed Claude /model picker.',
}


class ConfigValidationError(ValueError):
    def __init__(self, code, field=None):
        self.code = code if code in CONFIG_HINTS else 'invalid_or_sensitive_configuration'
        self.field = field
        super().__init__(self.code)

    def payload(self):
        return {'error': self.code, 'field': self.field,
                'message': CONFIG_HINTS.get(self.code, 'Check provider and history settings. No configuration was saved.')}


def config_error_field(raw, code):
    if not isinstance(raw, dict): return None
    if code in ('invalid_key_environment_name', 'missing_key_environment_name', 'invalid_timeout'):
        for group in ('decisions', 'local'):
            value = raw.get(group, {})
            if not isinstance(value, dict): continue
            if code == 'invalid_timeout':
                if not core.number(value.get('timeout_seconds', 3 if group == 'decisions' else 2), 0.05, 10):
                    return group + '.timeout_seconds'
            else:
                name = value.get('api_key_env', 'OPENAI_API_KEY' if group == 'decisions' else None)
                if ((name is None and group == 'decisions') or
                        (name is not None and (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', name)))):
                    return group + '.api_key_env'
    return {'invalid_confidence_threshold':'decisions.minimum_confidence',
            'invalid_evaluator_token_cap':'decisions.max_input_tokens', 'invalid_evaluator_cost_cap':'decisions.max_cost_usd',
            'invalid_output_budget':'local.max_output_tokens', 'invalid_local_efforts':'local.efforts',
            'invalid_local_model':'local.model', 'invalid_local_url':'local.base_url',
            'invalid_observability_configuration':'observability', 'invalid_config_flag':'providers',
            'invalid_native_models':'native.allowed_models'}.get(code)


def config_version(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def safe_config(raw):
    try: config = runtime.Config.parse(raw)
    except core.RoutingError as error:
        code = str(error)
        raise ConfigValidationError(code, config_error_field(raw, code)) from None
    for field, value in (('local.model', config.local.model), ('local.base_url', config.local.base_url)):
        if value and state.SECRET.search(value):
            raise ConfigValidationError('sensitive_model_label' if field == 'local.model' else 'sensitive_endpoint', field)
    if config.local.base_url:
        # URL shape is checked even while a provider is disabled; never echo URL credentials.
        try:
            parsed = urlsplit(config.local.base_url)
            parsed.port  # Validate a supplied port before persisting or echoing it.
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment): raise ValueError()
        except ValueError: raise ConfigValidationError('invalid_local_url', 'local.base_url') from None
    return {'schema_version': 1, **asdict(config)}


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if any((p / '.git').exists() for p in (self.root, *self.root.parents)):
            raise ValueError('state_must_be_outside_repository')
        self.path = self.root / 'config.json'
        self.lock = threading.Lock()
        self.catalog_error = None
        self.otel_status = {'enabled': False, 'last_received_at': None, 'last_observation_at': None,
            'accepted_records': 0, 'ignored_records': 0, 'rejected_records': 0,
            'requests': 0, 'unauthorized_requests': 0, 'invalid_batches': 0, 'storage_errors': 0,
            'validated_batches': 0, 'recording_disabled_batches': 0,
            **{reason + '_records': 0 for reason in otel.IGNORE_REASONS},
            'supported_cli_versions': list(otel.SUPPORTED_VERSIONS), 'exporter_configuration': 'unverified',
            'schema_compatible_series': list(otel.SCHEMA_SERIES),
            'runtime_version': package_version(),
            'running_revision': None, 'producer_metadata': {'versions': {}, 'events': {}, 'features': {}},
            'started_at': state.dt.datetime.now(state.dt.timezone.utc).isoformat(),
            'state_id': hashlib.sha256(str(self.root).encode()).hexdigest()[:12]}

    def runtime_identity(self):
        """Compare the actual server's loaded revision with the install receipt."""
        installed = None
        try:
            value = json.loads((self.root/'dashboard-service'/'installation.json').read_text(encoding='utf-8'))
            revision = value.get('revision')
            if isinstance(revision, str) and re.fullmatch(r'[0-9.]+\+[0-9a-f]{16}', revision): installed = revision
        except (OSError, ValueError, TypeError, AttributeError): pass
        running = self.otel_status.get('running_revision')
        return {'pid': os.getpid(), 'installed_revision': installed,
                'restart_required': installed != running if installed is not None and running is not None else None}

    def write_receiver_status(self):
        """Fixed metadata/counters only; never raw export data, IDs or capabilities."""
        path = self.root / 'receiver-status.json'
        try:
            if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400): return
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='.receiver-status-', dir=self.root)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump({'schema_version': 1, 'kind': 'routing_receiver', **self.otel_status,
                               **self.runtime_identity()}, stream)
                os.replace(name, path)
            finally: Path(name).unlink(missing_ok=True)
        except OSError: pass  # Diagnostics never turn accepted ingestion into failure.

    def config(self):
        config = safe_config(core.load_file(self.path)) if self.path.is_file() else safe_config({'schema_version': 1})
        version = config_version(config)
        return config, version

    def save(self, raw, expected):
        config = safe_config(raw)
        with self.lock:
            _, current = self.config()
            if not isinstance(expected, str) or not hmac.compare_digest(current, expected): raise FileExistsError()
            self.root.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt': self.root.chmod(0o700)
            descriptor, name = tempfile.mkstemp(prefix='.config-', dir=self.root)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(config, stream, indent=2, allow_nan=False)
                    stream.write('\n')
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
        # Return the version of this write, not a later concurrent writer's state.
        return config, config_version(config)

    def snapshot(self, session=None):
        config, version = self.config()
        settings = state.LedgerSettings.parse(config['observability'])
        all_events = state.Ledger(self.root, settings).snapshot()
        sessions = sorted({e['session_id'] for e in all_events})
        events = [e for e in all_events if session is None or e['session_id'] == session]
        daily = {}
        for event in events:
            day = event.get('first_observed_at', event['observed_at'])[:10]
            daily.setdefault(day, []).append(event)
        model_catalog = catalog.public(catalog.load(self.root))
        if self.catalog_error: model_catalog.update(status='error',last_error=self.catalog_error)
        return {'config': config, 'config_version': version, 'events': events, 'model_catalog':model_catalog,
                'totals': state.totals(events, settings), 'sessions': sessions, 'selected_session': session,
                'daily': [{'day': day, **state.totals(items, settings)} for day, items in sorted(daily.items())],
                'observed_at': state.dt.datetime.now(state.dt.timezone.utc).isoformat(),
                'coverage': {'router': 'instrumented_runtime_only', 'claude_exec': 'unsupported_no_verified_collector',
                             'claude_hooks': 'explicit_opt_in_event_metadata_only',
                             'interactive_tui': 'unobserved_without_hooks',
                             'claude_desktop': 'unsupported_no_verified_feed',
                             'automatic_dispatch': 'unsupported_recommendations_only'},
                'refresh_seconds': 2}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def server_bind(self):
        # HTTPServer's getfqdn() can stall on host DNS. This listener is always
        # numeric loopback and needs no hostname resolution or network lookup.
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]

    def __init__(self, port, store, otel_capability=None, control_capability=None, instance_id=None, revision=None):
        self.store = store
        self.otel_capability = otel.capability(otel_capability) if otel_capability is not None else None
        self.otel_lock = threading.Lock()
        self.control_capability = otel.capability(control_capability) if control_capability is not None else None
        self.instance_id, self.revision = instance_id, revision
        self.store.otel_status['running_revision'] = revision
        self.store.otel_status['enabled'] = self.otel_capability is not None
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.bootstrap = secrets.token_urlsafe(32)
        self.bootstrap_until = time.monotonic() + 300
        self.bootstrap_used = False
        self.bootstrap_lock = threading.Lock()
        self.launch_path = None
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.cookie_name = 'routing_' + str(self.server_port)
        self.last_status_write = 0
        self.record_otel()

    def record_producer(self, producer):
        with self.otel_lock:
            target = self.store.otel_status['producer_metadata']
            for kind in ('versions', 'events', 'features'):
                for label, amount in producer.get(kind, {}).items():
                    counts = target[kind]
                    if label not in counts and len(counts) >= otel.VERSION_TALLY_LIMIT: label = 'other'
                    counts[label] = counts.get(label, 0) + amount

    def record_otel(self, received=False, observed=False, **increments):
        with self.otel_lock:
            status = self.store.otel_status
            first_failure = any(name in ('unauthorized_requests', 'invalid_batches', 'storage_errors') and
                status[name] == 0 and amount > 0 for name, amount in increments.items())
            for name, amount in increments.items():
                if name not in status or type(status[name]) is not int or type(amount) is not int:
                    raise ValueError('invalid_receiver_counter')
                status[name] += amount
            if received: status['last_received_at'] = state.dt.datetime.now(state.dt.timezone.utc).isoformat()
            if observed: status['last_observation_at'] = status['last_received_at']
            # Bound writes for unauthenticated or invalid request bursts. The
            # authenticated UI always reads current in-memory counters.
            now = time.monotonic()
            if received or first_failure or now - self.last_status_write >= 1:
                self.store.write_receiver_status()
                self.last_status_write = now

    def create_launch_file(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt': self.store.root.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix='.dashboard-launch-', suffix='.html', dir=self.store.root)
        self.launch_path = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=' +
                         self.origin + '/#authorize=' + self.bootstrap + '"><title>Opening local routing dashboard</title>Opening the local routing dashboard…')
        return self.launch_path

    def authorize(self, token):
        with self.bootstrap_lock:
            try:
                if (self.bootstrap_used or time.monotonic() > self.bootstrap_until
                        or not hmac.compare_digest(token, self.bootstrap)): return False
            except TypeError: return False
            self.bootstrap_used = True
            if self.launch_path:
                try: self.launch_path.unlink(missing_ok=True)
                except OSError: pass
            return True

    def renew_launch_file(self):
        with self.bootstrap_lock:
            if self.launch_path: self.launch_path.unlink(missing_ok=True)
            self.bootstrap = secrets.token_urlsafe(32)
            self.bootstrap_until = time.monotonic() + 300
            self.bootstrap_used = False
            return self.create_launch_file()

    def server_close(self):
        if self.launch_path:
            try: self.launch_path.unlink(missing_ok=True)
            except OSError: pass
        super().server_close()

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try: super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.connections.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = 'RoutingDashboard'
    def log_message(self, *_): pass  # No request, token, endpoint or payload logging.

    def reply(self, status, body, content_type='application/json', cookie=False):
        if not isinstance(body, bytes): body = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', self.server.cookie_name + '=' + self.server.session + '; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, mutate=False, session=True, public=False):
        hosts = self.headers.get_all('Host', [])
        if hosts != [self.server.origin.removeprefix('http://')]: return False
        if not public and self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin'): return False
        origin = self.headers.get('Origin')
        if len(self.headers.get_all('Origin', [])) > 1: return False
        if origin is not None and origin != self.server.origin: return False
        if session:
            try:
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                value = cookie.get(self.server.cookie_name)
                if not value or not hmac.compare_digest(value.value, self.server.session): return False
            except (CookieError, TypeError): return False
            # Cookies are host-scoped across ports. Require the browser-origin
            # capability too, so a cookie exposed to another local port alone
            # cannot read this dashboard or obtain its CSRF capability.
            try:
                if not hmac.compare_digest(self.headers.get('X-Routing-CSRF', ''), self.server.csrf): return False
            except TypeError: return False
        if mutate:
            try:
                if origin != self.server.origin or not hmac.compare_digest(self.headers.get('X-Routing-CSRF', ''), self.server.csrf): return False
            except TypeError: return False
        return True

    def do_GET(self):
        public = self.path in ('/', '/app.js', '/style.css', '/router-mark.svg')
        if not self.allowed(session=not public, public=public): return self.reply(403, {'error': 'forbidden_origin_or_session'})
        try:
            if public:
                filename, kind = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                                  '/style.css': ('style.css', 'text/css; charset=utf-8'),
                                  '/router-mark.svg': ('router-mark.svg','image/svg+xml')}[self.path]
                return self.reply(200, (ASSETS / filename).read_bytes(), kind)
            if self.path == '/api/session': return self.reply(200, {'csrf': self.server.csrf})
            if urlsplit(self.path).path == '/api/state':
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if set(query) - {'session'} or any(len(v) != 1 for v in query.values()): raise ValueError()
                session = state.identifier(query['session'][0]) if 'session' in query else None
                snapshot = self.server.store.snapshot(session)
                with self.server.otel_lock:
                    snapshot['otel_receiver'] = {**self.server.store.otel_status, **self.server.store.runtime_identity()}
                    snapshot['otel_receiver']['producer_metadata'] = {
                        kind: dict(counts) for kind, counts in self.server.store.otel_status['producer_metadata'].items()}
                return self.reply(200, snapshot)
            return self.reply(404, {'error': 'not_found'})
        except (ValueError, TypeError, RecursionError, OSError, state.sqlite3.Error):
            return self.reply(503, {'error': 'configuration_or_ledger_unavailable'})

    def do_POST(self):
        if self.path in ('/v1/logs', '/api/hooks'): return self.otel_logs()
        if self.path == '/api/lifecycle': return self.lifecycle()
        bootstrap = self.path == '/api/auth'
        if bootstrap:
            if not self.allowed(session=False) or self.headers.get('Origin') != self.server.origin:
                return self.reply(403, {'error': 'forbidden_origin_or_session'})
        elif not self.allowed(mutate=True): return self.reply(403, {'error': 'forbidden_origin_or_session'})
        if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self.reply(415, {'error': 'json_required'})
        try:
            if len(self.headers.get_all('Content-Length', [])) != 1: raise ValueError()
            length = int(self.headers['Content-Length'])
            if not 1 <= length <= core.MAX_PACKET_BYTES: return self.reply(413, {'error': 'body_too_large'})
            raw = core.read_json(self.rfile.read(length).decode('utf-8'))
            if bootstrap:
                if raw != {} or not self.server.authorize(self.headers.get('X-Routing-Bootstrap', '')):
                    return self.reply(403, {'error': 'local_browser_authorization_required'})
                return self.reply(200, {'authorized': True, 'csrf': self.server.csrf}, cookie=True)
            if self.path == '/api/config':
                core.object_fields(raw, {'config', 'version'}, {'config', 'version'})
                _, version = self.server.store.save(raw['config'], raw['version'])
                return self.reply(200, {'saved': True, 'config_version': version})
            if self.path == '/api/models/refresh':
                if raw != {}: raise ValueError()
                try:
                    result = catalog.refresh(self.server.store.root,
                        Path(os.environ.get('CLAUDE_CONFIG_DIR',Path.home()/'.claude')))
                    self.server.store.catalog_error = None
                    return self.reply(200, {'model_catalog':result})
                except catalog.CatalogError as error:
                    self.server.store.catalog_error = str(error)
                    return self.reply(503, {'error':'catalog_refresh_failed', 'reason':str(error)})
            if self.path == '/api/catalog/import':
                try:
                    result = catalog.import_picker(self.server.store.root, raw)
                    self.server.store.catalog_error = None
                    return self.reply(200, {'model_catalog': result})
                except catalog.CatalogError:
                    # Never echo pasted metadata, which may include private fields.
                    return self.reply(400, {'error': 'invalid_picker_confirmation'})
            if self.path == '/api/ledger/clear':
                if raw != {'confirm': True}: raise ValueError()
                state.Ledger(self.server.store.root).clear()
                return self.reply(200, {'cleared': True})
            return self.reply(404, {'error': 'not_found'})
        except FileExistsError:
            return self.reply(409, {'error': 'configuration_changed_reload_before_saving'})
        except ConfigValidationError as error:
            return self.reply(400, error.payload())
        except (ValueError, TypeError, UnicodeError, runtime.core.ProviderFailure, RecursionError):
            return self.reply(400, {'error': 'invalid_or_sensitive_configuration'})
        except (OSError, state.sqlite3.Error):
            return self.reply(503, {'error': 'storage_unavailable'})

    def lifecycle(self):
        secret = self.server.control_capability
        if secret is None: return self.reply(404, {'error': 'not_found'})
        if (self.headers.get_all('Host', []) != [urlsplit(self.server.origin).netloc]
                or len(self.headers.get_all('X-Routing-Control', [])) != 1
                or not hmac.compare_digest(self.headers.get('X-Routing-Control', '').encode('utf-8'), secret.encode('ascii'))
                or any(name.lower().startswith('sec-fetch-') or name.lower() == 'origin' for name in self.headers)):
            return self.reply(403, {'error': 'forbidden_controller'})
        if (self.headers.get('Content-Type') != 'application/json' or self.headers.get('Content-Encoding')
                or self.headers.get('Transfer-Encoding')):
            return self.reply(415, {'error': 'uncompressed_json_required'})
        try:
            if len(self.headers.get_all('Content-Length', [])) != 1: raise ValueError()
            length = int(self.headers['Content-Length'])
            if not 1 <= length <= 512: return self.reply(413, {'error': 'body_too_large'})
            data = self.rfile.read(length)
            if len(data) != length: raise ValueError()
            raw = core.read_json(data.decode('utf-8'))
            if not isinstance(raw, dict) or set(raw) != {'action'} or raw['action'] not in ('status', 'open', 'stop'):
                raise ValueError()
            result = {'instance_id': self.server.instance_id, 'revision': self.server.revision,
                      'url': self.server.origin, 'otel_receiver_enabled': self.server.otel_capability is not None}
            if raw['action'] == 'open': result['launch_file'] = str(self.server.renew_launch_file())
            self.reply(200, result)
            if raw['action'] == 'stop': threading.Thread(target=self.server.shutdown, daemon=True).start()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            return self.reply(400, {'error': 'invalid_lifecycle_request'})

    def otel_logs(self):
        secret = self.server.otel_capability
        if secret is None:
            self.server.record_otel(requests=1)
            return self.reply(404, {'error': 'not_found'})
        # Dedicated native exporter capability; browser cookie/CSRF never grants
        # ingest permission. Browser Origin/Fetch headers are always rejected.
        if (self.headers.get_all('Host', []) != [urlsplit(self.server.origin).netloc]
                or len(self.headers.get_all(otel.HEADER, [])) != 1
                or not hmac.compare_digest(self.headers.get(otel.HEADER, '').encode('utf-8'), secret.encode('ascii'))
                or self.headers.get('Origin') is not None or self.headers.get('Sec-Fetch-Site') is not None):
            self.server.record_otel(requests=1, unauthorized_requests=1)
            return self.reply(403, {'error': 'forbidden_exporter'})
        if (self.headers.get('Content-Type') != 'application/json' or self.headers.get('Content-Encoding')
                or self.headers.get('Transfer-Encoding')):
            self.server.record_otel(requests=1, invalid_batches=1)
            return self.reply(415, {'error': 'uncompressed_json_required'})
        try:
            if len(self.headers.get_all('Content-Length', [])) != 1: raise ValueError()
            length = int(self.headers['Content-Length'])
        except (ValueError, TypeError):
            self.server.record_otel(requests=1, invalid_batches=1)
            return self.reply(400, {'error': 'invalid_hook_payload'})
        if not 1 <= length <= otel.MAX_BYTES:
            self.server.record_otel(requests=1, invalid_batches=1)
            return self.reply(413, {'error': 'body_too_large'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length: raise ValueError()
        except (OSError, ValueError):
            self.server.record_otel(requests=1, invalid_batches=1)
            return self.reply(400, {'error': 'invalid_hook_payload'})
        try:
            config, _ = self.server.store.config()
            settings = state.LedgerSettings.parse(config['observability'])
            if settings.enabled:
                diagnostics, producer = {}, {}
                counts = otel.ingest(raw, state.Ledger(self.server.store.root, settings), diagnostics, producer)
                self.server.record_producer(producer)
                self.server.record_otel(received=True, observed=counts['accepted'] > 0, requests=1, validated_batches=1,
                    **{field + '_records': amount for field, amount in counts.items()},
                    **{reason + '_records': amount for reason, amount in diagnostics.items()})
            else:
                otel.decode(raw)
                self.server.record_otel(received=True, requests=1, validated_batches=1, recording_disabled_batches=1)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            self.server.record_otel(requests=1, invalid_batches=1)
            return self.reply(400, {'error': 'invalid_hook_payload'})
        except (OSError, state.sqlite3.Error):
            self.server.record_otel(requests=1, storage_errors=1)
            return self.reply(503, {'error': 'storage_unavailable'})
        # A disconnected exporter must not turn successful ingestion into a
        # second request/storage failure. Never include transport writes in the
        # storage exception boundary. Empty hook response; no raw record echo.
        return self.reply(200, {})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=state.state_root())
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open-browser', action='store_true', help='Open the private one-use launch file in your browser')
    parser.add_argument('--otel-token-file', type=Path, help='Opt-in private local exporter capability file; never a provider key')
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535: raise ValueError()
        token = args.otel_token_file.read_text(encoding='ascii').strip() if args.otel_token_file else None
        server = Server(args.port, Store(args.state_dir), token)
        launch = server.create_launch_file()
        print('Routing dashboard: ' + server.origin + ' (local browser authorization required)', flush=True)
        print('Open this private one-use launch file within 5 minutes: ' + str(launch), flush=True)
        if args.open_browser:
            try:
                if not webbrowser.open(launch.as_uri()): print('Open the private launch file manually; no browser was available.', flush=True)
            except (OSError, webbrowser.Error): print('Open the private launch file manually; automatic browser opening was unavailable.', flush=True)
        try: server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt: pass
        finally: server.server_close()
        return 0
    except (OSError, ValueError):
        print('Dashboard unavailable: check the state directory and loopback port. No settings were changed.', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
