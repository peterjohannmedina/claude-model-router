"""Local, user-confirmed Claude Code picker catalog; never probes an account.

The API model catalog is not evidence of Claude subscription eligibility.
An import is an operator observation, not a provider entitlement attestation.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading

MAX_BYTES = 1024 * 1024
MAX_MODELS = 128
TTL_SECONDS = 900
EFFORTS = {'default', 'low', 'medium', 'high', 'xhigh', 'max'}
SOURCE = 'claude_model_picker_confirmation'
PROVIDERS = {'claude_ai', 'anthropic_api', 'bedrock', 'vertex', 'foundry', 'gateway', 'unknown'}
ERRORS = {'invalid_catalog', 'native_picker_confirmation_required', 'catalog_refresh_busy', 'catalog_unavailable'}
_slot = threading.Lock()


class CatalogError(ValueError):
    pass


def clock():
    return datetime.now(timezone.utc).isoformat()


def model_id(value):
    return (isinstance(value, str)
            and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}', value))
            and not re.search(r'(?i)(sk-|token|secret|password)', value))


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', value) else None


def read_json(path):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > MAX_BYTES:
        raise CatalogError('invalid_catalog')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def normalize_models(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_MODELS:
        raise CatalogError('invalid_catalog')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {'id', 'efforts', 'default_effort', 'input_modalities', 'policy_supported'}:
            raise CatalogError('invalid_catalog')
        # Never promote an explicitly ineligible/unverified row to eligible.
        # The field is optional on a new observation; normalized rows carry true.
        if 'policy_supported' in item and item['policy_supported'] is not True:
            raise CatalogError('invalid_catalog')
        name, efforts = item.get('id'), item.get('efforts')
        if not model_id(name) or name in seen or not isinstance(efforts, list) or not 1 <= len(efforts) <= len(EFFORTS):
            raise CatalogError('invalid_catalog')
        if any(not isinstance(e, str) or e not in EFFORTS for e in efforts) or len(set(efforts)) != len(efforts):
            raise CatalogError('invalid_catalog')
        default = item.get('default_effort')
        if default is not None and default not in efforts:
            raise CatalogError('invalid_catalog')
        modalities = item.get('input_modalities', ['text'])
        if not isinstance(modalities, list) or not modalities or any(m not in ('text', 'image', 'audio') for m in modalities):
            raise CatalogError('invalid_catalog')
        seen.add(name)
        result.append({'id': name, 'efforts': list(efforts), 'default_effort': default,
                       'input_modalities': sorted(set(modalities)), 'policy_supported': True})
    return result


def _atomic(root, value):
    root = Path(root)
    if root.is_symlink():
        raise CatalogError('invalid_catalog')
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'model-catalog.json'
    if target.is_symlink():
        raise CatalogError('invalid_catalog')
    fd, temporary = tempfile.mkstemp(prefix='.catalog-', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(value, allow_nan=False, indent=2) + '\n')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_picker(root, payload):
    """Persist only explicit current-picker confirmation, with a receiver timestamp."""
    if not _slot.acquire(blocking=False):
        raise CatalogError('catalog_refresh_busy')
    try:
        if (not isinstance(payload, dict)
                or set(payload) - {'confirmed', 'source', 'models', 'provider', 'producer_version'}
                or payload.get('confirmed') is not True
                or payload.get('source') != 'claude_code_model_picker'):
            raise CatalogError('native_picker_confirmation_required')
        provider = payload.get('provider', 'unknown')
        producer = payload.get('producer_version')
        if provider not in PROVIDERS or producer is not None and version(producer) is None:
            raise CatalogError('invalid_catalog')
        value = {'schema_version': 1, 'source': SOURCE, 'queried_at': clock(),
                 'auth_type': 'operator_observation', 'plan_type': 'unknown',
                 'provider': provider, 'producer_version': producer, 'confirmed': True,
                 'scope_verified': False, 'models': normalize_models(payload.get('models')), 'last_error': None}
        _atomic(root, value)
        return public(value)
    finally:
        _slot.release()


def load(root):
    try:
        value = read_json(Path(root) / 'model-catalog.json')
        if (not isinstance(value, dict) or value.get('schema_version') != 1
                or value.get('source') != SOURCE or value.get('confirmed') is not True
                or value.get('provider') not in PROVIDERS
                or value.get('producer_version') is not None and version(value['producer_version']) is None):
            return None
        parsed = datetime.fromisoformat(value['queried_at'])
        if parsed.tzinfo is None:
            return None
        # Rebuild from an allowlist. Never expose arbitrary fields from disk.
        return {'schema_version': 1, 'source': SOURCE, 'queried_at': value['queried_at'],
                'auth_type': 'operator_observation', 'plan_type': 'unknown',
                'provider': value['provider'], 'producer_version': value.get('producer_version'),
                'confirmed': True, 'scope_verified': False, 'models': normalize_models(value['models']),
                'last_error': value.get('last_error') if value.get('last_error') in ERRORS else None}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def public(value, home=None):
    baseline = [{'id': name, 'efforts': [], 'default_effort': None, 'input_modalities': ['text'],
                 'policy_supported': False} for name in ('haiku', 'sonnet', 'opus')]
    result = {'status': 'unverified', 'source': 'documented_alias_baseline', 'queried_at': None,
              'models': baseline, 'last_error': None, 'entitlement': 'unverified',
              'scope_verified': False, 'remote_freshness': 'unverified', 'freshness_seconds': TTL_SECONDS}
    if not value:
        return result
    try:
        if value.get('source') != SOURCE or value.get('confirmed') is not True:
            return result
        models = normalize_models(value['models'])
        parsed = datetime.fromisoformat(value['queried_at'])
        if parsed.tzinfo is None:
            return result
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except (ValueError, TypeError, KeyError, AttributeError):
        return result
    result.update({k: value.get(k) for k in ('source', 'queried_at', 'auth_type', 'plan_type', 'provider', 'producer_version')})
    result.update(models=models, status='queried' if 0 <= age <= TTL_SECONDS else 'stale',
                  entitlement='user_confirmed')
    if value.get('last_error') in ERRORS:
        result.update(status='error', last_error=value['last_error'])
    return result


def refresh(root, home=None, confirmation=None):
    """No automatic RPC exists here. A new observation must be explicitly imported."""
    if confirmation is None:
        raise CatalogError('native_picker_confirmation_required')
    return import_picker(root, confirmation)


def restrict(packet, allowed, value, home=None):
    """Intersect packet, optional selected IDs, and fresh user-confirmed capabilities."""
    import routing_decision as core
    packet = deepcopy(packet)
    models = {m['id']: m for m in public(value)['models']} if public(value)['status'] == 'queried' else {}
    required = packet.get('required_modalities', ['text'])
    if not isinstance(required, list) or not required or any(m not in ('text', 'image', 'audio') for m in required):
        raise core.RoutingError('invalid_required_modalities')
    incoming = packet.get('available_models')
    if not isinstance(incoming, dict):
        raise core.RoutingError('missing_catalog')
    packet['available_models'] = {name: [e for e in efforts if e in models[name]['efforts'] and e in EFFORTS]
        for name, efforts in incoming.items() if name in models and (allowed is None or name in allowed)
        and isinstance(efforts, list) and set(required) <= set(models[name]['input_modalities'])}
    packet['available_models'] = {name: efforts for name, efforts in packet['available_models'].items() if efforts}
    return packet
