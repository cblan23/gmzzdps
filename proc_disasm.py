#!/usr/bin/env python3
"""Disassemble a small RVA range directly from a live Windows process."""

from __future__ import annotations

import argparse
import ctypes
import mmap
from pathlib import Path

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
from pe_xrefs import PE


def integer(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rva", type=integer)
    parser.add_argument("size", nargs="?", type=integer, default=0x400)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--module", default="C7-Win64-Shipping.exe")
    parser.add_argument(
        "--image",
        type=Path,
        help="disassemble an on-disk PE image instead of a live process",
    )
    args = parser.parse_args()

    if args.image is not None:
        with args.image.open("rb") as source, mmap.mmap(
            source.fileno(), 0, access=mmap.ACCESS_READ
        ) as mapped:
            image = PE(mapped)
            raw = image.rva_to_raw(args.rva)
            if raw is None or args.rva < 0 or args.rva + args.size > image.size_of_image:
                raise SystemExit(
                    f"requested range is outside mapped image size "
                    f"0x{image.size_of_image:x}"
                )
            section = next(
                item
                for item in image.sections
                if item.virtual_address <= args.rva
                < item.virtual_address + max(item.virtual_size, item.raw_size)
            )
            available = min(
                args.size,
                section.raw_size - (args.rva - section.virtual_address),
            )
            data = bytes(mapped[raw : raw + available])
            base = image.image_base
        path = args.image
        print(f"IMAGE={path} base=0x{base:x}")
    else:
        pid = args.pid or find_pid(args.process)
        base, module_size, path = find_module(pid, args.module)
        if args.rva < 0 or args.rva + args.size > module_size:
            raise SystemExit(
                f"requested range is outside module size 0x{module_size:x}"
            )
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
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
