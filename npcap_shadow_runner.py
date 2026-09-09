#!/usr/bin/env python3
"""Run an isolated, passive Npcap capture with read-only RC4 anchors.

This program is deliberately independent from the DPS application. It never
imports combat/statistics modules, never transmits a packet, and never writes
to the game process. Its output is evidence for later packet-by-packet
comparison with the existing decoded-hook logs.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import struct
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional, Sequence

import npcap_shadow_capture as shadow_capture
import proc_inspect
import rc4_state_snapshot


ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
DEFAULT_STOP_EVENT = r"Local\GMZZDpsNpcapShadowStop"


class PcapStat(ctypes.Structure):
    _fields_ = [
        ("received", ctypes.c_uint),
        ("dropped", ctypes.c_uint),
        ("interface_dropped", ctypes.c_uint),
        ("captured", ctypes.c_uint),
    ]


@dataclass
class SequenceCoverage:
    seen: set[int] = field(default_factory=set)
    duplicates: int = 0
    payload_bytes: int = 0

    def add(self, sequence: int, payload_length: int) -> bool:
        sequence &= 0xFFFFFFFF
        if sequence in self.seen:
            self.duplicates += 1
            return True
        self.seen.add(sequence)
        self.payload_bytes += max(0, int(payload_length))
        return False

    def summary(self) -> dict:
        if not self.seen:
            return {
                "unique_pushes": 0,
                "duplicates": self.duplicates,
                "payload_bytes": self.payload_bytes,
                "first_sequence": None,
                "last_sequence": None,
                "missing_inside_span": 0,
                "complete_inside_span": True,
            }
        first = min(self.seen)
        last = max(self.seen)
        span = last - first + 1
        missing = max(0, span - len(self.seen))
        return {
            "unique_pushes": len(self.seen),
            "duplicates": self.duplicates,
            "payload_bytes": self.payload_bytes,
            "first_sequence": first,
            "last_sequence": last,
            "missing_inside_span": missing,
            "complete_inside_span": missing == 0,
        }


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def integer(value: str) -> int:
    return int(value, 0)


def write_jsonl(stream: IO[str], record: dict) -> None:
    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_json_atomic(path: Path, record: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def split_remote(value: str) -> tuple[str, int]:
    value = value.strip()
    if not value:
        return "", 0
    host, separator, raw_port = value.rpartition(":")
    if not separator or not host:
        raise ValueError("remote must use HOST:PORT format")
    port = int(raw_port)
    if not 0 < port <= 65535:
        raise ValueError("remote port is outside 1..65535")
    return host, port


def make_session_directory(root: Path, pid: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"shadow_{stamp}_pid{pid}"
    suffix = 1
    while candidate.exists():
        candidate = root / f"shadow_{stamp}_pid{pid}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def configure_stop_api():
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.SetEvent.restype = ctypes.c_bool
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


def request_stop(event_name: str) -> int:
    kernel32 = configure_stop_api()
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
    if not handle:
        print("NPCAP_SHADOW_NOT_RUNNING")
        return 1
    try:
        if not kernel32.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    print("NPCAP_SHADOW_STOP_REQUESTED")
    return 0


def create_stop_event(event_name: str):
    kernel32 = configure_stop_api()
    ctypes.set_last_error(0)
    handle = kernel32.CreateEventW(None, True, False, event_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise RuntimeError("another Npcap shadow runner is already active")
    return kernel32, handle


def stop_requested(kernel32, handle) -> bool:
    return kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def install_bpf(wpcap, handle, expression: str) -> None:
    program = shadow_capture.BpfProgram()
    if wpcap.pcap_compile(
        handle, ctypes.byref(program), expression.encode("ascii"), 1, 0xFFFFFFFF
    ) != 0:
        raise RuntimeError(shadow_capture._decode_native(wpcap.pcap_geterr(handle)))
    try:
        if wpcap.pcap_setfilter(handle, ctypes.byref(program)) != 0:
            raise RuntimeError(shadow_capture._decode_native(wpcap.pcap_geterr(handle)))
    finally:
        wpcap.pcap_freecode(ctypes.byref(program))


def configure_pcap_stats(wpcap) -> None:
    wpcap.pcap_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(PcapStat)]
    wpcap.pcap_stats.restype = ctypes.c_int


def read_pcap_stats(wpcap, handle) -> dict:
    stats = PcapStat()
    if wpcap.pcap_stats(handle, ctypes.byref(stats)) != 0:
        return {"available": False}
    return {
        "available": True,
        "received": int(stats.received),
        "dropped": int(stats.dropped),
        "interface_dropped": int(stats.interface_dropped),
        "captured": int(stats.captured),
    }


class SnapshotReader:
    def __init__(self, pid: int, cryptor: int):
        self.pid = int(pid)
        self.cryptor = int(cryptor)
        access = proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ
        self.process = proc_inspect.kernel32.OpenProcess(access, False, self.pid)
        if not self.process:
            raise proc_inspect.winerror(
                "OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ)"
            )

    def close(self) -> None:
        if self.process:
            proc_inspect.kernel32.CloseHandle(self.process)
            self.process = None

    def snapshot(self, remote: str, packet_index: int, flows: list[dict]) -> dict:
        raw = rc4_state_snapshot.read_exact(
            self.process,
            self.cryptor,
            rc4_state_snapshot.CRYPTOR_POINTERS.size,
            "cipher_cryptor",
        )
        owner, encrypt_ctx, decrypt_ctx = rc4_state_snapshot.CRYPTOR_POINTERS.unpack(raw)
        if not encrypt_ctx or not decrypt_ctx:
            raise RuntimeError("cipher_cryptor does not contain both EVP contexts")
        encrypt_flag = struct.unpack(
            "<I",
            rc4_state_snapshot.read_exact(
                self.process,
                encrypt_ctx + rc4_state_snapshot.EVP_ENCRYPT_OFFSET,
                4,
                "encrypt flag",
            ),
        )[0]
        decrypt_flag = struct.unpack(
            "<I",
            rc4_state_snapshot.read_exact(
                self.process,
                decrypt_ctx + rc4_state_snapshot.EVP_ENCRYPT_OFFSET,
                4,
                "decrypt flag",
            ),
        )[0]
        if encrypt_flag != 1 or decrypt_flag != 0:
            raise RuntimeError(
                f"unexpected EVP directions: encrypt={encrypt_flag}, decrypt={decrypt_flag}"
            )
        encrypt_state = rc4_state_snapshot.read_pointer(
            self.process,
            encrypt_ctx + rc4_state_snapshot.EVP_CIPHER_DATA_OFFSET,
            "encrypt cipher_data",
        )
        decrypt_state = rc4_state_snapshot.read_pointer(
            self.process,
            decrypt_ctx + rc4_state_snapshot.EVP_CIPHER_DATA_OFFSET,
            "decrypt cipher_data",
        )
        return {
            "record_type": "rc4_state_snapshot",
            "schema": 2,
            "mode": "read_only",
            "pid": self.pid,
            "remote": remote,
            "capture_packet_index_before_read": int(packet_index),
            "observed_push_flows": flows,
            "cryptor": self.cryptor,
            "cryptor_owner": owner,
            "encrypt_evp_ctx": encrypt_ctx,
            "decrypt_evp_ctx": decrypt_ctx,
            "encrypt": rc4_state_snapshot.snapshot_state(self.process, encrypt_state),
            "decrypt": rc4_state_snapshot.snapshot_state(self.process, decrypt_state),
        }


def snapshot_candidates(roots: Sequence[Path], pid: int) -> list[int]:
    matches: list[tuple[float, int]] = []
    visited: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("**/*rc4*.json"):
            try:
                resolved = path.resolve()
                if resolved in visited or path.stat().st_size > 256 * 1024:
                    continue
                visited.add(resolved)
                record = json.loads(path.read_text(encoding="utf-8"))
                if int(record.get("pid", -1)) != int(pid):
                    continue
                cryptor = int(record.get("cryptor", 0))
                if cryptor:
                    matches.append((path.stat().st_mtime, cryptor))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    result: list[int] = []
    for _modified, cryptor in sorted(matches, reverse=True):
        if cryptor not in result:
            result.append(cryptor)
    return result


def select_snapshot_reader(pid: int, explicit: int, roots: Sequence[Path]):
    candidates = [explicit] if explicit else snapshot_candidates(roots, pid)
    errors: list[str] = []
    for candidate in candidates:
        reader: Optional[SnapshotReader] = None
        try:
            reader = SnapshotReader(pid, candidate)
            reader.snapshot("", 0, [])
            return reader, candidate, errors
        except Exception as exc:
            errors.append(f"0x{candidate:x}: {exc}")
            if reader is not None:
                reader.close()
    return None, 0, errors


def endpoint_parts(udp: shadow_capture.ParsedUdp, local_ip: str) -> tuple[str, int, str, int, str]:
    if udp.src_ip == local_ip:
        return udp.src_ip, udp.src_port, udp.dst_ip, udp.dst_port, "outbound"
    if udp.dst_ip == local_ip:
        return udp.dst_ip, udp.dst_port, udp.src_ip, udp.src_port, "inbound"
    return local_ip, 0, udp.dst_ip, udp.dst_port, "unknown"


def flow_id(
    direction: str, local_port: int, remote_ip: str, remote_port: int, conv: int
) -> str:
    return f"{direction}|{local_port}|{remote_ip}:{remote_port}|{conv}"


def observed_push_flows(coverage: dict[str, SequenceCoverage]) -> list[dict]:
    result: list[dict] = []
    for key, value in sorted(coverage.items()):
        direction, local_port, remote, conv = key.split("|", 3)
        summary = value.summary()
        result.append(
            {
                "direction": direction,
                "local_port": int(local_port),
                "remote": remote,
                "conv": int(conv),
                "last_sequence": summary["last_sequence"],
            }
        )
    return result


def preferred_remote(remote_counts: Counter[str], configured: str) -> str:
    if configured:
        return configured
    inbound = [(count, key[3:]) for key, count in remote_counts.items() if key.startswith("in|")]
    if inbound:
        return max(inbound)[1]
    any_direction = [(count, key.split("|", 1)[-1]) for key, count in remote_counts.items()]
    return max(any_direction)[1] if any_direction else ""


def make_manifest(
    *,
    status: str,
    session_id: str,
    started: float,
    pid: int,
    executable: str,
    adapter: shadow_capture.Adapter,
    local_ip: str,
    ports: Sequence[int],
    raw_path: Path,
    events_path: Path,
    snapshots_path: Path,
    cryptor: int,
    counters: Counter[str],
    coverage: dict[str, SequenceCoverage],
    remote_counts: Counter[str],
    pcap_stats: dict,
    snapshot_errors: list[str],
    finished: Optional[float] = None,
) -> dict:
    now = finished or time.time()
    return {
        "record_type": "npcap_shadow_manifest",
        "schema": 1,
        "status": status,
        "session_id": session_id,
        "mode": "passive_read_only",
        "safety": {
            "packet_transmit_functions_loaded": False,
            "packets_sent_by_runner": 0,
            "game_process_access": "query_and_read_only",
            "imports_dps_application": False,
            "server_requests_added": 0,
        },
        "started_epoch": started,
        "started_utc": utc_iso(started),
        "updated_epoch": now,
        "updated_utc": utc_iso(now),
        "elapsed_seconds": round(now - started, 3),
        "pid": pid,
        "process": executable,
        "adapter": asdict(adapter),
        "local_ip": local_ip,
        "game_udp_ports": sorted(int(port) for port in ports),
        "cryptor": cryptor,
        "files": {
            "raw_packets": str(raw_path.resolve()),
            "events": str(events_path.resolve()),
            "rc4_snapshots": str(snapshots_path.resolve()),
        },
        "counts": dict(sorted(counters.items())),
        "pcap": pcap_stats,
        "sequence_coverage": {
            key: value.summary() for key, value in sorted(coverage.items())
        },
        "observed_remotes": dict(sorted(remote_counts.items())),
        "recent_snapshot_errors": snapshot_errors[-10:],
    }


def run(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise RuntimeError("Npcap shadow runner currently supports Windows only")
    split_remote(args.remote)
    stop_api, stop_handle = create_stop_event(args.stop_event)
    wpcap = None
    pcap_handle = None
    snapshot_reader: Optional[SnapshotReader] = None
    raw_stream: Optional[IO[str]] = None
    event_stream: Optional[IO[str]] = None
    snapshot_stream: Optional[IO[str]] = None
    manifest_path: Optional[Path] = None
    raw_path: Optional[Path] = None
    events_path: Optional[Path] = None
    snapshots_path: Optional[Path] = None
    final_manifest: Optional[dict] = None
    exit_code = 0
    try:
        wpcap = shadow_capture.load_wpcap()
        configure_pcap_stats(wpcap)
        adapters = shadow_capture.list_adapters(wpcap)
        discovery_args = argparse.Namespace(pid=args.pid, process_name=args.process_name)
        pid, executable, endpoints = shadow_capture.discover_game(discovery_args)
        adapter, local_ip = shadow_capture.select_adapter(
            adapters, args.adapter, args.local_ip
        )
        ports = sorted({row.local_port for row in endpoints})
        bpf = shadow_capture.make_bpf(local_ip, ports)
        errbuf = ctypes.create_string_buffer(shadow_capture.ERRBUF_SIZE)
        pcap_handle = wpcap.pcap_open_live(
            adapter.name.encode(), 65535, 0, 100, errbuf
        )
        if not pcap_handle:
            raise RuntimeError(
                f"pcap_open_live failed: {shadow_capture._decode_native(errbuf.value)}"
            )
        install_bpf(wpcap, pcap_handle, bpf)
        datalink = int(wpcap.pcap_datalink(pcap_handle))

        session_dir = make_session_directory(Path(args.output_root), pid)
        session_id = session_dir.name
        raw_path = session_dir / "raw_packets.jsonl"
        events_path = session_dir / "events.jsonl"
        snapshots_path = session_dir / "rc4_snapshots.jsonl"
        manifest_path = session_dir / "manifest.json"
        latest_snapshot_path = session_dir / "rc4_anchor_latest.json"
        raw_stream = raw_path.open("w", encoding="utf-8", newline="\n")
        event_stream = events_path.open("w", encoding="utf-8", newline="\n")
        snapshot_stream = snapshots_path.open("w", encoding="utf-8", newline="\n")

        started = time.time()
        counters: Counter[str] = Counter()
        remote_counts: Counter[str] = Counter()
        tracker = shadow_capture.DuplicateTracker()
        coverage: dict[str, SequenceCoverage] = {}
        snapshot_errors: list[str] = []
        known_endpoints: set[str] = set()
        known_push_conversations: set[str] = set()
        packet_index = 0
        snapshot_reader, cryptor, discovery_errors = select_snapshot_reader(
            pid,
            args.cryptor,
            [session_dir, Path(args.output_root), Path(".codex-tmp/npcap-shadow")],
        )
        snapshot_errors.extend(discovery_errors)
        start_record = {
            "record_type": "capture_start",
            "schema": 2,
            "session_id": session_id,
            "started_epoch": started,
            "started_utc": utc_iso(started),
            "pid": pid,
            "process": executable,
            "adapter": asdict(adapter),
            "local_ip": local_ip,
            "game_udp_ports": ports,
            "bpf": bpf,
            "datalink": datalink,
            "cryptor": cryptor,
            "mode": "passive_read_only",
            "server_requests_added": 0,
        }
        write_jsonl(raw_stream, start_record)
        write_jsonl(event_stream, start_record)
        if not snapshot_reader:
            write_jsonl(
                event_stream,
                {
                    "record_type": "snapshot_unavailable",
                    "timestamp_epoch": time.time(),
                    "errors": discovery_errors,
                    "raw_capture_continues": True,
                },
            )
        raw_stream.flush()
        event_stream.flush()

        print("NPCAP_SHADOW_READY", flush=True)
        print(f"session={session_dir.resolve()}", flush=True)
        print(f"pid={pid} local_ip={local_ip} ports={','.join(map(str, ports))}", flush=True)
        print(
            f"rc4={'0x%x' % cryptor if snapshot_reader else 'unavailable; raw-only'}",
            flush=True,
        )

        next_snapshot = started
        next_endpoint_refresh = started + args.endpoint_refresh_interval
        next_manifest = started
        next_heartbeat = started + args.heartbeat_interval
        deadline = started + args.duration if args.duration > 0 else None
        process_missing_since: Optional[float] = None

        while not stop_requested(stop_api, stop_handle):
            now = time.time()
            if deadline is not None and now >= deadline:
                break

            if snapshot_reader and now >= next_snapshot:
                try:
                    remote = preferred_remote(remote_counts, args.remote)
                    snapshot = snapshot_reader.snapshot(
                        remote, packet_index, observed_push_flows(coverage)
                    )
                    write_jsonl(snapshot_stream, snapshot)
                    snapshot_stream.flush()
                    write_json_atomic(latest_snapshot_path, snapshot)
                    counters["rc4_snapshots"] += 1
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    snapshot_errors.append(message)
                    counters["rc4_snapshot_errors"] += 1
                    write_jsonl(
                        event_stream,
                        {
                            "record_type": "rc4_snapshot_error",
                            "timestamp_epoch": now,
                            "timestamp_utc": utc_iso(now),
                            "error": message,
                        },
                    )
                next_snapshot = now + args.snapshot_interval

            if now >= next_endpoint_refresh:
                current_endpoints = [
                    row for row in shadow_capture.list_udp_endpoints() if row.pid == pid
                ]
                current_ports = sorted({row.local_port for row in current_endpoints})
                if current_ports:
                    process_missing_since = None
                    if current_ports != ports:
                        old_ports = ports
                        ports = current_ports
                        bpf = shadow_capture.make_bpf(local_ip, ports)
                        install_bpf(wpcap, pcap_handle, bpf)
                        counters["capture_filter_updates"] += 1
                        write_jsonl(
                            event_stream,
                            {
                                "record_type": "game_udp_ports_changed",
                                "timestamp_epoch": now,
                                "timestamp_utc": utc_iso(now),
                                "old_ports": old_ports,
                                "new_ports": ports,
                                "bpf": bpf,
                            },
                        )
                else:
                    process_missing_since = process_missing_since or now
                    if now - process_missing_since >= args.process_exit_grace:
                        counters["game_process_or_udp_exit"] += 1
                        break
                next_endpoint_refresh = now + args.endpoint_refresh_interval

            if now >= next_manifest:
                manifest = make_manifest(
                    status="running",
                    session_id=session_id,
                    started=started,
                    pid=pid,
                    executable=executable,
                    adapter=adapter,
                    local_ip=local_ip,
                    ports=ports,
                    raw_path=raw_path,
                    events_path=events_path,
                    snapshots_path=snapshots_path,
                    cryptor=cryptor,
                    counters=counters,
                    coverage=coverage,
                    remote_counts=remote_counts,
                    pcap_stats=read_pcap_stats(wpcap, pcap_handle),
                    snapshot_errors=snapshot_errors,
                )
                write_json_atomic(manifest_path, manifest)
                next_manifest = now + args.manifest_interval

            header_ptr = ctypes.POINTER(shadow_capture.PcapPacketHeader)()
            data_ptr = ctypes.POINTER(ctypes.c_ubyte)()
            status = wpcap.pcap_next_ex(
                pcap_handle, ctypes.byref(header_ptr), ctypes.byref(data_ptr)
            )
            if status == 0:
                if now >= next_heartbeat:
                    write_jsonl(
                        event_stream,
                        {
                            "record_type": "heartbeat",
                            "timestamp_epoch": now,
                            "timestamp_utc": utc_iso(now),
                            "packet_index": packet_index,
                            "counts": dict(counters),
                        },
                    )
                    event_stream.flush()
                    next_heartbeat = now + args.heartbeat_interval
                continue
            if status == -2:
                break
            if status < 0:
                raise RuntimeError(
                    shadow_capture._decode_native(wpcap.pcap_geterr(pcap_handle))
                )

            header = header_ptr.contents
            raw = ctypes.string_at(data_ptr, header.caplen)
            udp = shadow_capture.parse_udp_packet(raw, datalink)
            if not udp:
                counters["unparsed_packets"] += 1
                continue
            local_host, local_port, remote_ip, remote_port, direction = endpoint_parts(
                udp, local_ip
            )
            if local_port not in ports:
                counters["filtered_stale_port_packets"] += 1
                continue

            packet_index += 1
            timestamp = float(header.ts.tv_sec) + float(header.ts.tv_usec) / 1_000_000
            remote = f"{remote_ip}:{remote_port}"
            remote_counts[f"{'in' if direction == 'inbound' else 'out'}|{remote}"] += 1
            endpoint_key = f"{local_port}|{remote}"
            if endpoint_key not in known_endpoints:
                known_endpoints.add(endpoint_key)
                write_jsonl(
                    event_stream,
                    {
                        "record_type": "remote_endpoint_observed",
                        "timestamp_epoch": timestamp,
                        "timestamp_utc": utc_iso(timestamp),
                        "local_port": local_port,
                        "remote": remote,
                        "direction": direction,
                    },
                )

            segments = shadow_capture.parse_kcp_segments(udp.payload)
            for segment in segments:
                duplicate = tracker.mark(direction, segment)
                segment["duplicate"] = duplicate
                command = str(segment["command"])
                counters[f"kcp_{command.lower()}"] += 1
                counters["kcp_duplicates"] += int(duplicate)
                if command == "PUSH":
                    key = flow_id(
                        direction,
                        local_port,
                        remote_ip,
                        remote_port,
                        int(segment["conv"]),
                    )
                    sequence_coverage = coverage.setdefault(key, SequenceCoverage())
                    sequence_coverage.add(
                        int(segment["sequence"]), int(segment["payload_length"])
                    )
                    if key not in known_push_conversations:
                        known_push_conversations.add(key)
                        write_jsonl(
                            event_stream,
                            {
                                "record_type": "kcp_push_conversation_observed",
                                "timestamp_epoch": timestamp,
                                "timestamp_utc": utc_iso(timestamp),
                                "flow": key,
                                "sequence": int(segment["sequence"]),
                            },
                        )

            counters["udp_packets"] += 1
            counters[f"direction_{direction}"] += 1
            counters["udp_payload_bytes"] += len(udp.payload)
            counters["kcp_datagrams" if segments else "non_kcp_datagrams"] += 1
            write_jsonl(
                raw_stream,
                {
                    "record_type": "udp_packet",
                    "session_id": session_id,
                    "packet_index": packet_index,
                    "timestamp_epoch": timestamp,
                    "timestamp_utc": utc_iso(timestamp),
                    "direction": direction,
                    "local_ip": local_host,
                    "local_port": local_port,
                    "remote_ip": remote_ip,
                    "remote_port": remote_port,
                    "src_ip": udp.src_ip,
                    "src_port": udp.src_port,
                    "dst_ip": udp.dst_ip,
                    "dst_port": udp.dst_port,
                    "captured_length": int(header.caplen),
                    "wire_length": int(header.length),
                    "udp_payload_length": len(udp.payload),
                    "udp_payload_sha256": hashlib.sha256(udp.payload).hexdigest(),
                    "udp_payload_base64": base64.b64encode(udp.payload).decode("ascii"),
                    "kcp_segments": segments,
                },
            )
            if packet_index % args.flush_packets == 0:
                raw_stream.flush()
                event_stream.flush()

        finished = time.time()
        counters["clean_stop"] += 1
        pcap_final = read_pcap_stats(wpcap, pcap_handle)
        end_record = {
            "record_type": "capture_end",
            "session_id": session_id,
            "finished_epoch": finished,
            "finished_utc": utc_iso(finished),
            "elapsed_seconds": round(finished - started, 3),
            "counts": dict(sorted(counters.items())),
            "pcap": pcap_final,
            "sequence_coverage": {
                key: value.summary() for key, value in sorted(coverage.items())
            },
        }
        write_jsonl(raw_stream, end_record)
        write_jsonl(event_stream, end_record)
        raw_stream.flush()
        event_stream.flush()
        snapshot_stream.flush()
        final_manifest = make_manifest(
            status="finished",
            session_id=session_id,
            started=started,
            pid=pid,
            executable=executable,
            adapter=adapter,
            local_ip=local_ip,
            ports=ports,
            raw_path=raw_path,
            events_path=events_path,
            snapshots_path=snapshots_path,
            cryptor=cryptor,
            counters=counters,
            coverage=coverage,
            remote_counts=remote_counts,
            pcap_stats=pcap_final,
            snapshot_errors=snapshot_errors,
            finished=finished,
        )
        print("NPCAP_SHADOW_FINISHED", flush=True)
        print(f"session={session_dir.resolve()}", flush=True)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        if raw_stream is not None:
            raw_stream.close()
        if event_stream is not None:
            event_stream.close()
        if snapshot_stream is not None:
            snapshot_stream.close()
        if final_manifest is not None and manifest_path is not None and raw_path is not None:
            final_manifest["files"]["raw_packets_sha256"] = sha256_file(raw_path)
            final_manifest["files"]["events_sha256"] = sha256_file(events_path)
            final_manifest["files"]["rc4_snapshots_sha256"] = sha256_file(snapshots_path)
            write_json_atomic(manifest_path, final_manifest)
        if snapshot_reader is not None:
            snapshot_reader.close()
        if pcap_handle and wpcap is not None:
            wpcap.pcap_close(pcap_handle)
        stop_api.CloseHandle(stop_handle)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process-name", default="C7-Win64-Shipping.exe")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--local-ip", default="")
    parser.add_argument("--remote", default="")
    parser.add_argument("--cryptor", type=integer, default=0)
    parser.add_argument("--output-root", default=".codex-tmp/npcap-shadow-runs")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--snapshot-interval", type=float, default=2.0)
    parser.add_argument("--endpoint-refresh-interval", type=float, default=1.0)
    parser.add_argument("--manifest-interval", type=float, default=5.0)
    parser.add_argument("--heartbeat-interval", type=float, default=10.0)
    parser.add_argument("--process-exit-grace", type=float, default=5.0)
    parser.add_argument("--flush-packets", type=int, default=25)
    parser.add_argument("--stop-event", default=DEFAULT_STOP_EVENT)
    parser.add_argument("--stop", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in (
        "snapshot_interval",
        "endpoint_refresh_interval",
        "manifest_interval",
        "heartbeat_interval",
        "process_exit_grace",
    ):
        if float(getattr(args, name)) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be greater than zero")
    if args.duration < 0:
        raise ValueError("--duration cannot be negative")
    if args.flush_packets <= 0:
        raise ValueError("--flush-packets must be greater than zero")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.stop:
            return request_stop(args.stop_event)
        validate_args(args)
        return run(args)
    except Exception as exc:
        print(f"Npcap shadow runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
