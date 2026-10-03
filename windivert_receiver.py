"""Receive-only WinDivert packet source for native IPv6 compatibility.

The public receiver contract matches ``WindowsRawSocketReceiver``.  Only the
capture APIs are bound: the packet injection exports are intentionally absent,
and every handle is opened with SNIFF + RECV_ONLY.
"""
from __future__ import annotations

import ctypes
import ipaddress
import os
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from passive_transport import CaptureFrame
from windows_raw_receiver import DLT_RAW, RawSocketUnavailable

try:
    import winreg
except ImportError:  # pragma: no cover - imported only by Windows builds.
    winreg = None


WINDIVERT_LAYER_NETWORK = 0
WINDIVERT_FLAG_SNIFF = 0x0001
WINDIVERT_FLAG_RECV_ONLY = 0x0004
WINDIVERT_SHUTDOWN_RECV = 0x1
WINDIVERT_PARAM_QUEUE_LENGTH = 0
WINDIVERT_PARAM_QUEUE_TIME = 1
WINDIVERT_PARAM_QUEUE_SIZE = 2
WINDIVERT_PARAM_VERSION_MAJOR = 3
WINDIVERT_PARAM_VERSION_MINOR = 4
WINDIVERT_QUEUE_LENGTH = 16_384
WINDIVERT_QUEUE_TIME_MS = 2_000
WINDIVERT_QUEUE_SIZE = 32 * 1024 * 1024
WINDIVERT_MTU_MAX = 40 + 0xFFFF
WINDIVERT_ADDRESS_SIZE = 80
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_FILE_NOT_FOUND = 2
SC_MANAGER_CONNECT = 0x0001
DELETE_SERVICE = 0x00010000
RECEIVE_CLOCK_RESOLUTION_NS = max(
    1, round(time.get_clock_info("time").resolution * 1_000_000_000)
)


class WinDivertUnavailable(RawSocketUnavailable):
    """The bundled receive-only WinDivert source cannot be opened."""


class _WinDivertAddress(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("reserved3", ctypes.c_ubyte * 64),
    ]

    @property
    def outbound(self) -> bool:
        return bool((int(self.flags) >> 17) & 1)


_ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", _ULONG_PTR),
        ("internal_high", _ULONG_PTR),
        ("offset", ctypes.c_uint32),
        ("offset_high", ctypes.c_uint32),
        ("event", ctypes.c_void_p),
    ]


if ctypes.sizeof(_WinDivertAddress) != WINDIVERT_ADDRESS_SIZE:
    raise RuntimeError("Unexpected WinDivert address layout")


@dataclass(frozen=True)
class WinDivertHandle:
    handle: object
    timestamp_resolution_ns: int = RECEIVE_CLOCK_RESOLUTION_NS
    capture_mode: int = WINDIVERT_FLAG_SNIFF | WINDIVERT_FLAG_RECV_ONLY


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def locate_windivert_dll(bundle_directory: Path | None = None) -> Path:
    root = Path(bundle_directory or _bundle_dir())
    candidates = (
        root / "windivert" / "WinDivert.dll",
        root / "third_party" / "windivert" / "x64" / "WinDivert.dll",
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.with_name("WinDivert64.sys").is_file():
            return candidate
    raise WinDivertUnavailable(
        "IPv6 采集组件缺失：WinDivert.dll / WinDivert64.sys 未随程序发布"
    )


def _bind_library(dll) -> None:
    dll.WinDivertOpen.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int16,
        ctypes.c_uint64,
    ]
    dll.WinDivertOpen.restype = ctypes.c_void_p
    dll.WinDivertRecvEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint64,
        ctypes.POINTER(_WinDivertAddress),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_Overlapped),
    ]
    dll.WinDivertRecvEx.restype = ctypes.c_int
    dll.WinDivertShutdown.argtypes = [ctypes.c_void_p, ctypes.c_int]
    dll.WinDivertShutdown.restype = ctypes.c_int
    dll.WinDivertClose.argtypes = [ctypes.c_void_p]
    dll.WinDivertClose.restype = ctypes.c_int
    dll.WinDivertSetParam.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint64,
    ]
    dll.WinDivertSetParam.restype = ctypes.c_int
    dll.WinDivertGetParam.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    dll.WinDivertGetParam.restype = ctypes.c_int


