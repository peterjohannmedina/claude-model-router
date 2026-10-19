"""Opt-in status-line observer; silent unless exact stdin forwarding is requested.

This does not launch or chain commands or configure Claude. It is not a transparent
replacement for an existing status display. A separately reviewed user wrapper
may pass --forward-stdin and pipe the original payload to its existing display.
That opt-in stream contains the original private metadata; it is never stored.
Forwarding requires bounded valid JSON object input, independently of whether
this adapter recognizes the metadata schema, so schema drift cannot blank an
existing display. Malformed, oversized, or non-object input is not forwarded.
"""
import argparse
import json
import os
from pathlib import Path
import sys

import routing_statusline as statusline
import routing_state as state


def read_snapshot(stream):
    raw = stream.read(statusline.MAX_BYTES + 1)
    event = statusline.metadata(statusline.decode(raw), collection_transport='stdin')
    return raw, event


def collect(stream, ledger):
    _, event = read_snapshot(stream)
    return statusline.record(event, ledger)


def discard_broken_stdout():
    """Keep interpreter shutdown from retrying buffered writes to a dead pipe."""
    try:
        descriptor = sys.stdout.fileno()
    except (AttributeError, OSError, TypeError, ValueError):
        # Embedded callers may supply a stream without an OS descriptor.
        return
    null_descriptor = None
    try:
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_descriptor, descriptor)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        # If the original descriptor was already closed, open() can reuse it.
        # In that case it must stay open for the interpreter's final flush.
        if null_descriptor is not None and null_descriptor != descriptor:
            try:
                os.close(null_descriptor)
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=state.state_root())
    parser.add_argument('--diagnostics', action='store_true', help='Safe counters/errors to stderr only')
    parser.add_argument('--forward-stdin', action='store_true',
                        help='Explicitly forward exact bounded JSON object bytes to an existing trusted display via stdout')
    args = parser.parse_args(argv)
    raw = None
    try:
        candidate = sys.stdin.buffer.read(statusline.MAX_BYTES + 1)
        payload = statusline.decode(candidate)
        raw = candidate  # Schema drift may reject recording, but must preserve an existing display.
        event = statusline.metadata(payload, collection_transport='stdin')
        import routing_decision as core
        import routing_runtime as runtime
        config_path = args.state_dir / 'config.json'
        config = runtime.Config.parse(core.load_file(config_path)) if config_path.is_file() else runtime.Config()
        result = statusline.record(event, state.Ledger(args.state_dir, config.observability))
        if args.diagnostics:
            print(json.dumps(result), file=sys.stderr)
    except (ValueError, TypeError, OSError, state.sqlite3.Error, RecursionError, OverflowError):
        if args.diagnostics:
            print('{"error":"statusline_collection_failed"}', file=sys.stderr)
    finally:
        if args.forward_stdin and raw is not None:
            try:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            except (AttributeError, OSError, ValueError):
                discard_broken_stdout()
    # Observation failures never issue a Claude control decision.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
