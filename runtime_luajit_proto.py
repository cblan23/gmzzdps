#!/usr/bin/env python3
"""Read and disassemble LuaJIT 2.1 GC64 prototypes from a live process.

This research helper opens the process with read-only permissions.  It is
intended for locating the natural arguments used by game-owned Lua RPC calls.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import struct

from proc_inspect import (
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_READ,
    find_pid,
    kernel32,
    read_region,
    winerror,
)


GC64_POINTER_MASK = (1 << 47) - 1
GCPROTO_SIZE = 0x68
GCSTR_SIZE = 0x18

# Opcode order from LuaJIT 2.1's official lj_bc.h.  The game build can patch
# loop opcodes in place, but their operand layout remains compatible.
OPCODES = (
    "ISLT", "ISGE", "ISLE", "ISGT", "ISEQV", "ISNEV", "ISEQS", "ISNES",
    "ISEQN", "ISNEN", "ISEQP", "ISNEP", "ISTC", "ISFC", "IST", "ISF",
    "ISTYPE", "ISNUM", "MOV", "NOT", "UNM", "LEN", "ADDVN", "SUBVN",
    "MULVN", "DIVVN", "MODVN", "ADDNV", "SUBNV", "MULNV", "DIVNV",
    "MODNV", "ADDVV", "SUBVV", "MULVV", "DIVVV", "MODVV", "POW", "CAT",
    "KSTR", "KCDATA", "KSHORT", "KNUM", "KPRI", "KNIL", "UGET", "USETV",
    "USETS", "USETN", "USETP", "UCLO", "FNEW", "TNEW", "TDUP", "GGET",
    "GSET", "TGETV", "TGETS", "TGETB", "TGETR", "TSETV", "TSETS", "TSETB",
    "TSETM", "TSETR", "CALLM", "CALL", "CALLMT", "CALLT", "ITERC", "ITERN",
    "VARG", "ISNEXT", "RETM", "RET", "RET0", "RET1", "FORI", "JFORI",
    "FORL", "IFORL", "JFORL", "ITERL", "IITERL", "JITERL", "LOOP", "ILOOP",
    "JLOOP", "JMP", "BNOT", "BAND", "BOR", "BXOR", "BSHL", "BSHR", "BSAR",
    "FUNCF", "IFUNCF", "JFUNCF", "FUNCV", "IFUNCV", "JFUNCV", "FUNCC",
    "FUNCCW",
)

STRING_D_OPS = {"ISEQS", "ISNES", "KSTR", "USETS", "GGET", "GSET"}
STRING_C_OPS = {"TGETS", "TSETS"}
NUMBER_D_OPS = {"ISEQN", "ISNEN", "KNUM", "USETN", "TSETM"}
NUMBER_C_OPS = {"ADDVN", "SUBVN", "MULVN", "DIVVN", "MODVN"}
NUMBER_B_OPS = {"ADDNV", "SUBNV", "MULNV", "DIVNV", "MODNV"}
JUMP_OPS = {
    "UCLO", "ISNEXT", "FORI", "JFORI", "FORL", "IFORL", "ITERL",
    "IITERL", "LOOP", "ILOOP", "JMP",
}


def integer(value: str) -> int:
    return int(value, 0)


def pointer(value: int) -> int:
    return value & GC64_POINTER_MASK


def read_exact(process: int, address: int, size: int, label: str) -> bytes:
    data = read_region(process, address, size)
    if data is None or len(data) != size:
        raise RuntimeError(
            f"could not read {label} at 0x{address:x}: "
            f"expected {size} bytes, got {0 if data is None else len(data)}"
        )
    return data


def read_gc_string(process: int, address: int) -> str | None:
    address = pointer(address)
    if address < 0x10000:
        return None
    header = read_region(process, address, GCSTR_SIZE)
    if header is None or len(header) != GCSTR_SIZE or header[9] != 4:
        return None
    length = struct.unpack_from("<I", header, 0x14)[0]
    if length > 1024 * 1024:
        return None
    raw = read_region(process, address + GCSTR_SIZE, length)
    if raw is None or len(raw) != length:
        return None
    return raw.decode("utf-8", errors="replace")


def describe_gc(process: int, raw_pointer: int) -> tuple[str, int | None]:
    address = pointer(raw_pointer)
    if address < 0x10000:
        return f"invalid(0x{raw_pointer:016x})", None
    head = read_region(process, address, 0x18)
    if head is None or len(head) < 10:
        return f"unreadable(0x{address:x})", None
    gct = head[9]
    if gct == 4:
        text = read_gc_string(process, address)
        return f"string {text!r}", None
    if gct == 9:
        return f"proto 0x{address:x}", address
    type_names = {
        5: "upvalue", 6: "thread", 7: "proto", 8: "function",
        9: "proto", 10: "cdata", 11: "table", 12: "userdata",
    }
    return f"{type_names.get(gct, f'gct_{gct}')} 0x{address:x}", None


def parse_proto_header(raw: bytes) -> dict[str, int]:
    return {
        "gct": raw[9],
        "numparams": raw[0x0A],
        "framesize": raw[0x0B],
        "sizebc": struct.unpack_from("<I", raw, 0x0C)[0],
        "gclist": struct.unpack_from("<Q", raw, 0x18)[0],
        "k": struct.unpack_from("<Q", raw, 0x20)[0],
        "uv": struct.unpack_from("<Q", raw, 0x28)[0],
        "sizekgc": struct.unpack_from("<I", raw, 0x30)[0],
        "sizekn": struct.unpack_from("<I", raw, 0x34)[0],
        "sizept": struct.unpack_from("<I", raw, 0x38)[0],
        "sizeuv": raw[0x3C],
        "flags": raw[0x3D],
        "trace": struct.unpack_from("<H", raw, 0x3E)[0],
        "chunkname": struct.unpack_from("<Q", raw, 0x40)[0],
        "firstline": struct.unpack_from("<i", raw, 0x48)[0],
        "numline": struct.unpack_from("<i", raw, 0x4C)[0],
        "lineinfo": struct.unpack_from("<Q", raw, 0x50)[0],
        "uvinfo": struct.unpack_from("<Q", raw, 0x58)[0],
        "varinfo": struct.unpack_from("<Q", raw, 0x60)[0],
    }


def decode_number(raw: int) -> str:
    signed = ctypes.c_int64(raw).value
    tag = signed >> 47
    if tag == -14:
        return str(ctypes.c_int32(raw & 0xFFFFFFFF).value)
    number = struct.unpack("<d", struct.pack("<Q", raw))[0]
    if math.isfinite(number):
        return repr(number)
    return f"raw(0x{raw:016x})"


def resolve_constant(constants: list[str], index: int) -> str:
    if 0 <= index < len(constants):
        return constants[index]
    return f"<constant {index} out of range>"


def instruction_text(ins: int, kgc: list[str], knum: list[str]) -> str:
    op_number = ins & 0xFF
    op = OPCODES[op_number] if op_number < len(OPCODES) else f"OP_{op_number}"
    a = (ins >> 8) & 0xFF
    c = (ins >> 16) & 0xFF
    b = (ins >> 24) & 0xFF
    d = (ins >> 16) & 0xFFFF
    detail = f"A={a} B={b} C={c} D={d}"
    if op in STRING_D_OPS:
        detail += f" ; {resolve_constant(kgc, d)}"
    elif op in STRING_C_OPS:
        detail += f" ; {resolve_constant(kgc, c)}"
    elif op in NUMBER_D_OPS:
        detail += f" ; {resolve_constant(knum, d)}"
    elif op in NUMBER_C_OPS:
        detail += f" ; {resolve_constant(knum, c)}"
    elif op in NUMBER_B_OPS:
        detail += f" ; {resolve_constant(knum, b)}"
    elif op in JUMP_OPS:
        detail += f" ; jump={d - 0x8000:+d}"
    elif op == "KSHORT":
        detail += f" ; literal={ctypes.c_int16(d).value}"
    return f"{op:<8} {detail}"


def dump_proto(
    process: int,
    address: int,
    *,
    depth: int,
    max_depth: int,
    seen: set[int],
) -> None:
    address = pointer(address)
    prefix = "  " * depth
    if address in seen:
        print(f"{prefix}PROTO 0x{address:x} (already shown)")
        return
    seen.add(address)
    raw = read_exact(process, address, GCPROTO_SIZE, "GCproto")
    header = parse_proto_header(raw)
    chunk = read_gc_string(process, header["chunkname"])
    print(
        f"{prefix}PROTO 0x{address:x} gct={header['gct']} "
        f"params={header['numparams']} frame={header['framesize']} "
        f"bc={header['sizebc']} kgc={header['sizekgc']} kn={header['sizekn']} "
        f"uv={header['sizeuv']} flags=0x{header['flags']:02x} "
        f"lines={header['firstline']}+{header['numline']} chunk={chunk!r}"
    )
    if (
        header["gct"] not in {7, 9}
        or header["sizebc"] > 1_000_000
        or header["sizekgc"] > 1_000_000
        or header["sizekn"] > 1_000_000
    ):
        raise RuntimeError(f"0x{address:x} does not look like a valid GCproto")

    kgc: list[str] = []
    children: list[int] = []
    for index in range(header["sizekgc"]):
        cell = header["k"] - (index + 1) * 8
        raw_pointer = struct.unpack(
            "<Q", read_exact(process, cell, 8, "KGC cell")
        )[0]
        description, child = describe_gc(process, raw_pointer)
        kgc.append(description)
        if child is not None:
            children.append(child)
        print(
            f"{prefix}  KGC[{index:03d}] cell=0x{cell:x} "
            f"raw=0x{raw_pointer:016x} {description}"
        )

    knum: list[str] = []
    for index in range(header["sizekn"]):
        cell = header["k"] + index * 8
        raw_number = struct.unpack(
            "<Q", read_exact(process, cell, 8, "numeric constant")
        )[0]
        value = decode_number(raw_number)
        knum.append(value)
        print(f"{prefix}  KNUM[{index:03d}] cell=0x{cell:x} {value}")

    bytecode = read_exact(
        process, address + GCPROTO_SIZE, header["sizebc"] * 4, "bytecode"
    )
    for pc, ins in enumerate(struct.unpack(f"<{header['sizebc']}I", bytecode)):
        print(f"{prefix}  BC[{pc:04d}] 0x{ins:08x} {instruction_text(ins, kgc, knum)}")

    if depth < max_depth:
        for child in children:
            dump_proto(
                process,
                child,
                depth=depth + 1,
                max_depth=max_depth,
                seen=seen,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("address", type=integer, help="GCproto or GCfuncL address")
    parser.add_argument("--function", action="store_true", help="address is GCfuncL")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process", default="C7-Win64-Shipping.exe")
    parser.add_argument("--max-depth", type=int, default=2)
    args = parser.parse_args()

    pid = args.pid or find_pid(args.process)
    process = int(
        kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        or 0
    )
    if not process:
        raise winerror("OpenProcess(runtime LuaJIT prototype)")
    try:
        address = pointer(args.address)
        if args.function:
            function = read_exact(process, address, 0x28, "GCfuncL")
            pc = struct.unpack_from("<Q", function, 0x20)[0]
            print(
                f"FUNCTION 0x{address:x} ffid={function[0x0a]} "
                f"nupvalues={function[0x0b]} pc=0x{pc:x}"
            )
            address = pc - GCPROTO_SIZE
        dump_proto(
            process,
            address,
            depth=0,
            max_depth=max(0, args.max_depth),
            seen=set(),
        )
    finally:
        kernel32.CloseHandle(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
