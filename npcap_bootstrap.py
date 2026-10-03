"""One bounded read-only initialization, then handle-free local state copies.

Never stores key material on disk. Failed alignment does not reopen readers.
New transport connections require a separate explicitly identified bootstrap.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


class PassiveStateError(RuntimeError):
    """The passive source must stop; no memory refresh or Hook fallback."""


@dataclass(frozen=True)
class FrozenSessionState:
    anchor: object
    compression: object
    cryptor_address: int

    def close(self) -> None:
        # No OS handle belongs to this object.
        pass


def bootstrap_copies(pid: int, locate: Callable, coherent: Callable) -> tuple[list[FrozenSessionState], dict]:
    readers, diagnostic = locate(pid)
    copies, failures = [], 0
    cleanup_errors = []
    try:
        for reader in readers:
            try:
                anchor, compression = coherent(reader.rc4, reader.zstd)
                copies.append(FrozenSessionState(anchor, compression, reader.cryptor_address))
            except (OSError, ValueError, RuntimeError):
                failures += 1
    finally:
        for reader in readers:
            try:
                reader.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
    if cleanup_errors:
        raise PassiveStateError('Read-only initialization handle cleanup failed')
    if not copies:
        raise PassiveStateError('No coherent connection state; passive capture cannot decode')
    return copies, {**diagnostic,'bootstrap_candidates':len(copies),'bootstrap_failures':failures,
                    'game_read_handles_closed':True,'post_bootstrap_game_reads':0,
                    'memory_policy':'bounded_initialization_only_no_reopen'}
