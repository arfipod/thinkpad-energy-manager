from __future__ import annotations

import fcntl
import os
import queue
import signal
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TextIO

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.events import EventDetector
from battery_auditor.core.models import Event, SystemSnapshot
from battery_auditor.core.runtime import (
    control_path,
    lock_path,
    read_control_state,
    remove_heartbeat,
    write_control_state,
    write_heartbeat,
    write_lock_payload,
)
from battery_auditor.core.sleep_monitor import (
    RESUME_SAMPLE_TAKEN,
    RESUMED,
    SLEEP_MONITOR_UNAVAILABLE,
    SleepMonitor,
    SleepMonitorEvent,
    build_sleep_monitor,
)
from battery_auditor.core.sysfs import (
    SystemLoadCounters,
    read_process_metrics,
    read_snapshot,
    read_system_load_metrics,
)
from battery_auditor.core.thresholds import (
    STATUS_MISMATCH,
    THRESHOLD_MISMATCH,
    CommandRunner,
    restore_thresholds,
    status_for_snapshot,
)


@dataclass(slots=True)
class CollectorRunResult:
    session_id: str
    samples: int
    reason: str


class BatteryCollector:
    def __init__(
        self,
        cfg: AuditorConfig,
        db: BatteryDatabase | None = None,
        *,
        sleep_monitor_factory: Callable[[Callable[[SleepMonitorEvent], None]], SleepMonitor] | None = None,
        threshold_restore_runner: CommandRunner | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db or BatteryDatabase(cfg.resolved_db_path(), cfg)
        self.stop_requested = False
        self.detector = EventDetector(cfg)
        self._lock_file: TextIO | None = None
        self._control_mtime_ns: int | None = None
        self._pause_requested = False
        self._previous_system_load: SystemLoadCounters | None = None
        self._sleep_monitor_factory = sleep_monitor_factory
        self._sleep_monitor: SleepMonitor | None = None
        self._sleep_events: queue.Queue[SleepMonitorEvent] = queue.Queue()
        self._wake_event = threading.Event()
        self._threshold_restore_runner = threshold_restore_runner

    def request_stop(self, *_args: object) -> None:
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def recover(self) -> list[str]:
        self.db.init_schema()
        return self.db.recover_open_sessions()

    def run(
        self,
        name: str | None = None,
        interval_seconds: float | None = None,
        duration_seconds: float | None = None,
        blackbox: bool = False,
        recover_open_sessions: bool = True,
    ) -> CollectorRunResult:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.heartbeat_dir().mkdir(parents=True, exist_ok=True)
        self._acquire_collector_lock()
        try:
            write_control_state(self.cfg, paused=False)
            self.db.init_schema()
            integrity = self.db.check_integrity(quick=True)
            if integrity != ["ok"]:
                raise RuntimeError(f"Refusing to collect into a damaged database: {'; '.join(integrity)}")
            if recover_open_sessions:
                self.db.recover_open_sessions()

            interval = float(interval_seconds or self.cfg.interval_seconds)
            self.cfg.interval_seconds = interval
            session_id = self._new_session_id()
            self.db.start_session(session_id=session_id, name=name, cfg_json=self.cfg.to_json())
            self.install_signal_handlers()
            self._start_sleep_monitor(session_id)

            reason = "stopped"
            seq = 0
            paused = False
            started_mono = time.monotonic()
            next_deadline = started_mono
            last_heartbeat_wall = 0.0
            try:
                while not self.stop_requested:
                    resume_requested, sleep_event_seen = self._drain_sleep_monitor_events(session_id)
                    if sleep_event_seen and (blackbox or self.cfg.blackbox_flush_each_sample):
                        self.db.flush_to_disk()
                    self._wake_event.clear()
                    now_mono = time.monotonic()
                    if duration_seconds is not None and now_mono - started_mono >= duration_seconds:
                        reason = "duration_elapsed"
                        break
                    pause_requested = self._read_pause_requested()
                    if pause_requested != paused:
                        paused = pause_requested
                        self._record_pause_transition(session_id, paused)
                        if blackbox or self.cfg.blackbox_flush_each_sample:
                            self.db.flush_to_disk()
                        if not paused:
                            self.detector = EventDetector(self.cfg)
                            next_deadline = time.monotonic()
                    if paused:
                        heartbeat_interval = max(0.5, float(self.cfg.heartbeat_seconds))
                        now_wall = time.time()
                        if now_wall - last_heartbeat_wall >= heartbeat_interval:
                            self.db.update_heartbeat(
                                session_id,
                                now_wall,
                                self._wall_iso(now_wall),
                                time.monotonic(),
                            )
                            self._write_heartbeat_file(
                                session_id,
                                seq - 1,
                                sample_count=seq,
                                paused=True,
                                wall_time=now_wall,
                                monotonic_time=time.monotonic(),
                            )
                            last_heartbeat_wall = now_wall
                            if blackbox or self.cfg.blackbox_flush_each_sample:
                                self.db.flush_to_disk()
                        self._wait(min(1.0, heartbeat_interval))
                        continue
                    if resume_requested:
                        next_deadline = now_mono
                    if now_mono < next_deadline:
                        self._wait(min(0.25, next_deadline - now_mono))
                        continue

                    loop_delay_ms = max(0.0, (now_mono - next_deadline) * 1000.0) if seq > 0 else 0.0
                    snap = self._sample(loop_delay_ms=loop_delay_ms)
                    events = self.detector.process(snap)
                    restore_targets = self._threshold_restore_targets(snap, events, resume_requested=resume_requested)
                    if restore_targets:
                        events.extend(self._restore_threshold_events(restore_targets))
                    if resume_requested:
                        events.append(
                            Event(
                                RESUME_SAMPLE_TAKEN,
                                "info",
                                "Immediate battery sample taken after resume.",
                                wall_time=snap.wall_time,
                                monotonic_time=snap.monotonic_time,
                                details={"source": "sleep_monitor"},
                            )
                        )
                    self.db.insert_snapshot(session_id, seq, snap, events)
                    self.db.update_heartbeat(session_id, snap.wall_time, snap.wall_iso, snap.monotonic_time)
                    self._write_heartbeat_file(
                        session_id,
                        seq,
                        sample_count=seq + 1,
                        paused=False,
                        snap=snap,
                    )
                    last_heartbeat_wall = snap.wall_time

                    if blackbox or self.cfg.blackbox_flush_each_sample:
                        self.db.flush_to_disk()

                    seq += 1
                    next_deadline += interval
                    if next_deadline < time.monotonic() - interval:
                        # If the process was paused or the system slept, avoid a catch-up storm.
                        next_deadline = time.monotonic() + interval
            except Exception as exc:  # noqa: BLE001 - top-level recorder must persist the error
                reason = f"error:{type(exc).__name__}"
                with suppress(Exception):
                    self.db.insert_event(
                        session_id,
                        Event(
                            "COLLECTOR_ERROR",
                            "critical",
                            f"Collector stopped because of an error: {exc}",
                            details={"exception_type": type(exc).__name__, "exception": str(exc)},
                        ),
                    )
                with suppress(Exception):
                    self.db.flush_to_disk()
                raise
            finally:
                if self.stop_requested and reason == "stopped":
                    reason = "signal_or_user_stop"
                with suppress(Exception):
                    self.db.end_session(session_id, reason=reason)
                self._remove_heartbeat_file(session_id)
                with suppress(Exception):
                    write_control_state(self.cfg, paused=False)
                with suppress(Exception):
                    self._stop_sleep_monitor()
                if blackbox or self.cfg.blackbox_flush_each_sample:
                    with suppress(Exception):
                        self.db.flush_to_disk()
            return CollectorRunResult(session_id=session_id, samples=seq, reason=reason)
        finally:
            self._release_collector_lock()

    def _sample(self, loop_delay_ms: float) -> SystemSnapshot:
        snap = read_snapshot(self.cfg.sysfs_power_supply_dir)
        rss, user_cpu, system_cpu = read_process_metrics()
        system_metrics, self._previous_system_load = read_system_load_metrics(self._previous_system_load)
        snap.metrics.collector_rss_kib = rss
        snap.metrics.collector_user_cpu_seconds = user_cpu
        snap.metrics.collector_system_cpu_seconds = system_cpu
        snap.metrics.loop_delay_ms = loop_delay_ms
        snap.metrics.system_cpu_percent = _float_or_none(system_metrics["system_cpu_percent"])
        snap.metrics.system_load_1m = _float_or_none(system_metrics["system_load_1m"])
        snap.metrics.system_memory_total_kib = _int_or_none(system_metrics["system_memory_total_kib"])
        snap.metrics.system_memory_available_kib = _int_or_none(system_metrics["system_memory_available_kib"])
        snap.metrics.system_memory_used_percent = _float_or_none(system_metrics["system_memory_used_percent"])
        snap.metrics.system_disk_read_bytes_per_second = _float_or_none(
            system_metrics["system_disk_read_bytes_per_second"]
        )
        snap.metrics.system_disk_write_bytes_per_second = _float_or_none(
            system_metrics["system_disk_write_bytes_per_second"]
        )
        snap.metrics.display_brightness_percent = _float_or_none(system_metrics["display_brightness_percent"])
        snap.metrics.display_brightness_raw = _int_or_none(system_metrics["display_brightness_raw"])
        snap.metrics.display_brightness_max = _int_or_none(system_metrics["display_brightness_max"])
        snap.metrics.wifi_enabled = _bool_or_none(system_metrics["wifi_enabled"])
        snap.metrics.bluetooth_enabled = _bool_or_none(system_metrics["bluetooth_enabled"])
        return snap

    def _threshold_restore_targets(
        self,
        snap: SystemSnapshot,
        events: list[Event],
        *,
        resume_requested: bool,
    ) -> list[str]:
        targets: set[str] = set()
        if self.cfg.threshold_restore_on_mismatch:
            targets.update(
                event.battery_name
                for event in events
                if event.event_type == THRESHOLD_MISMATCH and event.battery_name is not None
            )
        if self.cfg.threshold_restore_on_resume and resume_requested:
            targets.update(self._mismatched_threshold_batteries(snap))
        return sorted(targets)

    def _mismatched_threshold_batteries(self, snap: SystemSnapshot) -> list[str]:
        batteries: list[str] = []
        for battery in snap.batteries:
            expected = self.cfg.expected_thresholds.get(battery.name)
            actual_start = battery.charge_control_start_threshold
            actual_stop = battery.charge_control_end_threshold
            if actual_start is None:
                actual_start = battery.charge_start_threshold
            if actual_stop is None:
                actual_stop = battery.charge_stop_threshold
            status = status_for_snapshot(battery.name, expected, actual_start, actual_stop)
            if status == STATUS_MISMATCH:
                batteries.append(battery.name)
        return batteries

    def _restore_threshold_events(self, batteries: list[str]) -> list[Event]:
        results = restore_thresholds(
            self.cfg,
            batteries=batteries,
            runner=self._threshold_restore_runner,
        )
        return [result.to_event() for result in results]

    def _start_sleep_monitor(self, session_id: str) -> None:
        if self._sleep_monitor_factory is not None:
            monitor = self._sleep_monitor_factory(self._enqueue_sleep_monitor_event)
        else:
            monitor = build_sleep_monitor(
                enabled=self.cfg.sleep_monitor_enabled,
                backend=self.cfg.sleep_monitor_backend,
                callback=self._enqueue_sleep_monitor_event,
            )
        self._sleep_monitor = monitor
        unavailable = monitor.start()
        if unavailable is not None:
            now = time.time()
            self.db.insert_event(
                session_id,
                Event(
                    SLEEP_MONITOR_UNAVAILABLE,
                    "warning",
                    "Sleep monitor is unavailable; falling back to wall/monotonic gap classification.",
                    wall_time=now,
                    monotonic_time=time.monotonic(),
                    details={"backend": unavailable.backend, "reason": unavailable.reason},
                ),
            )

    def _stop_sleep_monitor(self) -> None:
        if self._sleep_monitor is None:
            return
        self._sleep_monitor.stop()
        self._sleep_monitor = None

    def _enqueue_sleep_monitor_event(self, event: SleepMonitorEvent) -> None:
        self._sleep_events.put(event)
        self._wake_event.set()

    def _drain_sleep_monitor_events(self, session_id: str) -> tuple[bool, bool]:
        resume_seen = False
        event_seen = False
        while True:
            try:
                event = self._sleep_events.get_nowait()
            except queue.Empty:
                break
            event_seen = True
            self.db.insert_event(
                session_id,
                Event(
                    event.event_type,
                    "info",
                    "System is about to sleep." if event.event_type != RESUMED else "System resumed from sleep.",
                    wall_time=event.wall_time,
                    monotonic_time=event.monotonic_time,
                    details={"backend": event.backend},
                ),
            )
            if event.event_type == RESUMED:
                resume_seen = True
        return resume_seen, event_seen

    def _wait(self, seconds: float) -> None:
        self._wake_event.wait(timeout=max(0.0, seconds))

    def _new_session_id(self) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{timestamp}-{uuid.uuid4().hex[:8]}"

    def _write_heartbeat_file(
        self,
        session_id: str,
        last_seq: int,
        *,
        sample_count: int,
        paused: bool,
        snap: SystemSnapshot | None = None,
        wall_time: float | None = None,
        monotonic_time: float | None = None,
    ) -> None:
        if snap is not None:
            wall_time = snap.wall_time
            monotonic_time = snap.monotonic_time
            extra = {
                "ac_online": snap.ac_online,
                "total_computed_percent": snap.total_computed_percent,
                "total_energy_now_uwh": snap.total_energy_now_uwh,
            }
        else:
            extra = {}
        write_heartbeat(
            self.cfg,
            session_id=session_id,
            pid=os.getpid(),
            paused=paused,
            sample_count=sample_count,
            last_seq=last_seq,
            wall_time=wall_time if wall_time is not None else time.time(),
            monotonic_time=monotonic_time if monotonic_time is not None else time.monotonic(),
            extra=extra,
        )

    def _remove_heartbeat_file(self, session_id: str) -> None:
        remove_heartbeat(self.cfg, session_id)

    def _read_pause_requested(self) -> bool:
        path = control_path(self.cfg)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._control_mtime_ns = None
            self._pause_requested = False
            return False
        except OSError:
            return self._pause_requested
        if self._control_mtime_ns == mtime_ns:
            return self._pause_requested
        self._control_mtime_ns = mtime_ns
        state = read_control_state(self.cfg)
        self._pause_requested = state.paused if state.parse_error is None else self._pause_requested
        return self._pause_requested

    def _record_pause_transition(self, session_id: str, paused: bool) -> None:
        now = time.time()
        self.db.insert_event(
            session_id,
            Event(
                "SESSION_PAUSED" if paused else "SESSION_RESUMED",
                "info",
                "Collector paused by user request." if paused else "Collector resumed by user request.",
                wall_time=now,
                monotonic_time=time.monotonic(),
            ),
        )

    @staticmethod
    def _wall_iso(timestamp: float) -> str:
        from battery_auditor.core.models import wall_iso_from_timestamp

        return wall_iso_from_timestamp(timestamp)

    def _acquire_collector_lock(self) -> None:
        path = lock_path(self.cfg)
        lock_file = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeError(f"Another Battery Auditor collector is already running: {path}") from exc
        write_lock_payload(lock_file)
        self._lock_file = lock_file

    def _release_collector_lock(self) -> None:
        if self._lock_file is None:
            return
        lock_file = self._lock_file
        self._lock_file = None
        with suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            lock_file.close()
        with suppress(OSError):
            lock_path(self.cfg).unlink()


def _float_or_none(value: float | int | bool | None) -> float | None:
    return None if value is None else float(value)


def _int_or_none(value: float | int | bool | None) -> int | None:
    return None if value is None else int(value)


def _bool_or_none(value: float | int | bool | None) -> bool | None:
    return None if value is None else bool(value)
