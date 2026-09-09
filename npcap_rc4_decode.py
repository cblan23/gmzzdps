#!/usr/bin/env python3
"""Offline-decrypt Npcap KCP PUSH records around a read-only RC4 snapshot.

The snapshot is the RC4 state between two delivered PUSH sequence numbers.
Because RC4's PRGA can be stepped both forward and backward, one snapshot can
recover every contiguous captured PUSH on both sides of that boundary.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from npcap_shadow_capture import parse_kcp_segments


@dataclass(frozen=True)
class Segment:
    sequence: int
    timestamp_epoch: float
    timestamp_utc: str
    packet_index: int
    ciphertext: bytes


class Rc4State:
    def __init__(self, x: int, y: int, permutation: list[int]):
        self.x = int(x)
        self.y = int(y)
        self.s = list(permutation)
        if not (0 <= self.x <= 255 and 0 <= self.y <= 255):
            raise ValueError("RC4 indexes are invalid")
        if sorted(self.s) != list(range(256)):
            raise ValueError("RC4 permutation is invalid")

    def clone(self) -> "Rc4State":
        return Rc4State(self.x, self.y, self.s)

    def forward(self, data: bytes) -> bytes:
        output = bytearray()
        for value in data:
            self.x = (self.x + 1) & 0xFF
            old_x_value = self.s[self.x]
            self.y = (self.y + old_x_value) & 0xFF
            old_y_value = self.s[self.y]
            self.s[self.x] = old_y_value
            self.s[self.y] = old_x_value
            output.append(value ^ self.s[(old_x_value + old_y_value) & 0xFF])
        return bytes(output)

    def backward(self, data: bytes) -> bytes:
        reverse_plaintext = bytearray()
        for value in reversed(data):
            old_x_value = self.s[self.y]
            old_y_value = self.s[self.x]
            reverse_plaintext.append(
                value ^ self.s[(old_x_value + old_y_value) & 0xFF]
            )
            self.s[self.x], self.s[self.y] = self.s[self.y], self.s[self.x]
            self.y = (self.y - old_x_value) & 0xFF
            self.x = (self.x - 1) & 0xFF
        return bytes(reversed(reverse_plaintext))


def load_segments(path: Path, direction: str, remote: str) -> dict[int, Segment]:
    remote_host = ""
    remote_port = 0
    if remote:
        remote_host, separator, raw_port = remote.rpartition(":")
        if not separator:
            raise ValueError("snapshot remote must use host:port")
        remote_port = int(raw_port)
    result: dict[int, Segment] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                continue
            if record.get("record_type") != "udp_packet":
                continue
            if record.get("direction") != direction:
                continue
            if direction == "inbound":
                observed_host = str(record.get("src_ip", ""))
                observed_port = int(record.get("src_port", 0) or 0)
            else:
                observed_host = str(record.get("dst_ip", ""))
                observed_port = int(record.get("dst_port", 0) or 0)
            if remote_host and (observed_host, observed_port) != (
                remote_host,
                remote_port,
            ):
                continue
            raw = base64.b64decode(record["udp_payload_base64"])
            for item in parse_kcp_segments(raw):
                if item.get("command") != "PUSH":
                    continue
                sequence = int(item["sequence"])
                if sequence in result:
                    continue
                offset = int(item["payload_offset"])
                length = int(item["payload_length"])
                result[sequence] = Segment(
                    sequence=sequence,
                    timestamp_epoch=float(record["timestamp_epoch"]),
                    timestamp_utc=str(record["timestamp_utc"]),
                    packet_index=int(record["packet_index"]),
                    ciphertext=raw[offset : offset + length],
                )
    return result


def application_frame_valid(plaintext: bytes) -> bool:
    if len(plaintext) < 3:
        return False
    # The doraemon frame starts with a 20-bit, little-endian body bit count.
    # Its upper nibble shares byte 2 with the first four bits of the body.
    bit_count = int.from_bytes(plaintext[:3], "little") & 0xFFFFF
    return bit_count == len(plaintext) * 8 - 20


def choose_boundary(
    segments: dict[int, Segment], state: Rc4State, snapshot_epoch: float
) -> int:
    ordered = sorted(segments)
    nearby = sorted(
        ordered,
        key=lambda sequence: abs(segments[sequence].timestamp_epoch - snapshot_epoch),
    )[:24]
    candidates = sorted(set(nearby + [sequence - 1 for sequence in nearby]))
    scored: list[tuple[int, float, int]] = []
    for boundary in candidates:
        trial = state.clone()
        valid = 0
        available = 0
        for sequence in range(boundary + 1, boundary + 7):
            segment = segments.get(sequence)
            if segment is None:
                break
            available += 1
            if application_frame_valid(trial.forward(segment.ciphertext)):
                valid += 1
        if available:
            distance = abs(
                segments.get(boundary, segments.get(boundary + 1)).timestamp_epoch
                - snapshot_epoch
            )
            scored.append((valid, -distance, boundary))
    if not scored:
        raise RuntimeError("No PUSH sequence is near the RC4 snapshot")
    valid, _negative_distance, boundary = max(scored)
    if valid < 3:
        raise RuntimeError(
            "Could not align the RC4 state to a PUSH boundary "
            f"(best validation score {valid})"
        )
    return boundary


def contiguous_limits(segments: dict[int, Segment], boundary: int) -> tuple[int, int]:
    lower = boundary
    while lower - 1 in segments:
        lower -= 1
    upper = boundary
    while upper + 1 in segments:
        upper += 1
    return lower, upper


def decode(
    segments: dict[int, Segment], state: Rc4State, boundary: int
) -> dict[int, bytes]:
    lower, upper = contiguous_limits(segments, boundary)
    plaintext: dict[int, bytes] = {}
    backward = state.clone()
    for sequence in range(boundary, lower - 1, -1):
        plaintext[sequence] = backward.backward(segments[sequence].ciphertext)
    forward = state.clone()
    for sequence in range(boundary + 1, upper + 1):
        plaintext[sequence] = forward.forward(segments[sequence].ciphertext)
    return plaintext


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument(
        "--snapshot-line",
        type=int,
        help="zero-based record index when --snapshot is a JSONL file",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--direction", choices=("inbound", "outbound"), default="inbound")
    parser.add_argument("--boundary-sequence", type=int)
    args = parser.parse_args()

    if args.snapshot_line is None:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        if args.snapshot_line < 0:
            raise ValueError("--snapshot-line must be zero or greater")
        with args.snapshot.open("r", encoding="utf-8") as stream:
            snapshot = None
            for index, line in enumerate(stream):
                if index == args.snapshot_line:
                    snapshot = json.loads(line)
                    break
        if snapshot is None:
            raise ValueError(
                f"--snapshot-line {args.snapshot_line} is outside {args.snapshot}"
            )
    state_name = "decrypt" if args.direction == "inbound" else "encrypt"
    raw_state = snapshot[state_name]
    state = Rc4State(raw_state["x"], raw_state["y"], raw_state["s"])
    snapshot_epoch = float(raw_state["read_after_epoch_ns"]) / 1_000_000_000
    segments = load_segments(
        args.capture, args.direction, str(snapshot.get("remote", ""))
    )
    if not segments:
        raise RuntimeError("Capture contains no matching unique PUSH segments")
    boundary = (
        int(args.boundary_sequence)
        if args.boundary_sequence is not None
        else choose_boundary(segments, state, snapshot_epoch)
    )
    if boundary not in segments:
        raise RuntimeError(f"Boundary sequence {boundary} is absent from the capture")
    plaintext = decode(segments, state, boundary)
    valid_count = sum(application_frame_valid(value) for value in plaintext.values())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as output:
        for sequence in sorted(plaintext):
            segment = segments[sequence]
            value = plaintext[sequence]
            output.write(
                json.dumps(
                    {
                        "record_type": "decrypted_push",
                        "direction": args.direction,
                        "sequence": sequence,
                        "timestamp_epoch": segment.timestamp_epoch,
                        "timestamp_utc": segment.timestamp_utc,
                        "packet_index": segment.packet_index,
                        "payload_length": len(value),
                        "application_frame_valid": application_frame_valid(value),
                        "plaintext_base64": base64.b64encode(value).decode("ascii"),
                        "plaintext_hex": value.hex(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    print("NPCAP_RC4_DECODE_READY")
    print(f"output={args.output.resolve()}")
    print(
        f"boundary={boundary} decoded={len(plaintext)} "
        f"valid_application_frames={valid_count}"
    )
    return 0 if valid_count == len(plaintext) else 2


if __name__ == "__main__":
    raise SystemExit(main())
