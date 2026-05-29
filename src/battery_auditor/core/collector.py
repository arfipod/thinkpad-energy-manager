from __future__ import annotations

import json
import signal
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.events import EventDetector
from battery_auditor.core.models import Event, SystemSnapshot
from battery_auditor.core.sysfs import read_process_metrics, read_snapshot


@dataclass(slots=True)
class CollectorRunResult:
    session_id: str
    samples: int
    reason: str


class BatteryCollector:
    def __init__(self, cfg: AuditorConfig, db: BatteryDatabase | None = None) -> None:
        self.cfg = cfg
        self.db = db or BatteryDatabase(cfg.resolved_db_path(), cfg)
        self.stop_requested = False
        self.detector = EventDetector(cfg)

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
        self.db.init_schema()
        if recover_open_sessions:
            self.db.recover_open_sessions()

        interval = float(interval_seconds or self.cfg.interval_seconds)
        self.cfg.interval_seconds = interval
        session_id = self._new_session_id()
        self.db.start_session(session_id=session_id, name=name, cfg_json=self.cfg.to_json())
        self.install_signal_handlers()

        reason = "stopped"
        seq = 0
        started_mono = time.monotonic()
        next_deadline = started_mono
        try:
            while not self.stop_requested:
                now_mono = time.monotonic()
                if duration_seconds is not None and now_mono - started_mono >= duration_seconds:
                    reason = "duration_elapsed"
                    break
                if now_mono < next_deadline:
                    time.sleep(min(0.25, next_deadline - now_mono))
                    continue

                loop_delay_ms = max(0.0, (now_mono - next_deadline) * 1000.0) if seq > 0 else 0.0
                snap = self._sample(loop_delay_ms=loop_delay_ms)
                events = self.detector.process(snap)
                self.db.insert_snapshot(session_id, seq, snap, events)
                self.db.update_heartbeat(session_id, snap.wall_time, snap.wall_iso, snap.monotonic_time)
                self._write_heartbeat_file(session_id, seq, snap)

                if blackbox or self.cfg.blackbox_flush_each_sample:
                    self.db.flush_to_disk()

                seq += 1
                next_deadline += interval
                if next_deadline < time.monotonic() - interval:
                    # If the process was paused or the system slept, avoid a catch-up storm.
                    next_deadline = time.monotonic() + interval
        except Exception as exc:  # noqa: BLE001 - top-level recorder must persist the error
            reason = f"error:{type(exc).__name__}"
            self.db.insert_event(
                session_id,
                Event(
                    "COLLECTOR_ERROR",
                    "critical",
                    f"Collector stopped because of an error: {exc}",
                    details={"exception_type": type(exc).__name__, "exception": str(exc)},
                ),
            )
            self.db.flush_to_disk()
            raise
        finally:
            if self.stop_requested and reason == "stopped":
                reason = "signal_or_user_stop"
            self.db.end_session(session_id, reason=reason)
            self._remove_heartbeat_file(session_id)
            if blackbox or self.cfg.blackbox_flush_each_sample:
                self.db.flush_to_disk()
        return CollectorRunResult(session_id=session_id, samples=seq, reason=reason)

    def _sample(self, loop_delay_ms: float) -> SystemSnapshot:
        snap = read_snapshot(self.cfg.sysfs_power_supply_dir)
        rss, user_cpu, system_cpu = read_process_metrics()
        snap.metrics.collector_rss_kib = rss
        snap.metrics.collector_user_cpu_seconds = user_cpu
        snap.metrics.collector_system_cpu_seconds = system_cpu
        snap.metrics.loop_delay_ms = loop_delay_ms
        return snap

    def _new_session_id(self) -> str:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{timestamp}-{uuid.uuid4().hex[:8]}"

    def _heartbeat_path(self, session_id: str) -> Path:
        return self.cfg.heartbeat_dir() / f"{session_id}.json"

    def _write_heartbeat_file(self, session_id: str, seq: int, snap: SystemSnapshot) -> None:
        path = self._heartbeat_path(session_id)
        tmp_path = path.with_suffix(".json.tmp")
        payload = {
            "session_id": session_id,
            "seq": seq,
            "wall_time": snap.wall_time,
            "wall_iso": snap.wall_iso,
            "monotonic_time": snap.monotonic_time,
            "ac_online": snap.ac_online,
            "total_computed_percent": snap.total_computed_percent,
            "total_energy_now_uwh": snap.total_energy_now_uwh,
        }
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _remove_heartbeat_file(self, session_id: str) -> None:
        with suppress(FileNotFoundError):
            self._heartbeat_path(session_id).unlink()
