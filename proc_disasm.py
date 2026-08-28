#!/usr/bin/env python3
"""Disassemble a small RVA range directly from a live Windows process."""

from __future__ import annotations

import argparse
import ctypes

from capstone import Cs, CS_ARCH_X86, CS_MODE_64

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
    parser.add_argument("rva", type=integer)
    parser.add_argument("size", nargs="?", type=integer, default=0x400)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    base, module_size, path = find_module(pid, args.module)
    if args.rva < 0 or args.rva + args.size > module_size:
        raise SystemExit(f"requested range is outside module size 0x{module_size:x}")
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise winerror("OpenProcess")
    try:
        data = read_region(handle, base + args.rva, args.size)
    finally:
        kernel32.CloseHandle(handle)
    if not data:
        raise winerror("ReadProcessMemory")

    print(f"PID={pid} module={path} base=0x{base:x}")
    print(f"range RVA 0x{args.rva:x}..0x{args.rva+len(data):x}")
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    # Shipping images contain alignment/data islands, and callers often start
    # a few bytes before an unknown function boundary while tracing xrefs.
    md.skipdata = True
    for insn in md.disasm(data, base + args.rva):
        rva = insn.address - base
        raw = bytes(insn.bytes).hex(" ")
        print(f"{rva:09x}  {raw:<32} {insn.mnemonic:<8} {insn.op_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