def load_windivert(bundle_directory: Path | None = None):
    if os.name != "nt":
        raise WinDivertUnavailable("WinDivert capture is available only on Windows")
    path = locate_windivert_dll(bundle_directory)
    try:
        dll = ctypes.WinDLL(str(path), use_last_error=True)
    except OSError as exc:
        raise WinDivertUnavailable(f"IPv6 采集组件无法加载：{exc}") from exc
    _bind_library(dll)
    return dll


def _normalise_driver_path(value: object) -> Path | None:
    text = str(value or "").strip().strip('"')
    if text.startswith("\\??\\"):
        text = text[4:]
    if text.casefold().startswith("\\systemroot\\"):
        text = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / text[12:])
    text = os.path.expandvars(text)
    return Path(text) if text else None


def _remove_stale_windivert_service() -> bool:
    """Remove only a stopped/unstartable registration whose image is gone.

    Old portable WinDivert applications can leave a demand-start service that
    points at a deleted temporary directory. In that state the official DLL
    sees the service, attempts to start it, and returns ERROR_FILE_NOT_FOUND
    instead of registering the valid bundled driver.
    """
    if os.name != "nt" or winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\WinDivert",
        ) as key:
            image_path, _kind = winreg.QueryValueEx(key, "ImagePath")
    except OSError:
        return False
    resolved = _normalise_driver_path(image_path)
    if resolved is None or resolved.exists():
        return False

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenSCManagerW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.DeleteService.argtypes = [ctypes.c_void_p]
    advapi32.DeleteService.restype = ctypes.c_int
    advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    advapi32.CloseServiceHandle.restype = ctypes.c_int
    manager = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        return False
    service = None
    try:
        service = advapi32.OpenServiceW(manager, "WinDivert", DELETE_SERVICE)
        if not service:
            return False
        return bool(advapi32.DeleteService(service))
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(manager)


def _address_version(raw: object) -> int | None:
    try:
        address = ipaddress.ip_address(str(raw or ""))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return 4
    return address.version


def endpoint_needs_ipv6(endpoint: object) -> bool:
    return any(
        _address_version(getattr(endpoint, field, "")) == 6
        for field in ("local_address", "remote_address")
    )


def _endpoint_protocol(endpoint: object) -> str:
    return str(getattr(endpoint, "protocol", "udp") or "udp").casefold()


def windivert_filter(
    endpoints: Iterable[object], *, versions: Iterable[int] = (6,)
) -> str:
    """Build a narrow kernel filter around game-owned local transport ports."""
    version_set = {int(value) for value in versions if int(value) in (4, 6)}
    if not version_set:
        raise ValueError("At least one IP version is required")
    version_clause = " or ".join(
        value for version, value in ((4, "ip"), (6, "ipv6")) if version in version_set
    )
    clauses: list[str] = []
    rows = list(endpoints)
    for protocol in ("udp", "tcp"):
        ports = sorted(
            {
                int(getattr(row, "local_port", 0) or 0)
                for row in rows
                if _endpoint_protocol(row) == protocol
                and 0 < int(getattr(row, "local_port", 0) or 0) <= 65535
            }
        )
        if not ports:
            continue
        # ``localPort`` is flow/socket-layer metadata and is not available at
        # the NETWORK layer where full payloads are captured. Match both wire
        # directions with the transport header fields instead.
        local_ports = " or ".join(
            f"{protocol}.SrcPort == {port} or {protocol}.DstPort == {port}"
            for port in ports
        )
        clauses.append(f"({protocol} and ({local_ports}))")
    if not clauses:
        raise WinDivertUnavailable("没有可用于 IPv6 采集的游戏 TCP/UDP 端口")
    return f"({version_clause}) and ({' or '.join(clauses)})"


