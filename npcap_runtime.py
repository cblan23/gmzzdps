#!/usr/bin/env python3
"""Detect and validate the installed Npcap runtime without external packages."""

from __future__ import annotations

import ctypes
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MINIMUM_NPCAP_VERSION = (1, 88)


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("signature", ctypes.c_uint32),
        ("struct_version", ctypes.c_uint32),
        ("file_version_ms", ctypes.c_uint32),
        ("file_version_ls", ctypes.c_uint32),
        ("product_version_ms", ctypes.c_uint32),
        ("product_version_ls", ctypes.c_uint32),
        ("file_flags_mask", ctypes.c_uint32),
        ("file_flags", ctypes.c_uint32),
        ("file_os", ctypes.c_uint32),
        ("file_type", ctypes.c_uint32),
        ("file_subtype", ctypes.c_uint32),
        ("file_date_ms", ctypes.c_uint32),
        ("file_date_ls", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class NpcapRuntimeInfo:
    driver_path: Path
    version_parts: tuple[int, int, int, int]

    @property
    def version(self) -> str:
        parts = list(self.version_parts)
        while len(parts) > 2 and parts[-1] == 0:
            parts.pop()
        return ".".join(str(part) for part in parts)


def default_driver_path() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return windows / "System32" / "drivers" / "npcap.sys"


def windows_file_version(path: Path) -> tuple[int, int, int, int]:
    if os.name != "nt":
        raise OSError("Npcap is available only on Windows")

    version_dll = ctypes.WinDLL("version", use_last_error=True)
    version_dll.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    version_dll.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
    version_dll.GetFileVersionInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    version_dll.GetFileVersionInfoW.restype = ctypes.c_int
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    version_dll.VerQueryValueW.restype = ctypes.c_int

    path_text = str(path)
    size = int(version_dll.GetFileVersionInfoSizeW(path_text, None))
    if size <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(path_text, 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())

    def query_text(block: str) -> str:
        text_pointer = ctypes.c_void_p()
        text_size = ctypes.c_uint32()
        if not version_dll.VerQueryValueW(
            buffer,
            block,
            ctypes.byref(text_pointer),
            ctypes.byref(text_size),
        ):
            return ""
        if not text_pointer.value or text_size.value <= 1:
            return ""
        return str(ctypes.cast(text_pointer, ctypes.c_wchar_p).value or "").strip()

    translations: list[tuple[int, int]] = []
    translation_pointer = ctypes.c_void_p()
    translation_size = ctypes.c_uint32()
    if version_dll.VerQueryValueW(
        buffer,
        "\\VarFileInfo\\Translation",
        ctypes.byref(translation_pointer),
        ctypes.byref(translation_size),
    ) and translation_pointer.value:
        raw_translations = ctypes.string_at(
            translation_pointer, int(translation_size.value)
        )
        translations.extend(
            struct.unpack_from("<HH", raw_translations, offset)
            for offset in range(0, len(raw_translations) - 3, 4)
        )
    translations.extend(((0x0409, 0x04B0), (0x0409, 0x04E4)))

    seen_translations: set[tuple[int, int]] = set()
    for language, codepage in translations:
        translation = (int(language), int(codepage))
        if translation in seen_translations:
            continue
        seen_translations.add(translation)
        prefix = f"\\StringFileInfo\\{language:04X}{codepage:04X}"
        for name in ("ProductVersion", "FileVersion"):
            version_text = query_text(f"{prefix}\\{name}")
            numbers = [int(value) for value in re.findall(r"\d+", version_text)]
            if len(numbers) >= 2:
                return tuple((numbers + [0, 0])[:4])

    value = ctypes.c_void_p()
    value_size = ctypes.c_uint32()
    if not version_dll.VerQueryValueW(
        buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if value_size.value < ctypes.sizeof(_FixedFileInfo):
        raise OSError("Npcap driver version metadata is incomplete")

    fixed = ctypes.cast(value, ctypes.POINTER(_FixedFileInfo)).contents
    if fixed.signature != 0xFEEF04BD:
        raise OSError("Npcap driver version metadata is invalid")
    return (
        fixed.file_version_ms >> 16,
        fixed.file_version_ms & 0xFFFF,
        fixed.file_version_ls >> 16,
        fixed.file_version_ls & 0xFFFF,
    )


def require_npcap_runtime(
    minimum: tuple[int, int] = MINIMUM_NPCAP_VERSION,
    *,
    driver_path: Path | None = None,
    version_reader: Callable[[Path], tuple[int, int, int, int]] | None = None,
) -> NpcapRuntimeInfo:
    path = driver_path or default_driver_path()
    if not path.is_file():
        required = ".".join(str(part) for part in minimum)
        raise RuntimeError(f"未检测到 Npcap {required}，请安装后重新打开程序。")

    reader = version_reader or windows_file_version
    try:
        parts = tuple(int(part) for part in reader(path))
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("无法确认 Npcap 版本，请重新安装后再试。") from exc
    if len(parts) != 4:
        raise RuntimeError("无法确认 Npcap 版本，请重新安装后再试。")

    info = NpcapRuntimeInfo(path, parts)
    if parts[:2] < tuple(minimum):
        required = ".".join(str(part) for part in minimum)
        raise RuntimeError(
            f"当前 Npcap 版本为 {info.version}，请升级到 {required} 或更高版本。"
        )
    return info
