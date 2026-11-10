"""Keep Python and native failure evidence outside the portable EXE directory."""
from __future__ import annotations

import datetime as dt
import faulthandler
import os
from pathlib import Path
import threading
import traceback


_fault_log = None
_log_lock = threading.Lock()


def write_runtime_diagnostic(message: str, *, exception=None) -> None:
    handle = _fault_log
    if handle is None:
        return
    try:
        details = ""
        if exception is not None:
            details = "".join(traceback.format_exception(*exception))[-12000:]
        with _log_lock:
            handle.write(
                f"\n[{dt.datetime.now().astimezone().isoformat()}] "
                f"pid={os.getpid()} {message}\n{details}"
            )
            handle.flush()
    except (OSError, ValueError):
        pass


def enable_runtime_diagnostics(
    component: str, *, directory: Path | None = None, client_build: str = ""
) -> Path | None:
    """Retain the fault-handler file for the entire process lifetime."""
    global _fault_log
    if _fault_log is not None:
        return Path(_fault_log.name)
    if directory is None:
        directory = Path(os.environ.get("LOCALAPPDATA", Path.cwd())) / "GMZZDpsMeter" / "logs"
    handle = None
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"dps_fault_{component}.log"
        handle = path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        if handle is not None:
            handle.close()
        return None
    _fault_log = handle
    previous_hook = threading.excepthook

    def thread_exception(args):
        write_runtime_diagnostic(
            f"unhandled_thread_exception thread={getattr(args.thread, 'name', 'unknown')}",
            exception=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_hook(args)

    threading.excepthook = thread_exception
    write_runtime_diagnostic(f"process_started component={component} build={client_build}")
    return path
