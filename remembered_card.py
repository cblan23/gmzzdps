#!/usr/bin/env python3
"""Encrypt a remembered card key for the current Windows user."""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes


CONFIG_KEY = "remembered_card_dpapi_v1"
_ENTROPY = b"GMZZDpsMeter/remembered-card/v1"
_MAX_CARD_LENGTH = 80
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def _windows_libraries():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _valid_card_text(value: object) -> str:
    card_key = str(value or "").strip()
    if not 1 <= len(card_key) <= _MAX_CARD_LENGTH:
        return ""
    if any(ord(character) < 0x20 for character in card_key):
        return ""
    return card_key


def protect_card_key(card_key: object) -> str:
    card_key = _valid_card_text(card_key)
    if sys.platform != "win32" or not card_key:
        return ""
    try:
        crypt32, kernel32 = _windows_libraries()
        input_blob, input_buffer = _blob(card_key.encode("utf-8"))
        entropy_blob, entropy_buffer = _blob(_ENTROPY)
        output_blob = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "GMZZ DPS remembered card",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            return ""
        try:
            encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        return base64.b64encode(encrypted).decode("ascii")
    except (OSError, ValueError, TypeError, ctypes.ArgumentError):
        return ""


def unprotect_card_key(encrypted_value: object) -> str:
    if sys.platform != "win32":
        return ""
    try:
        encrypted = base64.b64decode(
            str(encrypted_value or "").encode("ascii"), validate=True
        )
    except (UnicodeEncodeError, ValueError, TypeError):
        return ""
    if not encrypted:
        return ""
    try:
        crypt32, kernel32 = _windows_libraries()
        input_blob, input_buffer = _blob(encrypted)
        entropy_blob, entropy_buffer = _blob(_ENTROPY)
        output_blob = _DataBlob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            return ""
        try:
            cleartext = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        return _valid_card_text(cleartext.decode("utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        ctypes.ArgumentError,
    ):
        return ""


def remember_card(config: dict, card_key: object) -> bool:
    encrypted = protect_card_key(card_key)
    if not encrypted:
        return False
    config[CONFIG_KEY] = encrypted
    return True


def remembered_card(config: dict) -> str:
    return unprotect_card_key(config.get(CONFIG_KEY, ""))
