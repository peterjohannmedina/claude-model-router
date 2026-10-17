"""Opt-in stdin collector for one Claude lifecycle hook; never launches Claude."""
import argparse
import json
from pathlib import Path
import sys

import routing_otel as hooks
import routing_state as state


def collect(stream, ledger, producer_version=None, event_id=None):
    raw = stream.read(hooks.MAX_BYTES + 1)
    if len(raw) > hooks.MAX_BYTES:
        raise ValueError('body_limit')
    records = hooks.decode(raw)
    if len(records) != 1:
        raise ValueError('one_hook_per_invocation')
    event = hooks.metadata(records[0], producer_version=producer_version, event_id=event_id)
    if event is None:
        return {'accepted': 0, 'ignored': 1, 'rejected': 0}
    ledger.put(event)
    return {'accepted': 1, 'ignored': 0, 'rejected': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=state.state_root())
    parser.add_argument('--producer-version', help='Optional operator annotation, not a hook field or provider attestation')
    parser.add_argument('--event-id', help='Optional stable UUID for replay deduplication')
    parser.add_argument('--diagnostics', action='store_true', help='Print only safe counters to stderr')
    args = parser.parse_args(argv)
    try:
        import routing_decision as core
        import routing_runtime as runtime
        config_path = args.state_dir / 'config.json'
        config = runtime.Config.parse(core.load_file(config_path)) if config_path.is_file() else runtime.Config()
        result = collect(sys.stdin.buffer, state.Ledger(args.state_dir, config.observability),
                         args.producer_version, args.event_id)
        # stdout from some hooks becomes model context. Emit nothing by default.
        if args.diagnostics:
            print(json.dumps(result), file=sys.stderr)
    except (ValueError, TypeError, OSError, state.sqlite3.Error, RecursionError):
        if args.diagnostics:
            print('{"error":"hook_collection_failed"}', file=sys.stderr)
    # This observer never blocks Claude or emits a hook control decision.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
