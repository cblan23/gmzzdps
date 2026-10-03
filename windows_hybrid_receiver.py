"""Adaptive no-Npcap Windows receiver for IPv4 and IPv6 game traffic."""
from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Iterable

from windivert_receiver import WinDivertReceiver, endpoint_needs_ipv6
from windows_raw_receiver import (
    RawSocketUnavailable,
    WindowsRawSocketReceiver,
    _capture_ipv4_address,
)


def _can_use_ipv4_raw(endpoint: object) -> bool:
    return _capture_ipv4_address(getattr(endpoint, "local_address", "")) is not None


class WindowsHybridReceiver:
    """Use Raw Socket for IPv4 and bundled receive-only WinDivert for IPv6.

    A dual-stack ``::`` endpoint is intentionally sent to both sources.  This
    avoids guessing whether the game selected IPv4 or native IPv6.  If Raw
    Socket is blocked, WinDivert covers both versions without loading Npcap.
    """

    def __init__(
        self,
        endpoints: Iterable[object],
        *,
        raw_factory=WindowsRawSocketReceiver,
        windivert_factory=WinDivertReceiver,
    ):
        self.raw_factory = raw_factory
        self.windivert_factory = windivert_factory
        self.raw = None
        self.windivert = None
        self.endpoints = list(endpoints)
        self.handles = {}
        self.local_addresses: set[str] = set()
        self.counters: Counter[str] = Counter()
        self.errors: dict[str, str] = {}
        self.cursor = 0
        self.closed = False
        try:
            self.refresh(self.endpoints)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _sets(endpoints: list[object]):
        ipv4 = [row for row in endpoints if _can_use_ipv4_raw(row)]
        ipv6 = [row for row in endpoints if endpoint_needs_ipv6(row)]
        return ipv4, ipv6

    def _sync_public_state(self) -> None:
        handles = {}
        addresses: set[str] = set()
        for prefix, receiver in (("raw", self.raw), ("windivert", self.windivert)):
            if receiver is None:
                continue
            handles.update(
                {f"{prefix}:{name}": value for name, value in receiver.handles.items()}
            )
            addresses.update(receiver.local_addresses)
        self.handles = handles
        self.local_addresses = addresses

    def _close_child(self, name: str) -> None:
        receiver = getattr(self, name)
        if receiver is not None:
            receiver.close()
            setattr(self, name, None)

    def refresh(self, endpoints: Iterable[object]) -> None:
        endpoints = list(endpoints)
        ipv4, ipv6 = self._sets(endpoints)
        raw_failed = False
        if ipv4:
            try:
                if self.raw is None:
                    self.raw = self.raw_factory(ipv4, poll_seconds=0.003)
                else:
                    self.raw.refresh(ipv4)
                self.errors.pop("raw", None)
            except (OSError, RuntimeError) as exc:
                self.errors["raw"] = f"{type(exc).__name__}: {exc}"
                self._close_child("raw")
                raw_failed = True
        else:
            self._close_child("raw")

        windivert_endpoints = list(ipv6)
        versions = {6} if ipv6 else set()
        if raw_failed:
            windivert_endpoints.extend(
                row for row in ipv4 if row not in windivert_endpoints
            )
            versions.add(4)
        if windivert_endpoints:
            if self.windivert is not None and self.windivert.versions != frozenset(versions):
                self._close_child("windivert")
            try:
                if self.windivert is None:
                    self.windivert = self.windivert_factory(
                        windivert_endpoints, versions=versions, poll_seconds=0.003
                    )
                else:
                    self.windivert.refresh(windivert_endpoints)
                self.errors.pop("windivert", None)
            except (OSError, RuntimeError) as exc:
                self.errors["windivert"] = f"{type(exc).__name__}: {exc}"
                self._close_child("windivert")
                raise RawSocketUnavailable(
                    "Windows 免 Npcap 采集无法覆盖当前游戏连接：" + str(exc)
                ) from exc
        else:
            self._close_child("windivert")

        if self.raw is None and self.windivert is None:
            details = "; ".join(self.errors.values())
            raise RawSocketUnavailable(
                "Windows 免 Npcap 采集没有可用接收源"
                + (f"：{details}" if details else "")
            )
        self.endpoints = endpoints
        self._sync_public_state()

    def next_frame(self):
        receivers = [row for row in (self.raw, self.windivert) if row is not None]
        if not receivers:
            raise RawSocketUnavailable("All Windows capture sources are closed")
        for offset in range(len(receivers)):
            index = (self.cursor + offset) % len(receivers)
            receiver = receivers[index]
            frame = receiver.next_frame()
            self.counters.update(receiver.counters)
            receiver.counters.clear()
            if frame is not None:
                self.cursor = (index + 1) % len(receivers)
                self._sync_public_state()
                return frame
        self._sync_public_state()
        return None

    def statistics(self, _read_stats=None):
        interfaces = {}
        dropped = 0
        visibility = True
        sources = []
        for prefix, receiver in (("raw", self.raw), ("windivert", self.windivert)):
            if receiver is None:
                continue
            status = receiver.statistics(None)
            sources.append(str(status.get("capture_source", prefix)))
            dropped += int(status.get("dropped", 0) or 0)
            visibility = visibility and bool(status.get("kernel_drop_visibility"))
            interfaces.update(
                {
                    f"{prefix}:{name}": value
                    for name, value in dict(status.get("interfaces", {})).items()
                }
            )
        return {
            "available": bool(interfaces),
            "interfaces": interfaces,
            "dropped": dropped,
            "kernel_drop_visibility": visibility,
            "capture_source": "windows_hybrid_receive_only",
            "active_sources": sources,
            "interface_open_errors": dict(self.errors),
        }

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._close_child("raw")
        self._close_child("windivert")
        self.handles.clear()
        self.local_addresses.clear()
