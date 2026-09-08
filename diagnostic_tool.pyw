#!/usr/bin/env python3
"""Standalone GUI for collecting an anonymous DPS capture diagnostic."""

from __future__ import annotations

import ctypes
import hashlib
import multiprocessing
import os
import queue
import secrets
import sys
import threading
import time
from pathlib import Path

from dpi_support import (
    configure_tk_dpi_scaling,
    enable_windows_dpi_awareness,
    get_window_dpi,
    tk_font_spec,
)

# Initialize DPI awareness before Tk creates the first HWND.  Otherwise
# Windows bitmap-scales this standalone tool on 125% displays as well.
enable_windows_dpi_awareness()

import tkinter as tk

from capture_process import CaptureProcessClient, TEAM_STATS_MODE_UNKNOWN
from device_identity import resolve_client_id
from diagnostic_report import (
    DiagnosticAnalyzer,
    DiagnosticUploadError,
    TOOL_NAME,
    TOOL_VERSION,
    diagnostic_environment,
    submit_diagnostic_report,
)
from licensing import DEFAULT_SERVER_URL
from monster_metadata import load_monster_metadata
from proc_inspect import (
    PROCESSENTRY32W,
    TH32CS_SNAPPROCESS,
    find_pid,
    kernel32,
    snapshot,
)
from runtime_capability import create_development_capability


BUNDLE_DIR = Path(__file__).resolve().parent
IS_PACKAGED = bool(
    getattr(sys, "frozen", False)
    or "__compiled__" in globals()
    or (bool(sys.argv) and Path(str(sys.argv[0])).suffix.casefold() == ".exe")
)
PROGRAM_PATH = Path(
    str(
        getattr(globals().get("__compiled__"), "original_argv0", "")
        or (sys.argv[0] if sys.argv else __file__)
    )
).resolve()
SHARED_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", PROGRAM_PATH.parent)) / "GMZZDpsMeter"
DEVICE_ID_PATH = SHARED_DATA_DIR / "device_id"
SHARED_CONFIG_PATH = SHARED_DATA_DIR / "dps_config.json"
MONSTER_METADATA_PATH = BUNDLE_DIR / "monster_metadata.json"
BOSS_ALLOWLIST_PATH = BUNDLE_DIR / "boss_allowlist.txt"
CA_BUNDLE_PATH = BUNDLE_DIR / "cacert.pem"
APP_ICON_PATH = BUNDLE_DIR / "assets" / "app_icon.ico"
RUNTIME_PROFILE_PATH = BUNDLE_DIR / "runtime-profile.json"
CAPTURE_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 20.0
CAPTURE_SHUTDOWN_TIMEOUT_SECONDS = 8.0

BG = "#0b0d10"
SURFACE = "#111419"
PANEL = "#171b20"
BORDER = "#2c333c"
TEXT = "#f2f5f7"
MUTED = "#8f9ba8"
ACCENT = "#62d6b2"
WARN = "#e5b765"
ERROR = "#ea6a72"


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def load_boss_allowlist(path: Path) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def running_dps_pid() -> int:
    current_pid = os.getpid()
    process_snapshot = snapshot(TH32CS_SNAPPROCESS)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Process32FirstW(
            process_snapshot, ctypes.byref(entry)
        )
        while available:
            name = str(entry.szExeFile or "").casefold()
            if entry.th32ProcessID != current_pid and "dps-logs" in name:
                return int(entry.th32ProcessID)
            available = kernel32.Process32NextW(
                process_snapshot, ctypes.byref(entry)
            )
    finally:
        kernel32.CloseHandle(process_snapshot)
    return 0


