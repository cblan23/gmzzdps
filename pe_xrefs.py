#!/usr/bin/env python3
"""Small PE64 string/xref inspector for the C7 shipping binary.

It intentionally has no pefile dependency.  The executable uses non-standard
section names, so sections are selected by their PE characteristics instead.
"""

from __future__ import annotations

import argparse
import mmap
import re
import struct
from dataclasses import dataclass
from pathlib import Path


IMAGE_SCN_MEM_EXECUTE = 0x20000000


@dataclass(frozen=True)
class Section:
    name: str
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_offset: int
    characteristics: int

    @property
    def executable(self) -> bool:
        return bool(self.characteristics & IMAGE_SCN_MEM_EXECUTE)


class PE:
    def __init__(self, data: mmap.mmap):
        self.data = data
        if data[:2] != b"MZ":
            raise ValueError("not an MZ executable")
        peoff = struct.unpack_from("<I", data, 0x3C)[0]
        if data[peoff : peoff + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        coff = peoff + 4
        section_count = struct.unpack_from("<H", data, coff + 2)[0]
        optional_size = struct.unpack_from("<H", data, coff + 16)[0]
        optional = coff + 20
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic != 0x20B:
            raise ValueError(f"expected PE32+, got optional magic 0x{magic:x}")
        self.image_base = struct.unpack_from("<Q", data, optional + 24)[0]
        self.size_of_image = struct.unpack_from("<I", data, optional + 56)[0]
        section_table = optional + optional_size
        sections: list[Section] = []
        for index in range(section_count):
            off = section_table + index * 40
            name = bytes(data[off : off + 8]).split(b"\0", 1)[0].decode("ascii", "replace")
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
                "<IIII", data, off + 8
            )
            characteristics = struct.unpack_from("<I", data, off + 36)[0]
            sections.append(
                Section(
                    name,
                    virtual_size,
                    virtual_address,
                    raw_size,
                    raw_offset,
                    characteristics,
                )
            )
        self.sections = sections

    def raw_to_rva(self, raw: int) -> int | None:
        for sec in self.sections:
            if sec.raw_offset <= raw < sec.raw_offset + sec.raw_size:
                return sec.virtual_address + raw - sec.raw_offset
        return None

    def rva_to_raw(self, rva: int) -> int | None:
        for sec in self.sections:
            span = max(sec.virtual_size, sec.raw_size)
            if sec.virtual_address <= rva < sec.virtual_address + span:
                delta = rva - sec.virtual_address
                if delta < sec.raw_size:
                    return sec.raw_offset + delta
        return None

    def describe_rva(self, rva: int) -> str:
        for sec in self.sections:
            span = max(sec.virtual_size, sec.raw_size)
            if sec.virtual_address <= rva < sec.virtual_address + span:
                return f"{sec.name}+0x{rva - sec.virtual_address:x}"
        return "<outside sections>"


def all_occurrences(data: mmap.mmap, needle: bytes):
    start = 0
    while True:
        found = data.find(needle, start)
        if found < 0:
            return
        yield found
        start = found + 1


def scan_pointer_values(pe: PE, values: set[int]):
    """Find raw little-endian VA/RVA values that point at a target string."""
    for value in values:
        encodings: list[tuple[int, bytes]] = []
        if value <= 0xFFFFFFFF:
            encodings.append((4, struct.pack("<I", value)))
        encodings.append((8, struct.pack("<Q", value)))
        for width, needle in encodings:
            for raw in all_occurrences(pe.data, needle):
                rva = pe.raw_to_rva(raw)
                if rva is not None:
                    yield width, raw, rva, value


def scan_rip_xrefs(pe: PE, targets: set[int]):
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        from capstone.x86 import X86_OP_MEM, X86_REG_RIP
    except ImportError as exc:
        raise SystemExit("capstone is required: py -m pip install capstone") from exc

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    # Shipping images contain alignment/data islands inside executable sections.
    md.skipdata = True
    for sec in pe.sections:
        if not sec.executable or not sec.raw_size:
            continue
        # Disassemble in chunks.  An overlap covers instructions crossing a boundary.
        chunk_size = 4 * 1024 * 1024
        overlap = 15
        for chunk_at in range(0, sec.raw_size, chunk_size):
            read_at = max(0, chunk_at - overlap)
            read_end = min(sec.raw_size, chunk_at + chunk_size)
            code = pe.data[sec.raw_offset + read_at : sec.raw_offset + read_end]
            address = pe.image_base + sec.virtual_address + read_at
            min_address = pe.image_base + sec.virtual_address + chunk_at
            for insn in md.disasm(code, address):
                if insn.address < min_address:
                    continue
                if insn.id == 0:  # Capstone skipdata pseudo-instruction.
                    continue
                for operand in insn.operands:
                    if operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP:
                        target = insn.address + insn.size + operand.mem.disp
                        target_rva = target - pe.image_base
                        if target_rva in targets:
                            yield insn, target_rva