def _packet_addresses(packet: bytes) -> tuple[str, str] | None:
    if len(packet) >= 20 and packet[0] >> 4 == 4:
        return (
            socket.inet_ntop(socket.AF_INET, packet[12:16]),
            socket.inet_ntop(socket.AF_INET, packet[16:20]),
        )
    if len(packet) >= 40 and packet[0] >> 4 == 6:
        return (
            socket.inet_ntop(socket.AF_INET6, packet[8:24]),
            socket.inet_ntop(socket.AF_INET6, packet[24:40]),
        )
    return None


class WinDivertReceiver:
    """Bounded-polling receive-only source for full IPv4/IPv6 IP packets."""

    def __init__(
        self,
        endpoints: Iterable[object],
        *,
        versions: Iterable[int] = (6,),
        dll=None,
        bundle_directory: Path | None = None,
        poll_seconds: float = 0.01,
        clock_ns: Callable[[], int] = time.time_ns,
    ):
        self.dll = dll or load_windivert(bundle_directory)
        self.versions = frozenset(int(value) for value in versions)
        self.poll_milliseconds = max(1, min(100, round(float(poll_seconds) * 1000)))
        self.clock_ns = clock_ns
        self.endpoints = list(endpoints)
        self.local_addresses: set[str] = set()
        self.handles: dict[str, tuple[WinDivertHandle, int, str]] = {}
        self.counters: Counter[str] = Counter()
        self.errors: dict[str, str] = {}
        self.closed = False
        self._packet = ctypes.create_string_buffer(WINDIVERT_MTU_MAX)
        self._address = _WinDivertAddress()
        self._received = ctypes.c_uint32(0)
        self._address_length = ctypes.c_uint32(WINDIVERT_ADDRESS_SIZE)
        self._overlapped = _Overlapped()
        self._pending = False
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind_kernel32()
        self._event = self._kernel32.CreateEventW(None, True, False, None)
        if not self._event:
            raise WinDivertUnavailable(f"IPv6 receive event creation failed: {ctypes.WinError(ctypes.get_last_error())}")
        self._overlapped.event = self._event
        try:
            self.refresh(self.endpoints)
        except BaseException:
            self.close()
            raise

    def _bind_kernel32(self) -> None:
        self._kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
        self._kernel32.ResetEvent.restype = ctypes.c_int
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.GetOverlappedResult.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Overlapped),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_int,
        ]
        self._kernel32.GetOverlappedResult.restype = ctypes.c_int
        self._kernel32.CancelIoEx.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Overlapped),
        ]
        self._kernel32.CancelIoEx.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int

    def _open(self, expression: str) -> WinDivertHandle:
        flags = WINDIVERT_FLAG_SNIFF | WINDIVERT_FLAG_RECV_ONLY
        encoded = expression.encode("ascii")
        handle = self.dll.WinDivertOpen(encoded, WINDIVERT_LAYER_NETWORK, 0, flags)
        error = ctypes.get_last_error()
        if (
            (not handle or int(handle) == INVALID_HANDLE_VALUE)
            and error == ERROR_FILE_NOT_FOUND
            and _remove_stale_windivert_service()
        ):
            handle = self.dll.WinDivertOpen(
                encoded, WINDIVERT_LAYER_NETWORK, 0, flags
            )
            error = ctypes.get_last_error()
        if not handle or int(handle) == INVALID_HANDLE_VALUE:
            raise WinDivertUnavailable(
                f"Windows IPv6 被动采集无法启动（WinDivert 错误 {error}）：{ctypes.WinError(error)}"
            )
        try:
            for parameter, value in (
                (WINDIVERT_PARAM_QUEUE_LENGTH, WINDIVERT_QUEUE_LENGTH),
                (WINDIVERT_PARAM_QUEUE_TIME, WINDIVERT_QUEUE_TIME_MS),
                (WINDIVERT_PARAM_QUEUE_SIZE, WINDIVERT_QUEUE_SIZE),
            ):
                if not self.dll.WinDivertSetParam(handle, parameter, value):
                    raise ctypes.WinError(ctypes.get_last_error())
            return WinDivertHandle(handle)
        except BaseException:
            self.dll.WinDivertClose(handle)
            raise

    def _cancel_pending(self, handle: object) -> None:
        if not self._pending:
            return
        self._kernel32.CancelIoEx(handle, ctypes.byref(self._overlapped))
        self._kernel32.WaitForSingleObject(self._event, 1_000)
        transferred = ctypes.c_uint32(0)
        self._kernel32.GetOverlappedResult(
            handle, ctypes.byref(self._overlapped), ctypes.byref(transferred), False
        )
        self._pending = False
        self._kernel32.ResetEvent(self._event)

    def _close_capture_handle(self, capture: WinDivertHandle) -> None:
        self._cancel_pending(capture.handle)
        self.dll.WinDivertShutdown(capture.handle, WINDIVERT_SHUTDOWN_RECV)
        self.dll.WinDivertClose(capture.handle)

    def refresh(self, endpoints: Iterable[object]) -> None:
        endpoints = list(endpoints)
        expression = windivert_filter(endpoints, versions=self.versions)
        existing = self.handles.get("windivert")
        if existing is not None and existing[2] == expression:
            self.endpoints = endpoints
            return
        if existing is not None:
            self._close_capture_handle(existing[0])
            self.handles.clear()
        try:
            opened = self._open(expression)
        except (OSError, RuntimeError) as exc:
            self.errors["windivert"] = f"{type(exc).__name__}: {exc}"
            raise WinDivertUnavailable(str(exc)) from exc
        self.handles["windivert"] = (opened, DLT_RAW, expression)
        self.errors.clear()
        self.endpoints = endpoints
        for endpoint in endpoints:
            raw = str(getattr(endpoint, "local_address", "") or "")
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if not address.is_unspecified and not address.is_multicast:
                self.local_addresses.add(str(address))
        self.counters["capture_adapter_opens"] += 1

    def _start_receive(self, handle: object) -> bytes | None:
        self._received.value = 0
        self._address_length.value = WINDIVERT_ADDRESS_SIZE
        ctypes.memset(ctypes.byref(self._overlapped), 0, ctypes.sizeof(self._overlapped))
        self._overlapped.event = self._event
        self._kernel32.ResetEvent(self._event)
        result = self.dll.WinDivertRecvEx(
            handle,
            self._packet,
            len(self._packet),
            ctypes.byref(self._received),
            0,
            ctypes.byref(self._address),
            ctypes.byref(self._address_length),
            ctypes.byref(self._overlapped),
        )
        if result:
            return bytes(self._packet.raw[: self._received.value])
        error = ctypes.get_last_error()
        if error != ERROR_IO_PENDING:
            raise WinDivertUnavailable(
                f"Windows IPv6 receive failed ({error}): {ctypes.WinError(error)}"
            )
        self._pending = True
        return None

    def _finish_receive(self, handle: object) -> bytes | None:
        status = self._kernel32.WaitForSingleObject(self._event, self.poll_milliseconds)
        if status == WAIT_TIMEOUT:
            return None
        if status != WAIT_OBJECT_0:
            raise WinDivertUnavailable(f"Windows IPv6 receive wait failed ({status})")
        transferred = ctypes.c_uint32(0)
        result = self._kernel32.GetOverlappedResult(
            handle, ctypes.byref(self._overlapped), ctypes.byref(transferred), False
        )
        self._pending = False
        if not result:
            error = ctypes.get_last_error()
            if error == ERROR_OPERATION_ABORTED:
                return None
            raise WinDivertUnavailable(
                f"Windows IPv6 receive completion failed ({error}): {ctypes.WinError(error)}"
            )
        size = int(self._received.value or transferred.value)
        if not 0 < size <= len(self._packet):
            self.counters["invalid_capture_lengths"] += 1
            return None
        return bytes(self._packet.raw[:size])

    def next_frame(self):
        item = self.handles.get("windivert")
        if item is None:
            raise WinDivertUnavailable("Windows IPv6 capture handle is closed")
        handle = item[0].handle
        packet = self._finish_receive(handle) if self._pending else self._start_receive(handle)
        if packet is None:
            return None
        addresses = _packet_addresses(packet)
        if addresses is None:
            self.counters["windivert_invalid_packets"] += 1
            return None
        source, destination = addresses
        self.local_addresses.add(source if self._address.outbound else destination)
        self.counters["windivert_frames_received"] += 1
        self.counters["windivert_bytes_received"] += len(packet)
        return CaptureFrame(
            packet,
            self.clock_ns(),
            "windivert",
            DLT_RAW,
            len(packet),
            RECEIVE_CLOCK_RESOLUTION_NS,
        )

    def statistics(self, _read_stats=None):
        item = self.handles.get("windivert")
        major = ctypes.c_uint64(0)
        minor = ctypes.c_uint64(0)
        if item is not None:
            self.dll.WinDivertGetParam(
                item[0].handle, WINDIVERT_PARAM_VERSION_MAJOR, ctypes.byref(major)
            )
            self.dll.WinDivertGetParam(
                item[0].handle, WINDIVERT_PARAM_VERSION_MINOR, ctypes.byref(minor)
            )
        return {
            "available": item is not None,
            "interfaces": {
                "windivert": {
                    "available": item is not None,
                    "capture_mode": "sniff_receive_only",
                    "ip_versions": sorted(self.versions),
                    "driver_version": f"{major.value}.{minor.value}",
                    "kernel_drop_visibility": False,
                }
            },
            "dropped": 0,
            "kernel_drop_visibility": False,
            "timestamp_source": "userspace_receive_clock",
            "capture_source": "windivert_receive_only",
            "interface_open_errors": dict(self.errors),
        }

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for capture, _datalink, _expression in list(self.handles.values()):
            self._close_capture_handle(capture)
        self.handles.clear()
        if getattr(self, "_event", None):
            self._kernel32.CloseHandle(self._event)
            self._event = None
        self.local_addresses.clear()


