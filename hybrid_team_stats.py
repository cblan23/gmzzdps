"""Optional Npcap + minimal team-stat Hook coordination.

The request driver schedules the game's existing zero-argument
``ReqCommonCombatStatisticsByTeam`` call.  The receive driver observes only the
two decoded team-stat callbacks.  Npcap remains responsible for every other
network message; no battle IDs, statistics, or protocol payloads are created
here.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Mapping


HYBRID_TEAM_STATS_CONFIG_KEY = "team_stats_hybrid_hook_enabled"
HYBRID_TEAM_STATS_INTERVAL_CONFIG_KEY = "team_stats_hybrid_interval_seconds"
DEFAULT_HYBRID_TEAM_STATS_INTERVAL_SECONDS = 1.0
MIN_HYBRID_TEAM_STATS_INTERVAL_SECONDS = 0.75
MAX_HYBRID_TEAM_STATS_INTERVAL_SECONDS = 5.0
QUERY_TRIGGER_TIMEOUT_SECONDS = 5.0
TEAM_STATISTICS_METHODS = frozenset(
    {
        "RetCommonCombatStatisticsByTeam",
        "RetDirtyCommonCombatStatisticsByTeam",
    }
)
LOCAL_ROLE_SCRIPT_METHODS = frozenset(
    {
        "RetNTP",
        "OnUpdateTeamGroupSelfProps",
        "OnMsgUpdateStageCombatStatistics",
        "OnMsgSettlementCombatStatistics",
        *TEAM_STATISTICS_METHODS,
    }
)
RECEIVE_POLL_IDLE_SECONDS = 0.005
RECEIVE_RETRY_SECONDS = 2.0


def _configured_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
    if value is None:
        return bool(default)
    return bool(value)


def hybrid_team_stats_settings(config: object) -> tuple[bool, float]:
    """Return the local hybrid-Hook setting and bounded cadence."""

    values = config if isinstance(config, dict) else {}
    enabled = _configured_bool(values.get(HYBRID_TEAM_STATS_CONFIG_KEY), True)
    try:
        interval = float(
            values.get(
                HYBRID_TEAM_STATS_INTERVAL_CONFIG_KEY,
                DEFAULT_HYBRID_TEAM_STATS_INTERVAL_SECONDS,
            )
        )
    except (TypeError, ValueError, OverflowError):
        interval = DEFAULT_HYBRID_TEAM_STATS_INTERVAL_SECONDS
    interval = min(
        MAX_HYBRID_TEAM_STATS_INTERVAL_SECONDS,
        max(MIN_HYBRID_TEAM_STATS_INTERVAL_SECONDS, interval),
    )
    return enabled, interval


class HybridTeamStatsReceiveDriver:
    """Capture decoded team-stat callbacks without duplicating Npcap events."""

    def __init__(
        self,
        profile: Mapping[str, object],
        emit: Callable[[dict[str, object]], None],
        *,
        hook_factory=None,
    ) -> None:
        self.profile = profile
        self.emit = emit
        self.hook_factory = hook_factory
        self.hook = None
        self.game_pid = 0
        self.next_install_at = 0.0
        self.local_stop: threading.Event | None = None
        self.poller: threading.Thread | None = None
        self.records: queue.SimpleQueue = queue.SimpleQueue()
        self.errors: queue.SimpleQueue = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.response_count = 0
        self.decoded_response_count = 0
        self.decode_error_count = 0
        self.last_response_filetime = 0
        self.last_method = ""
        self.expected_sequence: int | None = None
        self.sequence_gap_count = 0
        self.local_role_script_entity = 0
        self.local_role_evidence_method = ""

    def _event(self, event: str, **fields: object) -> None:
        self.emit(
            {
                "event": event,
                "event_time_ns": time.time_ns(),
                "hybrid_receive_hook": True,
                **fields,
            }
        )

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text = str(error).strip().splitlines()[0] if str(error).strip() else ""
        return f"{type(error).__name__}: {text[:240]}"

    def _new_hook(self, game_pid: int):
        factory = self.hook_factory
        if factory is None:
            from network_capture import NetworkMessageHook

            factory = NetworkMessageHook
        return factory(
            profile=self.profile,
            pid=int(game_pid),
            takeover_existing=True,
        )

    @staticmethod
    def _decode_team_method(method: str) -> bool:
        return method in TEAM_STATISTICS_METHODS

    def _run_poller(self, hook, local_stop: threading.Event) -> None:
        try:
            while not local_stop.is_set() and bool(getattr(hook, "alive", True)):
                captured = hook.poll(
                    decode_arguments=True,
                    decode_method_filter=self._decode_team_method,
                )
                retained = []
                for record in captured:
                    try:
                        sequence = int(record.get("sequence", -1))
                    except (TypeError, ValueError, OverflowError):
                        sequence = -1
                    if sequence >= 0:
                        if (
                            self.expected_sequence is not None
                            and sequence != self.expected_sequence
                        ):
                            with self.lock:
                                self.sequence_gap_count += 1
                        self.expected_sequence = sequence + 1
                    method = str(record.get("method", ""))
                    if method in LOCAL_ROLE_SCRIPT_METHODS:
                        try:
                            script_entity = int(
                                record.get("script_entity", 0) or 0
                            )
                        except (TypeError, ValueError, OverflowError):
                            script_entity = 0
                        if 0x1_0000 <= script_entity < 0x0000_8000_0000_0000:
                            with self.lock:
                                self.local_role_script_entity = script_entity
                                self.local_role_evidence_method = method
                    if method not in TEAM_STATISTICS_METHODS:
                        continue
                    item = dict(record)
                    item["capture_source"] = "memory_receive_hook"
                    item["hybrid_receive_hook"] = True
                    retained.append(item)
                for record in retained:
                    self.records.put(record)
                local_stop.wait(
                    0.001 if captured else RECEIVE_POLL_IDLE_SECONDS
                )
        except BaseException as error:
            self.errors.put(self._safe_error(error))

    def _install(self, game_pid: int) -> bool:
        now = time.monotonic()
        if now < self.next_install_at:
            return False
        self.next_install_at = now + RECEIVE_RETRY_SECONDS
        self._event("hybrid_receive_installing", game_pid=int(game_pid))
        try:
            hook = self._new_hook(game_pid).install()
            local_stop = threading.Event()
            poller = threading.Thread(
                target=self._run_poller,
                args=(hook, local_stop),
                name="C7TeamStatsReceiveAck",
                daemon=False,
            )
            self.hook = hook
            self.local_stop = local_stop
            self.poller = poller
            self.game_pid = int(game_pid)
            self.expected_sequence = None
            poller.start()
            self._event(
                "hybrid_receive_installed",
                game_pid=int(game_pid),
                adopted=bool(getattr(hook, "adopted", False)),
            )
            return True
        except BaseException as error:
            self.hook = None
            self.local_stop = None
            self.poller = None
            self._event(
                "hybrid_receive_install_error",
                game_pid=int(game_pid),
                failure="TEAM_RECEIVE_HOOK_UNAVAILABLE",
                details=self._safe_error(error),
            )
            return False

    def sync(self, *, game_pid: int) -> None:
        next_pid = max(0, int(game_pid or 0))
        if self.hook is not None and self.game_pid != next_pid:
            self._close_hook(reason="game_process_changed")
            self.next_install_at = 0.0
            with self.lock:
                self.local_role_script_entity = 0
                self.local_role_evidence_method = ""
        self.game_pid = next_pid
        if next_pid and self.hook is None:
            self._install(next_pid)

    def _take_error(self) -> str:
        try:
            return str(self.errors.get_nowait())
        except queue.Empty:
            return ""

    def poll(self) -> list[dict[str, object]]:
        error = self._take_error()
        if error:
            self._event(
                "hybrid_receive_runtime_error",
                game_pid=self.game_pid,
                failure="TEAM_RECEIVE_POLL_FAILED",
                details=error,
            )
            self._close_hook(reason="receive_poller_failed")
            self.next_install_at = 0.0
            return []

        result: list[dict[str, object]] = []
        while True:
            try:
                result.append(self.records.get_nowait())
            except queue.Empty:
                break
        for record in result:
            method = str(record.get("method", ""))
            decoded = "decoded_arguments" in record and not record.get(
                "decode_error"
            )
            try:
                filetime_100ns = max(
                    0, int(record.get("filetime_100ns", 0) or 0)
                )
            except (TypeError, ValueError, OverflowError):
                filetime_100ns = 0
            with self.lock:
                self.response_count += 1
                if decoded:
                    self.decoded_response_count += 1
                else:
                    self.decode_error_count += 1
                self.last_response_filetime = filetime_100ns
                self.last_method = method
            self._event(
                "hybrid_receive_response",
                game_pid=self.game_pid,
                method=method,
                filetime_100ns=filetime_100ns,
                decoded=decoded,
                decode_error=str(record.get("decode_error", ""))[:240],
            )
        return result

    def status_snapshot(self) -> dict[str, object]:
        poller = self.poller
        hook = self.hook
        with self.lock:
            return {
                "installed": bool(hook is not None),
                "adopted": bool(getattr(hook, "adopted", False)),
                "poller_alive": bool(poller is not None and poller.is_alive()),
                "response_count": int(self.response_count),
                "decoded_response_count": int(self.decoded_response_count),
                "decode_error_count": int(self.decode_error_count),
                "last_response_filetime": int(self.last_response_filetime),
                "last_method": self.last_method,
                "sequence_gap_count": int(self.sequence_gap_count),
                "local_role_script_entity": int(
                    self.local_role_script_entity
                ),
                "local_role_evidence_method": (
                    self.local_role_evidence_method
                ),
            }

    def _close_hook(self, *, reason: str) -> None:
        hook, self.hook = self.hook, None
        local_stop, self.local_stop = self.local_stop, None
        poller, self.poller = self.poller, None
        if local_stop is not None:
            local_stop.set()
        if poller is not None and poller.is_alive():
            poller.join(1.0)
        if hook is None:
            return
        try:
            hook.close()
            self._event(
                "hybrid_receive_uninstalled",
                reason=str(reason or "unknown"),
            )
        except BaseException as error:
            self._event(
                "hybrid_receive_cleanup_error",
                reason=str(reason or "unknown"),
                failure="TEAM_RECEIVE_HOOK_CLEANUP_FAILED",
                details=self._safe_error(error),
            )

    def close(self) -> None:
        self._close_hook(reason="driver_close")


class HybridTeamStatsHookDriver:
    """Manage one stable request Hook while Npcap consumes the responses."""

    def __init__(
        self,
        profile: Mapping[str, object],
        emit: Callable[[dict[str, object]], None],
        *,
        interval: float = DEFAULT_HYBRID_TEAM_STATS_INTERVAL_SECONDS,
        hook_factory=None,
    ) -> None:
        self.profile = profile
        self.emit = emit
        self.interval = min(
            MAX_HYBRID_TEAM_STATS_INTERVAL_SECONDS,
            max(MIN_HYBRID_TEAM_STATS_INTERVAL_SECONDS, float(interval)),
        )
        self.hook_factory = hook_factory
        self.hook = None
        self.game_pid = 0
        self.mode = "unknown"
        self.enabled = False
        self.script_entity_override = 0
        self.next_install_at = 0.0
        self.next_status_at = 0.0
        self.cached_status: dict[str, object] = self._empty_status()
        self.last_reported_request_count = 0
        self.active_attempt: dict[str, object] | None = None
        self.attempted_encounters: set[str] = set()
        self.attempt_deadline = 0.0

    def _empty_status(self) -> dict[str, object]:
        return {
            "installed": False,
            "enabled": False,
            "adopted": False,
            "request_count": 0,
            "last_result": 0,
            "last_request_filetime": 0,
            "captured_request_count": 0,
            "dropped_request_count": 0,
            "response_health": "inactive",
            "hybrid_npcap_hook": True,
            "interval_seconds": float(self.interval),
            "script_entity_override": int(self.script_entity_override),
            "last_script_entity": 0,
        }

    def _event(self, event: str, **fields: object) -> None:
        self.emit(
            {
                "event": event,
                "event_time_ns": time.time_ns(),
                "hybrid_npcap_hook": True,
                **fields,
            }
        )

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text = str(error).strip().splitlines()[0] if str(error).strip() else ""
        return f"{type(error).__name__}: {text[:240]}"

    def _new_hook(self, game_pid: int):
        factory = self.hook_factory
        if factory is None:
            from team_stats_request_hook import TeamStatsRequestHook

            factory = TeamStatsRequestHook
        return factory(
            profile=self.profile,
            pid=int(game_pid),
            interval=self.interval,
            enabled=False,
            takeover_existing=True,
            stable_primary_only=True,
        )

    def _close_hook(self, *, reason: str) -> bool:
        hook = self.hook
        if hook is None:
            self.enabled = False
            self.cached_status = self._empty_status()
            self.last_reported_request_count = 0
            return True
        try:
            hook.set_enabled(False)
            hook.close()
        except BaseException as error:
            self._event(
                "hybrid_cleanup_error",
                reason=str(reason or "unknown"),
                failure="TEAM_HOOK_CLEANUP_FAILED",
                details=self._safe_error(error),
            )
            return False
        self.hook = None
        self.enabled = False
        self.cached_status = self._empty_status()
        self.last_reported_request_count = 0
        self._event("hybrid_uninstalled", reason=str(reason or "unknown"))
        return True

    def pause_for_external_request(self) -> bool:
        """Release call_server while one bounded equipment request runs."""

        if self.active_attempt is not None:
            return False
        if not self._close_hook(reason="external_request"):
            return False
        self.next_install_at = 0.0
        return True

    def resume_after_external_request(self) -> None:
        self.next_install_at = 0.0

    def _install(self, game_pid: int, script_entity_override: int) -> bool:
        now = time.monotonic()
        if now < self.next_install_at:
            return False
        self.next_install_at = now + 2.0
        self._event("hybrid_installing", game_pid=int(game_pid))
        try:
            hook = self._new_hook(game_pid).install()
            hook.set_script_entity_override(script_entity_override)
            hook.set_enabled(True)
            self.hook = hook
            self.game_pid = int(game_pid)
            self.script_entity_override = int(script_entity_override)
            self.enabled = True
            self.cached_status = self._read_status()
            self.last_reported_request_count = int(
                self.cached_status.get("request_count", 0) or 0
            )
            self._event(
                "hybrid_installed",
                game_pid=int(game_pid),
                adopted=bool(getattr(hook, "adopted", False)),
                interval_seconds=self.interval,
                script_entity_override=int(script_entity_override),
                baseline_request_count=self.last_reported_request_count,
            )
            return True
        except BaseException as error:
            self.hook = None
            self.enabled = False
            self.cached_status = self._empty_status()
            self._event(
                "hybrid_install_error",
                game_pid=int(game_pid),
                failure="TEAM_HOOK_UNAVAILABLE",
                details=self._safe_error(error),
            )
            return False

    def sync(
        self,
        *,
        game_pid: int,
        mode: object,
        script_entity_override: object = 0,
    ) -> None:
        """Apply the latest process and conservative scene classification."""

        next_pid = max(0, int(game_pid or 0))
        next_mode = str(mode or "unknown").strip().casefold()
        if next_mode not in {"unknown", "dummy", "team"}:
            next_mode = "unknown"
        try:
            next_script_entity = max(0, int(script_entity_override or 0))
        except (TypeError, ValueError, OverflowError):
            next_script_entity = 0
        if self.hook is not None and self.game_pid != next_pid:
            if not self._close_hook(reason="game_process_changed"):
                return
            self.next_install_at = 0.0
        self.game_pid = next_pid
        previous_mode = self.mode
        self.mode = next_mode
        should_enable = bool(
            next_pid and next_mode == "team" and next_script_entity
        )
        if not should_enable:
            if self.hook is not None and self.enabled:
                try:
                    self.hook.set_enabled(False)
                    self.enabled = False
                    self._event(
                        "hybrid_disabled",
                        game_pid=next_pid,
                        mode=next_mode,
                    )
                except BaseException as error:
                    self._event(
                        "hybrid_control_error",
                        failure="TEAM_HOOK_DISABLE_FAILED",
                        details=self._safe_error(error),
                    )
                    self._close_hook(reason="disable_failed")
            return
        if self.hook is None and not self._install(
            next_pid, next_script_entity
        ):
            return
        if (
            self.hook is not None
            and self.script_entity_override != next_script_entity
        ):
            try:
                self.hook.set_script_entity_override(next_script_entity)
                self.script_entity_override = next_script_entity
                self._event(
                    "hybrid_script_entity_updated",
                    game_pid=next_pid,
                    script_entity_override=next_script_entity,
                )
            except BaseException as error:
                self._event(
                    "hybrid_control_error",
                    failure="TEAM_HOOK_ENTITY_UPDATE_FAILED",
                    details=self._safe_error(error),
                )
                self._close_hook(reason="entity_update_failed")
                return
        if self.hook is not None and not self.enabled:
            try:
                self.hook.set_enabled(True)
                if previous_mode != "team":
                    self.hook.arm_one_shot()
                self.enabled = True
                self._event("hybrid_enabled", game_pid=next_pid, mode=next_mode)
            except BaseException as error:
                self._event(
                    "hybrid_control_error",
                    failure="TEAM_HOOK_ENABLE_FAILED",
                    details=self._safe_error(error),
                )
                self._close_hook(reason="enable_failed")

    def _read_status(self) -> dict[str, object]:
        hook = self.hook
        if hook is None:
            return self._empty_status()
        status = dict(hook.status())
        status.update(
            {
                "installed": True,
                "enabled": bool(status.get("enabled", self.enabled)),
                "adopted": bool(getattr(hook, "adopted", False)),
                "response_health": (
                    "waiting_response" if self.enabled else "inactive"
                ),
                "hybrid_npcap_hook": True,
                "interval_seconds": float(self.interval),
                "script_entity_override": int(
                    status.get(
                        "script_entity_override",
                        self.script_entity_override,
                    )
                    or 0
                ),
                "last_script_entity": int(
                    status.get("last_script_entity", 0) or 0
                ),
            }
        )
        return status

    def status_snapshot(self) -> dict[str, object]:
        return dict(self.cached_status)

    def arm(self, command: object, *, game_pid: int) -> bool:
        """Make the next ordinary game-thread RPC edge query once immediately."""

        if not isinstance(command, dict):
            return False
        encounter_id = str(command.get("local_encounter_id", "") or "")
        if not encounter_id or encounter_id in self.attempted_encounters:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="DUPLICATE_ATTEMPT",
            )
            return False
        self.attempted_encounters.add(encounter_id)
        if self.active_attempt is not None:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="QUERY_BUSY",
            )
            return False
        if not game_pid or int(game_pid) != self.game_pid or self.hook is None:
            self._event(
                "rejected",
                local_encounter_id=encounter_id,
                failure="GAME_NOT_CONNECTED",
            )
            return False
        self._event(
            "installing",
            local_encounter_id=encounter_id,
            game_pid=int(game_pid),
            reused_persistent_hook=True,
        )
        try:
            baseline = int(self.hook.arm_one_shot())
            self.enabled = True
            self.active_attempt = dict(command)
            self.active_attempt["baseline_request_count"] = baseline
            self.attempt_deadline = time.monotonic() + QUERY_TRIGGER_TIMEOUT_SECONDS
            self._event(
                "armed",
                local_encounter_id=encounter_id,
                baseline_request_count=baseline,
                trigger_timeout_seconds=QUERY_TRIGGER_TIMEOUT_SECONDS,
                reused_persistent_hook=True,
            )
            return True
        except BaseException as error:
            self.active_attempt = None
            self.attempt_deadline = 0.0
            self._event(
                "error",
                local_encounter_id=encounter_id,
                failure="QUERY_HOOK_UNAVAILABLE",
                details=self._safe_error(error),
            )
            return False

    def poll(self, *, force: bool = False) -> None:
        hook = self.hook
        if hook is None:
            return
        now = time.monotonic()
        if not force and now < self.next_status_at:
            return
        self.next_status_at = now + 0.1
        try:
            attached = getattr(hook, "is_attached", None)
            if callable(attached) and not attached():
                raise RuntimeError("team-stat request entry detached")
            status = self._read_status()
            self.cached_status = status
        except BaseException as error:
            self._event(
                "hybrid_runtime_error",
                failure="TEAM_HOOK_STATUS_UNAVAILABLE",
                details=self._safe_error(error),
            )
            self._close_hook(reason="runtime_status_failed")
            self.next_install_at = 0.0
            return

        request_count = int(status.get("request_count", 0) or 0)
        if request_count > self.last_reported_request_count:
            self._event(
                "hybrid_request_progress",
                game_pid=self.game_pid,
                request_count=request_count,
                request_delta=request_count - self.last_reported_request_count,
                last_result=int(status.get("last_result", 0) or 0),
                last_request_filetime=int(
                    status.get("last_request_filetime", 0) or 0
                ),
                script_entity_override=int(
                    status.get("script_entity_override", 0) or 0
                ),
                original_script_entity=int(
                    status.get("last_script_entity", 0) or 0
                ),
            )
            self.last_reported_request_count = request_count

        attempt = self.active_attempt
        if attempt is None:
            return
        baseline = int(attempt.get("baseline_request_count", 0) or 0)
        encounter_id = str(attempt.get("local_encounter_id", "") or "")
        if request_count > baseline:
            observed_ns = time.time_ns()
            self.active_attempt = None
            self.attempt_deadline = 0.0
            self._event(
                "triggered",
                local_encounter_id=encounter_id,
                request_trigger_time_ns=observed_ns,
                request_return_observed_time_ns=observed_ns,
                request_count_before=baseline,
                request_count_after=request_count,
                request_filetime_100ns=int(
                    status.get("last_request_filetime", 0) or 0
                ),
                request_call_result=int(status.get("last_result", 0) or 0),
                request_method="ReqCommonCombatStatisticsByTeam",
                reused_persistent_hook=True,
            )
            return
        if now < self.attempt_deadline:
            return
        self.active_attempt = None
        self.attempt_deadline = 0.0
        self._event(
            "timeout",
            local_encounter_id=encounter_id,
            failure="REQUEST_NOT_TRIGGERED",
            reused_persistent_hook=True,
        )

    def close(self) -> None:
        self.active_attempt = None
        self.attempt_deadline = 0.0
        self._close_hook(reason="driver_close")
