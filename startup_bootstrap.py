"""Windows startup and receive-only capture preflight.

The released client has one capture policy: Windows built-in passive capture
using Raw Socket for IPv4 and the bundled receive-only WinDivert component
when native IPv6 traffic requires it. This module deliberately contains no
driver installer, service maintenance, or process-injection fallback.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from windows_raw_receiver import list_local_ipv4, probe_windows_raw_socket
from windivert_receiver import probe_windivert


class StartupBootstrapError(RuntimeError):
    """A user-actionable prerequisite failure."""

    title = "叨叨诡秘助手无法启动"


def is_process_elevated() -> bool:
    """Return whether the current process can open receive-only raw sockets."""

    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = ctypes.c_int
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def elevation_command(
    *,
    frozen: bool,
    application_path: Path,
    source_path: Path,
    python_executable: Path,
    arguments: Sequence[str],
) -> tuple[Path, tuple[str, ...]]:
    """Build the administrator relaunch command without moving the app."""

    if frozen:
        return Path(application_path), tuple(str(value) for value in arguments)
    return Path(python_executable), (
        str(Path(source_path)),
        *(str(value) for value in arguments),
    )


def relaunch_as_admin(
    *,
    frozen: bool,
    application_path: Path,
    source_path: Path,
    python_executable: Path,
    arguments: Sequence[str],
    working_directory: Path,
) -> None:
    if os.name != "nt":
        raise StartupBootstrapError("叨叨诡秘助手仅支持在 Windows 上以管理员身份运行。")
    executable, command_arguments = elevation_command(
        frozen=frozen,
        application_path=application_path,
        source_path=source_path,
        python_executable=python_executable,
        arguments=arguments,
    )
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        parameters = subprocess.list2cmdline(list(command_arguments))
        result = shell32.ShellExecuteW(
            None,
            "runas",
            str(executable),
            parameters or None,
            str(working_directory),
            1,
        )
        result_code = int(result or 0)
    except (AttributeError, OSError, ValueError) as exc:
        raise StartupBootstrapError(f"无法请求管理员权限：{exc}") from exc
    if result_code <= 32:
        raise StartupBootstrapError(
            "叨叨诡秘助手需要管理员权限进行被动网络采集。"
            "请重新打开程序，并在 Windows 提示中选择“是”。"
        )


def show_startup_message(
    message: str, *, title: str = "叨叨诡秘助手无法启动"
) -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(
            None,
            str(message),
            str(title),
            0x00000010 | 0x00010000,  # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        )
    except (AttributeError, OSError):
        pass


def ensure_passive_capture_ready(
    bundle_directory: Path,
    capture_backend: str = "windows_raw",
    *,
    reporter: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Validate the built-in receive-only source before creating the UI.

    ``bundle_directory`` is kept in the public signature for frozen-build
    callers; the receiver resolves bundled resources itself.
    """

    del bundle_directory
    report = reporter or write_startup_log
    backend = str(capture_backend or "windows_raw").strip().casefold()
    if backend != "windows_raw":
        raise StartupBootstrapError(
            f"不支持的被动采集后端：{capture_backend}。当前版本只使用 Windows 内置采集。"
        )
    if os.name != "nt":
        raise StartupBootstrapError("Windows 内置网络采集需要 Windows 10/11。")
    if not is_process_elevated():
        raise StartupBootstrapError(
            "请以管理员身份运行叨叨诡秘助手，并允许 Windows 提权提示。"
        )

    errors: list[str] = []
    addresses = list(list_local_ipv4())
    ordered = sorted(addresses, key=lambda value: str(value).startswith("127."))
    for address in ordered:
        try:
            result = probe_windows_raw_socket(address)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"{address}: {exc}")
            continue
        report(
            "Built-in capture preflight available=True "
            f"interface={address} mode={result.get('capture_mode', 'raw')}"
        )
        return {
            **result,
            "capture_source": "windows_raw",
            "traffic_validation_pending": True,
        }

    try:
        fallback = probe_windivert()
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"WinDivert: {exc}")
    else:
        report(
            "Raw Socket unavailable; bundled receive-only IPv6 source "
            f"available=True version={fallback.get('driver_version', 'unknown')}"
        )
        return {
            **fallback,
            "capture_source": "windows_raw",
            "fallback": True,
            "fallback_source": "windivert_receive_only",
            "built_in_errors": errors,
            "traffic_validation_pending": True,
        }

    details = "; ".join(errors) or "没有检测到可用的 IPv4 网卡"
    report(f"Built-in capture preflight available=False errors={details}")
    raise StartupBootstrapError(
        "当前电脑的 Windows 内置被动采集不可用。请确认使用 Windows 10/11、"
        "管理员权限，且杀毒软件没有阻止 Raw Socket 或随程序提供的 IPv6 采集组件。\n\n"
        f"检测结果：{details}"
    )


def write_startup_log(message: str) -> None:
    try:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.cwd()))
        log_directory = local_app_data / "GMZZDpsMeter" / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        with (log_directory / "startup-bootstrap.log").open(
            "a", encoding="utf-8"
        ) as output:
            output.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} "
                f"{str(message).replace(chr(10), ' ')}\n"
            )
    except OSError:
        pass
