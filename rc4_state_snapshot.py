#!/usr/bin/env python3
"""Save a read-only snapshot of this game's OpenSSL RC4 stream state.

The tool is intentionally separate from the DPS application. It only opens the
game process with query/read permissions and never writes or injects anything.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

import proc_inspect


CRYPTOR_POINTERS = struct.Struct("<QQQ")
RC4_STATE = struct.Struct("<II256I")
EVP_ENCRYPT_OFFSET = 0x10
EVP_CIPHER_DATA_OFFSET = 0x70


def integer(value: str) -> int:
    return int(value, 0)


def read_exact(process: int, address: int, size: int, label: str) -> bytes:
    data = proc_inspect.read_region(process, address, size)
    if data is None or len(data) != size:
        actual = 0 if data is None else len(data)
        raise RuntimeError(
            f"Could not read {label} at 0x{address:x}: expected {size}, got {actual}"
        )
    return data


def read_pointer(process: int, address: int, label: str) -> int:
    return struct.unpack("<Q", read_exact(process, address, 8, label))[0]


def utc_iso_from_ns(timestamp_ns: int) -> str:
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc).isoformat(
        timespec="microseconds"
    )


def snapshot_state(process: int, address: int, attempts: int = 20) -> dict:
    last_error = ""
    for _ in range(attempts):
        before_ns = time.time_ns()
        raw = read_exact(process, address, RC4_STATE.size, "RC4 state")
        after_ns = time.time_ns()
        values = RC4_STATE.unpack(raw)
        x, y = values[:2]
        permutation = list(values[2:])
        if x > 255 or y > 255:
            last_error = f"invalid RC4 indexes x={x}, y={y}"
            continue
        if len(set(permutation)) != 256 or min(permutation) != 0 or max(permutation) != 255:
            last_error = "RC4 permutation changed during the memory read"
            continue
        return {
            "address": address,
            "read_before_epoch_ns": before_ns,
            "read_after_epoch_ns": after_ns,
            "read_before_utc": utc_iso_from_ns(before_ns),
            "read_after_utc": utc_iso_from_ns(after_ns),
            "read_duration_ns": after_ns - before_ns,
            "x": x,
            "y": y,
            "s": permutation,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    raise RuntimeError(last_error or "Could not obtain a consistent RC4 state")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--cryptor", type=integer, required=True)
    parser.add_argument("--remote", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    access = proc_inspect.PROCESS_QUERY_INFORMATION | proc_inspect.PROCESS_VM_READ
    process = proc_inspect.kernel32.OpenProcess(access, False, args.pid)
    if not process:
        raise proc_inspect.winerror("OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ)")
    try:
        cryptor = read_exact(process, args.cryptor, CRYPTOR_POINTERS.size, "cipher_cryptor")
        _, encrypt_ctx, decrypt_ctx = CRYPTOR_POINTERS.unpack(cryptor)
        if not encrypt_ctx or not decrypt_ctx:
            raise RuntimeError("cipher_cryptor does not contain both EVP contexts")

        encrypt_flag = struct.unpack(
            "<I", read_exact(process, encrypt_ctx + EVP_ENCRYPT_OFFSET, 4, "encrypt flag")
        )[0]
        decrypt_flag = struct.unpack(
            "<I", read_exact(process, decrypt_ctx + EVP_ENCRYPT_OFFSET, 4, "decrypt flag")
        )[0]
        if encrypt_flag != 1 or decrypt_flag != 0:
            raise RuntimeError(
                f"Unexpected EVP directions: encrypt={encrypt_flag}, decrypt={decrypt_flag}"
            )

        encrypt_state = read_pointer(
            process, encrypt_ctx + EVP_CIPHER_DATA_OFFSET, "encrypt cipher_data"
        )
        decrypt_state = read_pointer(
            process, decrypt_ctx + EVP_CIPHER_DATA_OFFSET, "decrypt cipher_data"
        )
        record = {
            "record_type": "rc4_state_snapshot",
            "schema": 1,
            "mode": "read_only",
            "pid": args.pid,
            "remote": args.remote,
            "cryptor": args.cryptor,
            "encrypt_evp_ctx": encrypt_ctx,
            "decrypt_evp_ctx": decrypt_ctx,
            "encrypt": snapshot_state(process, encrypt_state),
            "decrypt": snapshot_state(process, decrypt_state),
        }
    finally:
        proc_inspect.kernel32.CloseHandle(process)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("RC4_STATE_SNAPSHOT_READY", flush=True)
    print(f"output={output.resolve()}", flush=True)
    print(
        f"remote={args.remote or '-'} decrypt_state=0x{record['decrypt']['address']:x} "
        f"x={record['decrypt']['x']} y={record['decrypt']['y']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