def probe_windivert(
    *, dll=None, bundle_directory: Path | None = None
) -> dict[str, object]:
    """Open a receive-only match-nothing handle and close it immediately."""
    library = dll or load_windivert(bundle_directory)
    flags = WINDIVERT_FLAG_SNIFF | WINDIVERT_FLAG_RECV_ONLY
    handle = library.WinDivertOpen(
        b"false", WINDIVERT_LAYER_NETWORK, 0, flags
    )
    error = ctypes.get_last_error()
    if (
        (not handle or int(handle) == INVALID_HANDLE_VALUE)
        and error == ERROR_FILE_NOT_FOUND
        and _remove_stale_windivert_service()
    ):
        handle = library.WinDivertOpen(
            b"false", WINDIVERT_LAYER_NETWORK, 0, flags
        )
        error = ctypes.get_last_error()
    if not handle or int(handle) == INVALID_HANDLE_VALUE:
        raise WinDivertUnavailable(
            f"WinDivert preflight failed ({error}): {ctypes.WinError(error)}"
        )
    try:
        major = ctypes.c_uint64(0)
        minor = ctypes.c_uint64(0)
        library.WinDivertGetParam(handle, WINDIVERT_PARAM_VERSION_MAJOR, ctypes.byref(major))
        library.WinDivertGetParam(handle, WINDIVERT_PARAM_VERSION_MINOR, ctypes.byref(minor))
        return {
            "available": True,
            "capture_mode": "sniff_receive_only",
            "driver_version": f"{major.value}.{minor.value}",
        }
    finally:
        library.WinDivertClose(handle)
