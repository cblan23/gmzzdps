#!/usr/bin/env python3
"""List live executable instructions using a selected memory displacement."""

from __future__ import annotations

import argparse
import mmap
import struct
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_MEM

from pe_xrefs import PE
from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_module,
    find_pid,
    kernel32,
    read_region,
    winerror,
)


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("displacement", type=integer)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, module_size, raw_path = find_module(pid, args.module)
    path = Path(raw_path)
    with path.open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ
    ) as mapped:
        image = PE(mapped)
        sections = [section for section in image.sections if section.executable]

    process = int(
        kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        or 0
    )
    if not process:
        raise winerror("OpenProcess(runtime field xrefs)")
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    encoded = struct.pack("<i", args.displacement)
    results: dict[int, tuple[bytes, str, str]] = {}
    try:
        for section in sections:
            size = min(section.virtual_size, module_size - section.virtual_address)
            data = read_region(process, base + section.virtual_address, size)
            if not data:
                continue
            found = data.find(encoded)
            while found >= 0:
                for back in range(1, 16):
                    start = found - back
                    if start < 0:
                        continue
                    address = base + section.virtual_address + start
                    instructions = list(md.disasm(data[start : found + 12], address, count=1))
                    if not instructions:
                        continue
                    instruction = instructions[0]
                    instruction_end = start + instruction.size
                    if not (start <= found and found + 4 <= instruction_end):
                        continue
                    if any(
                        operand.type == X86_OP_MEM
                        and operand.mem.disp == args.displacement
                        for operand in instruction.operands
                    ):
                        rva = instruction.address - base
                        results[rva] = (
                            bytes(instruction.bytes),
                            instruction.mnemonic,
                            instruction.op_str,
                        )
                found = data.find(encoded, found + 1)
    finally:
        kernel32.CloseHandle(process)

    print(f"PID={pid} module={path} displacement=0x{args.displacement:x}")
    for rva, (raw, mnemonic, operands) in sorted(results.items()):
        print(f"RVA=0x{rva:x} {raw.hex(' '):<32} {mnemonic:<8} {operands}")
    print(f"MATCHES={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
