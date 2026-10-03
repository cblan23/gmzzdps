"""Exercise the actual passive backend under its existing development lease.

Reports aggregate counters only. Reads RC4/Zstd state using the backend's
read-only access; does not change game/client configuration or stop other apps.
"""
import argparse
import json
import queue
import os
import time
from collections import Counter
from pathlib import Path

from npcap_capture_process import CaptureProcessClient
from runtime_capability import create_development_capability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--source', choices=('npcap', 'windows_raw'), default='npcap')
    parser.add_argument('--no-fallback', action='store_true',
                        help='require the selected packet source; never load Npcap as fallback')
    parser.add_argument('--records-output', type=Path,
                        help='optional new private gameplay JSONL file; never overwritten')
    parser.add_argument('--unknown-output', type=Path, help='private diagnostic samples of unknown RPCs')
    args = parser.parse_args()
    if not 0 < args.seconds <= 1800:
        parser.error('--seconds must be between 0 and 1800')
    capability = create_development_capability(
        Path(__file__).with_name('runtime-profile.dev.json'), session_id='npcap-local-probe',
        client_id='0'*32, build_id='source-development', client_build='local-validation',
        lease_seconds=args.seconds + 60)
    client = CaptureProcessClient(runtime_capability=capability, allow_development=True,
                                  capture_source=args.source,
                                  allow_npcap_fallback=not args.no_fallback)
    methods, stages = Counter(), Counter()
    diagnostics = {}
    serialization_errors = 0
    output = args.records_output.open('x', encoding='utf-8') if args.records_output else None
    unknown_output = args.unknown_output.open('x', encoding='utf-8') if args.unknown_output else None
    if unknown_output is not None:
        os.environ['GMZZ_NPCAP_CAPTURE_UNKNOWN'] = '1'
        os.environ['GMZZ_NPCAP_CAPTURE_UNKNOWN_TIMELINE'] = '1'
    started = time.monotonic()
    try:
        client.start()
        while time.monotonic()-started < args.seconds:
            try:
                kind, payload = client.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind in ('state', 'capture_error'):
                stages[str(payload.get('stage', kind))] += 1
                if kind == 'capture_error':
                    print(json.dumps({'capture_error': payload}, ensure_ascii=False), flush=True)
            elif kind == 'batch':
                for record in payload.get('entity_metadata_records', []):
                    methods['read_only_entity_metadata'] += 1
                    if output is not None:
                        output.write(json.dumps(record, ensure_ascii=False)+'\n')
                for record in payload.get('records', []):
                    methods[str(record.get('method', 'unknown'))] += 1
                    try:
                        serialized = json.dumps(record, ensure_ascii=False)
                    except (ValueError, TypeError):
                        serialization_errors += 1
                    else:
                        if output is not None and 'chat' not in str(record.get('method', '')).casefold():
                            output.write(serialized + '\n')
                if output is not None:
                    output.flush()
                diagnostics = payload.get('native_diagnostic', {}).get('counters', {})
            elif kind == 'fatal':
                stages['fatal'] += 1
                print(json.dumps({'fatal': payload}, ensure_ascii=False), flush=True)
                break
            elif kind == 'protocol_unknown':
                if unknown_output is not None:
                    for record in payload:
                        unknown_output.write(json.dumps(record, ensure_ascii=True, default=str)+'\n')
                    unknown_output.flush()
            else:
                stages[kind] += 1
    finally:
        client.request_stop()
        deadline = time.monotonic() + 5
        while client.is_alive() and time.monotonic() < deadline:
            try:
                client.get(timeout=0.1)
            except queue.Empty:
                pass
        if client.is_alive():
            client.terminate()  # Only our own passive diagnostic child.
            stages['probe_child_forced_stop'] += 1
        client.join(2)
        client.close(wait_for_queue=False)
        if output is not None:
            output.close()
        if unknown_output is not None:
            unknown_output.close()
    print(json.dumps({'source': args.source, 'stages': dict(stages), 'method_counts': dict(methods),
                      'counters': diagnostics, 'json_serialization_errors': serialization_errors,
                      'note': 'Not independent parity evidence while legacy capture is running.'}))


if __name__ == '__main__':
    main()