class DiagnosticWindow:
    def __init__(self):
        self.elevated = is_elevated()
        self.running = False
        self.uploading = False
        self.close_requested = False
        self.capture_client: CaptureProcessClient | None = None
        self.stop_requested = threading.Event()
        self.ui_messages: queue.Queue = queue.Queue()
        self.pending_report: dict[str, object] | None = None
        self.client_id = resolve_client_id(
            {}, DEVICE_ID_PATH, SHARED_CONFIG_PATH
        )

        self.root = tk.Tk()
        self.root.title(TOOL_NAME)
        self.root.configure(bg=BG)
        self.root.geometry("560x536")
        self.root.resizable(False, False)
        self.root.update_idletasks()
        self.window_dpi = configure_tk_dpi_scaling(self.root)
        if APP_ICON_PATH.is_file():
            try:
                self.root.iconbitmap(str(APP_ICON_PATH))
            except tk.TclError:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.status_var = tk.StringVar(value="正在检查运行环境")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.report_id_var = tk.StringVar(value="")
        self.step_labels: dict[str, tuple[tk.Label, tk.Label]] = {}
        self._build_ui()
        self._set_step(
            "admin",
            "通过" if self.elevated else "需要管理员权限",
            "ok" if self.elevated else "error",
        )
        self._configure_primary_for_environment()
        self.root.after(0, self._refresh_window_dpi)
        self.root.after(80, self._drain_ui_messages)
        self.root.after(150, self._poll_game)

    def _refresh_window_dpi(self) -> None:
        if not self.root.winfo_exists():
            return
        try:
            dpi = get_window_dpi(self.root)
            if dpi != getattr(self, "window_dpi", 0):
                self.window_dpi = dpi
                configure_tk_dpi_scaling(self.root)
                self.root.update_idletasks()
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            pass
        try:
            self.root.after(250, self._refresh_window_dpi)
        except tk.TclError:
            return

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=SURFACE, height=78)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text=TOOL_NAME,
            bg=SURFACE,
            fg=TEXT,
            font=tk_font_spec("Microsoft YaHei UI", 17, "bold"),
            anchor="w",
        ).pack(fill="x", padx=28, pady=(15, 0))
        tk.Label(
            header,
            text=f"v{TOOL_VERSION.split('+', 1)[0]}  ·  匿名诊断",
            bg=SURFACE,
            fg=MUTED,
            font=tk_font_spec("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", padx=29, pady=(2, 12))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=20)

        status_row = tk.Frame(body, bg=BG, height=32)
        status_row.pack(fill="x")
        status_row.pack_propagate(False)
        self.status_dot = tk.Canvas(
            status_row, bg=BG, width=18, height=18, highlightthickness=0
        )
        self.status_dot.pack(side="left", pady=6)
        self.status_dot_item = self.status_dot.create_oval(
            4, 4, 14, 14, fill=MUTED, outline=""
        )
        tk.Label(
            status_row,
            textvariable=self.status_var,
            bg=BG,
            fg=TEXT,
            font=tk_font_spec("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.progress = tk.Canvas(
            body, bg=PANEL, height=5, highlightthickness=0
        )
        self.progress.pack(fill="x", pady=(7, 20))
        self.progress.bind("<Configure>", lambda _event: self._draw_progress())

        steps = tk.Frame(body, bg=BG)
        steps.pack(fill="x")
        for key, title in (
            ("admin", "管理员权限"),
            ("game", "游戏进程"),
            ("network", "网络采集"),
            ("damage", "伤害采集"),
            ("boss", "Boss 识别"),
            ("upload", "报告上传"),
        ):
            row = tk.Frame(steps, bg=BG, height=36)
            row.pack(fill="x")
            row.pack_propagate(False)
            title_label = tk.Label(
                row,
                text=title,
                bg=BG,
                fg=TEXT,
                font=tk_font_spec("Microsoft YaHei UI", 10),
                anchor="w",
            )
            title_label.pack(side="left", fill="both", expand=True)
            value_label = tk.Label(
                row,
                text="等待",
                bg=BG,
                fg=MUTED,
                font=tk_font_spec("Microsoft YaHei UI", 10),
                anchor="e",
            )
            value_label.pack(side="right")
            self.step_labels[key] = (title_label, value_label)

        separator = tk.Frame(body, bg=BORDER, height=1)
        separator.pack(fill="x", pady=(12, 16))

        self.report_row = tk.Frame(body, bg=BG, height=40)
        self.report_label = tk.Label(
            self.report_row,
            text="诊断编号",
            bg=BG,
            fg=MUTED,
            font=tk_font_spec("Microsoft YaHei UI", 9),
        )
        self.report_label.pack(side="left")
        self.report_entry = tk.Entry(
            self.report_row,
            textvariable=self.report_id_var,
            state="readonly",
            readonlybackground=PANEL,
            fg=TEXT,
            relief="flat",
            font=tk_font_spec("Consolas", 11, "bold"),
            justify="center",
        )
        self.report_entry.pack(side="left", fill="x", expand=True, padx=12, ipady=7)
        self.copy_button = tk.Button(
            self.report_row,
            text="复制",
            command=self._copy_report_id,
            bg=PANEL,
            fg=TEXT,
            activebackground=BORDER,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=14,
            pady=7,
            cursor="hand2",
            font=tk_font_spec("Microsoft YaHei UI", 9),
        )
        self.copy_button.pack(side="right")

        self.primary_button = tk.Button(
            body,
            text="开始检测",
            command=self._primary_action,
            bg=ACCENT,
            fg="#07110e",
            activebackground="#82e7c8",
            activeforeground="#07110e",
            disabledforeground="#6f7882",
            relief="flat",
            bd=0,
            pady=11,
            cursor="hand2",
            font=tk_font_spec("Microsoft YaHei UI", 10, "bold"),
        )
        self.primary_button.pack(fill="x", side="bottom")

    def _draw_progress(self) -> None:
        self.progress.delete("all")
        width = max(1, self.progress.winfo_width())
        ratio = max(0.0, min(1.0, float(self.progress_var.get())))
        self.progress.create_rectangle(0, 0, width, 5, fill=PANEL, outline="")
        if ratio:
            self.progress.create_rectangle(
                0, 0, int(width * ratio), 5, fill=ACCENT, outline=""
            )

    def _set_progress(self, ratio: float) -> None:
        self.progress_var.set(max(0.0, min(1.0, ratio)))
        self._draw_progress()

    def _set_status(self, text: str, state: str = "normal") -> None:
        colors = {"normal": MUTED, "running": ACCENT, "warn": WARN, "error": ERROR}
        self.status_var.set(text)
        self.status_dot.itemconfigure(
            self.status_dot_item, fill=colors.get(state, MUTED)
        )

    def _set_step(self, key: str, value: str, state: str = "normal") -> None:
        labels = self.step_labels.get(key)
        if labels is None:
            return
        colors = {"normal": MUTED, "ok": ACCENT, "warn": WARN, "error": ERROR}
        labels[1].configure(text=value, fg=colors.get(state, MUTED))

    def _set_primary(self, text: str, command, *, enabled: bool = True) -> None:
        self.primary_button.configure(
            text=text,
            command=command,
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
            bg=ACCENT if enabled else PANEL,
        )

    def _configure_primary_for_environment(self) -> None:
        if not self.elevated:
            self._set_status("需要管理员权限", "error")
            self._set_primary("以管理员身份重启", self._restart_elevated)
            return
        self._set_primary("开始检测", self._start_detection, enabled=False)

    def _game_pid(self) -> int:
        try:
            return int(find_pid("C7-Win64-Shipping.exe"))
        except (OSError, RuntimeError):
            return 0

    def _poll_game(self) -> None:
        if not self.root.winfo_exists():
            return
        if (
            not self.running
            and self.elevated
            and not self.uploading
            and self.pending_report is None
            and not self.report_id_var.get()
        ):
            pid = self._game_pid()
            try:
                dps_pid = running_dps_pid()
            except OSError:
                dps_pid = 0
            if pid:
                self._set_step("game", f"已找到 · PID {pid}", "ok")
                if dps_pid:
                    self._set_step("network", f"DPS 正在运行 · PID {dps_pid}", "warn")
                    self._set_status("请先关闭 Dps-Logs 主程序", "warn")
                    self._set_primary(
                        "等待关闭 Dps-Logs", self._start_detection, enabled=False
                    )
                else:
                    self._set_step("network", "等待", "normal")
                    self._set_status("进入木桩场景，准备好后开始检测", "normal")
                    self._set_primary("开始检测", self._start_detection)
            else:
                self._set_step("game", "未找到", "warn")
                self._set_status("请先启动游戏", "warn")
                self._set_primary("开始检测", self._start_detection, enabled=False)
        self.root.after(1000, self._poll_game)

    def _restart_elevated(self) -> None:
        if sys.platform != "win32":
            return
        if IS_PACKAGED:
            executable = str(PROGRAM_PATH)
            parameters = ""
        else:
            executable = sys.executable
            parameters = f'"{Path(__file__).resolve()}"'
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            executable,
            parameters,
            str(PROGRAM_PATH.parent),
            1,
        )
        if int(result) > 32:
            self.root.destroy()
        else:
            self._set_status("管理员模式启动失败", "error")

    def _primary_action(self) -> None:
        if self.running:
            self._finish_early()
        elif self.pending_report is not None:
            self._retry_upload()
        else:
            self._start_detection()

    def _start_detection(self) -> None:
        try:
            dps_pid = running_dps_pid()
        except OSError:
            dps_pid = 0
        if (
            self.running
            or self.uploading
            or not self.elevated
            or not self._game_pid()
            or dps_pid
        ):
            return
        self.running = True
        self.stop_requested.clear()
        self.pending_report = None
        self.report_id_var.set("")
        if self.report_row.winfo_manager():
            self.report_row.pack_forget()
        self._set_progress(0.0)
        self._set_status("正在连接游戏", "running")
        self._set_step("network", "连接中", "normal")
        self._set_step("damage", "等待", "normal")
        self._set_step("boss", "等待", "normal")
        self._set_step("upload", "等待", "normal")
        self._set_primary("取消检测", self._finish_early)
        threading.Thread(
            target=self._capture_and_upload,
            name="GMZZDiagnosticController",
            daemon=True,
        ).start()

    def _finish_early(self) -> None:
        if not self.running:
            return
        self.stop_requested.set()
        client = self.capture_client
        if client is not None:
            client.request_stop()
        self._set_status("正在安全结束并上传", "running")
        self._set_primary("正在结束", self._finish_early, enabled=False)

    def _post(self, kind: str, payload: object = None) -> None:
        self.ui_messages.put((kind, payload))

    def _capture_and_upload(self) -> None:
        analyzer = DiagnosticAnalyzer(
            load_monster_metadata(MONSTER_METADATA_PATH),
            load_boss_allowlist(BOSS_ALLOWLIST_PATH),
        )
        capture: CaptureProcessClient | None = None
        connected_at: float | None = None
        started_at = time.monotonic()
        stop_sent = False
        stopped_message = False
        process_exit_seen_at: float | None = None
        try:
            capability = create_development_capability(
                RUNTIME_PROFILE_PATH,
                session_id=secrets.token_hex(16),
                client_id=self.client_id,
                build_id=hashlib.sha256(
                    f"GMZZ-Diagnostic/{TOOL_VERSION}".encode("utf-8")
                ).hexdigest()[:32],
                client_build=f"diagnostic-{TOOL_VERSION}",
                lease_seconds=(
                    CAPTURE_SECONDS
                    + CONNECT_TIMEOUT_SECONDS
                    + CAPTURE_SHUTDOWN_TIMEOUT_SECONDS
                    + 60.0
                ),
            )
            # Exercise every current target identity route while keeping the
            # unrelated active team-stat request disabled.
            capture = CaptureProcessClient(
                runtime_capability=capability,
                allow_development=True,
                parent_pid=os.getpid(),
                target_boss_lookup_enabled=True,
                team_stats_mode=TEAM_STATS_MODE_UNKNOWN,
            )
            self.capture_client = capture
            capture.start()
            while True:
                now = time.monotonic()
                if connected_at is not None:
                    elapsed = max(0.0, now - connected_at)
                    remaining = max(0.0, CAPTURE_SECONDS - elapsed)
                    self._post(
                        "tick",
                        {
                            "ratio": min(1.0, elapsed / CAPTURE_SECONDS),
                            "remaining": int(remaining + 0.999),
                            "network_records": analyzer.network_records,
                            "damage_records": (
                                analyzer.network_damage_positive
                                + analyzer.native_positive_damage_records
                            ),
                            "boss_records": analyzer.native_boss_records,
                            "boss_events": (
                                analyzer._confirmed_boss_events()
                                + analyzer._damage_dummy_events()
                            ),
                            "lookup_candidates": int(
                                analyzer.native_diagnostic.get(
                                    "target_lookup_candidates", 0
                                )
                                or 0
                            ),
                            "lookup_resolved": int(
                                analyzer.native_diagnostic.get(
                                    "target_lookup_resolved", 0
                                )
                                or 0
                            ),
                        },
                    )
                    if elapsed >= CAPTURE_SECONDS:
                        self.stop_requested.set()
                elif now - started_at >= CONNECT_TIMEOUT_SECONDS:
                    self.stop_requested.set()

                if self.stop_requested.is_set() and not stop_sent:
                    capture.request_stop()
                    stop_sent = True
                    self._post("cleanup")

                try:
                    kind, payload = capture.get(timeout=0.1)
                except queue.Empty:
                    if capture.is_alive():
                        continue
                    if process_exit_seen_at is None:
                        process_exit_seen_at = now
                        continue
                    if now - process_exit_seen_at < 0.3:
                        continue
                    break
                process_exit_seen_at = None
                analyzer.handle(kind, payload)
                self._post("capture_event", (kind, payload))
                if kind == "connected" and connected_at is None:
                    connected_at = time.monotonic()
                elif kind == "stopped":
                    stopped_message = True
                if stopped_message and not capture.is_alive():
                    break
        except BaseException as exc:
            analyzer.handle("fatal", exc)
            self._post("capture_event", ("fatal", str(exc)))
        finally:
            if capture is not None and capture.is_alive():
                capture.request_stop()
                self._post("cleanup")
                deadline = time.monotonic() + CAPTURE_SHUTDOWN_TIMEOUT_SECONDS
                while capture.is_alive() and time.monotonic() < deadline:
                    remaining = max(
                        0.05,
                        min(0.2, deadline - time.monotonic()),
                    )
                    try:
                        kind, payload = capture.get(timeout=remaining)
                    except queue.Empty:
                        continue
                    analyzer.handle(kind, payload)
                    self._post("capture_event", (kind, payload))
                if capture.is_alive():
                    # This is the isolated diagnostic child, never the game
                    # process. A hard stop keeps report generation finite if a
                    # native cleanup call is stuck in an old client build.
                    capture.terminate()
                    capture.join(2.0)
                    analyzer.handle(
                        "cleanup_error",
                        {"details": "diagnostic capture child shutdown timeout"},
                    )
            elif capture is not None:
                capture.join(0)
            if capture is not None:
                try:
                    capture.close(wait_for_queue=False)
                except Exception as exc:
                    # Cleanup must never prevent an already collected report
                    # from reaching the upload step.
                    analyzer.handle("cleanup_error", {"details": str(exc)})
            self.capture_client = None

        try:
            report = analyzer.finish(
                diagnostic_environment(elevated=self.elevated, packaged=IS_PACKAGED)
            )
        except Exception as exc:
            self._post(
                "upload_failed",
                f"报告生成失败：{type(exc).__name__}: {exc}",
            )
            return
        self.pending_report = report
        self.uploading = True
        self._post("uploading", report["assessment"])
        self._upload_report(report)

    def _upload_report(self, report: dict[str, object]) -> None:
        try:
            submission = submit_diagnostic_report(
                DEFAULT_SERVER_URL,
                self.client_id,
                report,
                ca_bundle_path=CA_BUNDLE_PATH,
            )
        except DiagnosticUploadError as exc:
            self._post("upload_failed", str(exc))
            return
        except Exception:
            self._post("upload_failed", "上传失败，请检查网络后重试。")
            return
        self.pending_report = None
        self._post(
            "completed",
            {
                "diagnostic_id": submission.diagnostic_id,
                "message": submission.message,
                "assessment": report.get("assessment", {}),
            },
        )

    def _retry_upload(self) -> None:
        if self.uploading or self.pending_report is None:
            return
        report = self.pending_report
        self.uploading = True
        self._set_step("upload", "上传中", "normal")
        self._set_status("正在重新上传报告", "running")
        self._set_primary("正在上传", self._retry_upload, enabled=False)

        def retry() -> None:
            self._upload_report(report)

        threading.Thread(target=retry, name="GMZZDiagnosticUpload", daemon=True).start()

    def _handle_capture_event(self, kind: str, payload: object) -> None:
        if kind == "connected" and isinstance(payload, dict):
            self._set_step("network", "已连接", "ok")
            source = str(payload.get("damage_source", ""))
            self._set_step(
                "damage",
                "检测中 · 原生" if source == "native" else "检测中 · 网络",
                "normal",
            )
            template_hooks = sum(
                bool(payload.get(key))
                for key in (
                    "native_boss_type_hook_installed",
                    "native_boss_init_hook_installed",
                    "native_template_id_hook_installed",
                    "native_template_bulk_hook_installed",
                )
            )
            self._set_step(
                "boss",
                f"识别入口 {template_hooks}/4",
                "ok" if template_hooks == 4 else "warn",
            )
            self._set_status("请持续攻击同一个 Boss 或伤害木桩，不要切换目标", "running")
            self._set_primary("结束并上传", self._finish_early)
        elif kind == "capture_error":
            self._set_step("network", "连接异常", "error")
        elif kind == "fatal":
            self._set_step("network", "检测异常", "error")

    def _drain_ui_messages(self) -> None:
        while True:
            try:
                kind, payload = self.ui_messages.get_nowait()
            except queue.Empty:
                break
            if kind == "capture_event" and isinstance(payload, tuple):
                self._handle_capture_event(payload[0], payload[1])
            elif kind == "tick" and isinstance(payload, dict):
                self._set_progress(float(payload.get("ratio", 0.0)))
                remaining = int(payload.get("remaining", 0))
                records = int(payload.get("network_records", 0))
                damage = int(payload.get("damage_records", 0))
                self._set_step("network", f"消息 {records}", "ok" if records else "normal")
                self._set_step(
                    "damage", f"伤害 {damage}", "ok" if damage else "normal"
                )
                boss_records = int(payload.get("boss_records", 0))
                boss_events = int(payload.get("boss_events", 0))
                lookup_candidates = int(payload.get("lookup_candidates", 0))
                lookup_resolved = int(payload.get("lookup_resolved", 0))
                if boss_events > 0:
                    boss_text, boss_state = "已识别", "ok"
                elif lookup_resolved > 0:
                    boss_text, boss_state = "已读取 · 核对中", "warn"
                elif lookup_candidates > 0:
                    boss_text, boss_state = f"目标反查 {lookup_candidates}", "normal"
                elif boss_records > 0:
                    boss_text, boss_state = f"模板记录 {boss_records}", "normal"
                else:
                    boss_text, boss_state = "等待目标", "normal"
                self._set_step("boss", boss_text, boss_state)
                self._set_status(
                    f"持续攻击同一个 Boss 或伤害木桩 · 剩余 {remaining} 秒",
                    "running",
                )
            elif kind == "cleanup":
                self._set_status("正在安全恢复游戏采集函数", "running")
                self._set_primary("正在结束", self._finish_early, enabled=False)
            elif kind == "uploading":
                self.running = False
                self.uploading = True
                self._set_progress(1.0)
                self._set_step("upload", "上传中", "normal")
                self._set_status("正在上传脱敏报告", "running")
            elif kind == "upload_failed":
                self.running = False
                self.uploading = False
                self._set_step("upload", "失败", "error")
                self._set_status(str(payload or "上传失败"), "error")
                self._set_primary("重新上传", self._retry_upload)
                if self.close_requested:
                    self.root.destroy()
                    return
            elif kind == "completed" and isinstance(payload, dict):
                self.running = False
                self.uploading = False
                diagnostic_id = str(payload.get("diagnostic_id", ""))
                assessment = payload.get("assessment", {})
                if not isinstance(assessment, dict):
                    assessment = {}
                code = str(assessment.get("code", ""))
                summary = str(assessment.get("summary", "检测完成"))
                damage_state = (
                    "ok"
                    if code in {"capture_pipeline_ok", "damage_dummy_pipeline_ok"}
                    else "warn"
                )
                if code in {
                    "native_hook_silent_network_fallback_blocked",
                    "network_hook_silent",
                    "network_hook_failed",
                }:
                    damage_state = "error"
                self._set_step("damage", "已分析", damage_state)
                self._set_step(
                    "boss",
                    "识别正常"
                    if code in {"capture_pipeline_ok", "damage_dummy_pipeline_ok"}
                    else "未识别 · 已定位",
                    "ok"
                    if code in {"capture_pipeline_ok", "damage_dummy_pipeline_ok"}
                    else "warn",
                )
                self._set_step("upload", "已完成", "ok")
                self.report_id_var.set(diagnostic_id)
                if not self.report_row.winfo_manager():
                    self.report_row.pack(fill="x", before=self.primary_button, pady=(0, 14))
                self._set_status(summary, "normal" if damage_state == "ok" else damage_state)
                self._set_primary("再次检测", self._start_detection)
                if self.close_requested:
                    self.root.destroy()
                    return
        if self.root.winfo_exists():
            self.root.after(80, self._drain_ui_messages)

    def _copy_report_id(self) -> None:
        value = self.report_id_var.get().strip()
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.copy_button.configure(text="已复制")
        self.root.after(1200, lambda: self.copy_button.configure(text="复制"))

    def _close(self) -> None:
        if self.running:
            self.close_requested = True
            self._finish_early()
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    multiprocessing.freeze_support()
    DiagnosticWindow().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