_RIP_MEMORY_RE = re.compile(
    # Optional operand-size prefix, optional REX, then common one-byte opcodes
    # whose RIP-relative disp32 is the final instruction field.
    rb"(?:\x66)?(?:[\x40-\x4f])?[\x03\x0b\x23\x2b\x33\x3b\x63\x8b\x89\x8d]"
    rb"[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}",
    re.DOTALL,
)


def scan_rip_xrefs_fast(pe: PE, targets: set[int]):
    """Fast scan for common x64 RIP-relative memory operands.

    This is sufficient for compiler-generated LEA/MOV references to UE static
    log records and avoids crossing the Python boundary for every instruction.
    Candidate matches are decoded with Capstone before being reported.
    """
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    from capstone.x86 import X86_OP_MEM, X86_REG_RIP

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    for sec in pe.sections:
        if not sec.executable or not sec.raw_size:
            continue
        start = sec.raw_offset
        end = start + sec.raw_size
        for match in _RIP_MEMORY_RE.finditer(pe.data, start, end):
            instruction_end_raw = match.end()
            instruction_end_rva = sec.virtual_address + instruction_end_raw - sec.raw_offset
            disp = struct.unpack_from("<i", pe.data, instruction_end_raw - 4)[0]
            target_rva = instruction_end_rva + disp
            if target_rva not in targets:
                continue
            address = pe.image_base + sec.virtual_address + match.start() - sec.raw_offset
            insns = list(md.disasm(pe.data[match.start() : match.end()], address, count=1))
            if not insns:
                continue
            insn = insns[0]
            if insn.size != match.end() - match.start():
                continue
            valid = False
            for operand in insn.operands:
                if operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP:
                    resolved = insn.address + insn.size + operand.mem.disp - pe.image_base
                    valid |= resolved == target_rva
            if valid:
                yield insn, target_rva


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("strings", nargs="+")
    parser.add_argument("--context", type=int, default=64)
    parser.add_argument("--no-disasm", action="store_true")
    parser.add_argument(
        "--rva",
        action="append",
        default=[],
        type=lambda value: int(value, 0),
        help="additional target RVA (repeatable, accepts 0x...)",
    )
    parser.add_argument("--full-disasm", action="store_true")
    args = parser.parse_args()

    with args.exe.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pe = PE(data)
        print(f"image base: 0x{pe.image_base:x}, image size: 0x{pe.size_of_image:x}")
        for sec in pe.sections:
            print(
                f"section {sec.name!r}: RVA 0x{sec.virtual_address:x}, "
                f"VSZ 0x{sec.virtual_size:x}, RAW 0x{sec.raw_offset:x}+0x{sec.raw_size:x}, "
                f"chars 0x{sec.characteristics:08x}"
            )

        targets: dict[int, str] = {}
        for rva in args.rva:
            targets[rva] = f"explicit RVA 0x{rva:x}"
        for text in args.strings:
            for encoding, needle in (("ascii", text.encode()), ("utf16", text.encode("utf-16le"))):
                for raw in all_occurrences(data, needle):
                    rva = pe.raw_to_rva(raw)
                    if rva is None:
                        continue
                    targets[rva] = f"{text} ({encoding})"
                    lo = max(0, raw - args.context)
                    hi = min(len(data), raw + len(needle) + args.context)
                    context = bytes(data[lo:hi]).replace(b"\0", b".")
                    print(
                        f"string {text!r} [{encoding}] raw=0x{raw:x} rva=0x{rva:x} "
                        f"va=0x{pe.image_base + rva:x} ({pe.describe_rva(rva)})"
                    )
                    print(f"  context: {context!r}")

        if not targets:
            print("no target strings found")
            return 1

        pointer_targets = set(targets) | {pe.image_base + rva for rva in targets}
        print("pointer-value references:")
        for width, raw, rva, value in scan_pointer_values(pe, pointer_targets):
            print(
                f"  {width * 8}-bit raw=0x{raw:x} rva=0x{rva:x} "
                f"({pe.describe_rva(rva)}) -> 0x{value:x}"
            )

        if not args.no_disasm:
            print("RIP-relative code references:")
            scanner = scan_rip_xrefs if args.full_disasm else scan_rip_xrefs_fast
            for insn, target_rva in scanner(pe, set(targets)):
                insn_rva = insn.address - pe.image_base
                print(
                    f"  rva=0x{insn_rva:x} ({pe.describe_rva(insn_rva)}): "
                    f"{insn.mnemonic} {insn.op_str} -> {targets[target_rva]!r}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
