from __future__ import annotations

import json
import os
import platform
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from battery_auditor.config import AuditorConfig
from battery_auditor.core.models import BatterySnapshot, Event, SystemSnapshot

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    hostname TEXT,
    kernel TEXT,
    started_at_wall REAL NOT NULL,
    started_at_iso TEXT NOT NULL,
    started_at_monotonic REAL NOT NULL,
    ended_at_wall REAL,
    ended_at_iso TEXT,
    ended_reason TEXT,
    probable_power_loss INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_wall REAL,
    last_heartbeat_iso TEXT,
    last_heartbeat_monotonic REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    system_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    wall_time REAL NOT NULL,
    wall_iso TEXT NOT NULL,
    monotonic_time REAL NOT NULL,
    ac_online INTEGER,
    total_energy_now_uwh INTEGER,
    total_energy_full_uwh INTEGER,
    total_energy_full_design_uwh INTEGER,
    total_power_now_uw INTEGER,
    total_computed_percent REAL,
    total_health_percent REAL,
    active_batteries TEXT NOT NULL DEFAULT '[]',
    sample_duration_ms REAL,
    db_write_duration_ms REAL,
    collector_rss_kib INTEGER,
    collector_user_cpu_seconds REAL,
    collector_system_cpu_seconds REAL,
    loop_delay_ms REAL,
    created_at_wall REAL NOT NULL,
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS sample_batteries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    present INTEGER,
    status TEXT,
    capacity_percent REAL,
    computed_percent REAL,
    health_percent REAL,
    capacity_level TEXT,
    energy_now_uwh INTEGER,
    energy_full_uwh INTEGER,
    energy_full_design_uwh INTEGER,
    power_now_uw INTEGER,
    voltage_now_uv INTEGER,
    voltage_min_design_uv INTEGER,
    cycle_count INTEGER,
    technology TEXT,
    manufacturer TEXT,
    model_name TEXT,
    serial_number TEXT,
    charge_control_start_threshold INTEGER,
    charge_control_end_threshold INTEGER,
    charge_start_threshold INTEGER,
    charge_stop_threshold INTEGER,
    charge_behaviour TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(sample_id, name)
);

CREATE TABLE IF NOT EXISTS power_supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT,
    online INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(sample_id, name)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sample_id INTEGER REFERENCES samples(id) ON DELETE SET NULL,
    wall_time REAL,
    monotonic_time REAL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    battery_name TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at_wall REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_session_time ON samples(session_id, wall_time);
CREATE INDEX IF NOT EXISTS idx_sample_batteries_session_name_time ON sample_batteries(session_id, name, sample_id);
CREATE INDEX IF NOT EXISTS idx_events_session_time ON events(session_id, wall_time);
"""


class BatteryDatabase:
    def __init__(self, db_path: Path, cfg: AuditorConfig | None = None) -> None:
        self.db_path = db_path.expanduser()
        self.cfg = cfg or AuditorConfig(db_path=self.db_path)
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA synchronous = {self.cfg.sqlite_synchronous}")
        conn.execute(f"PRAGMA wal_autocheckpoint = {int(self.cfg.sqlite_wal_autocheckpoint_pages)}")
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    def start_session(self, session_id: str, name: str | None, cfg_json: str) -> None:
        from battery_auditor.core.models import wall_iso_from_timestamp

        conn = self.connect()
        now = time.time()
        mono = time.monotonic()
        system = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        }
        conn.execute(
            """
            INSERT INTO sessions (
              id, name, hostname, kernel, started_at_wall, started_at_iso,
              started_at_monotonic, config_json, system_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                name,
                platform.node(),
                platform.release(),
                now,
                wall_iso_from_timestamp(now),
                mono,
                cfg_json,
                json.dumps(system, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()

    def update_heartbeat(self, session_id: str, wall_time: float, wall_iso: str, monotonic_time: float) -> None:
        conn = self.connect()
        conn.execute(
            """
            UPDATE sessions
               SET last_heartbeat_wall = ?, last_heartbeat_iso = ?, last_heartbeat_monotonic = ?
             WHERE id = ?
            """,
            (wall_time, wall_iso, monotonic_time, session_id),
        )
        conn.commit()

    def end_session(self, session_id: str, reason: str = "stopped") -> None:
        from battery_auditor.core.models import wall_iso_from_timestamp

        conn = self.connect()
        now = time.time()
        conn.execute(
            """
            UPDATE sessions
               SET ended_at_wall = ?, ended_at_iso = ?, ended_reason = ?
             WHERE id = ? AND ended_at_wall IS NULL
            """,
            (now, wall_iso_from_timestamp(now), reason, session_id),
        )
        conn.commit()

    def recover_open_sessions(self, reason: str = "interrupted_or_power_loss") -> list[str]:
        from battery_auditor.core.models import wall_iso_from_timestamp

        conn = self.connect()
        rows = conn.execute(
            "SELECT id, last_heartbeat_wall, last_heartbeat_iso, last_heartbeat_monotonic FROM sessions WHERE ended_at_wall IS NULL"
        ).fetchall()
        recovered: list[str] = []
        now = time.time()
        for row in rows:
            session_id = str(row["id"])
            conn.execute(
                """
                UPDATE sessions
                   SET ended_at_wall = COALESCE(last_heartbeat_wall, ?),
                       ended_at_iso = COALESCE(last_heartbeat_iso, ?),
                       ended_reason = ?,
                       probable_power_loss = 1
                 WHERE id = ?
                """,
                (now, wall_iso_from_timestamp(now), reason, session_id),
            )
            conn.execute(
                """
                INSERT INTO events (
                  session_id, sample_id, wall_time, monotonic_time, event_type,
                  severity, battery_name, message, details_json, created_at_wall
                ) VALUES (?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    session_id,
                    row["last_heartbeat_wall"],
                    row["last_heartbeat_monotonic"],
                    "PROBABLE_POWER_LOSS",
                    "warning",
                    "La sesión quedó abierta. Puede haber terminado por apagado, suspensión forzada o corte de energía.",
                    json.dumps({"reason": reason}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            recovered.append(session_id)
        conn.commit()
        return recovered

    def insert_snapshot(self, session_id: str, seq: int, snap: SystemSnapshot, events: Iterable[Event]) -> int:
        conn = self.connect()
        start = time.monotonic()
        active_json = json.dumps(snap.active_batteries, ensure_ascii=False)
        cur = conn.execute(
            """
            INSERT INTO samples (
              session_id, seq, wall_time, wall_iso, monotonic_time, ac_online,
              total_energy_now_uwh, total_energy_full_uwh, total_energy_full_design_uwh,
              total_power_now_uw, total_computed_percent, total_health_percent,
              active_batteries, sample_duration_ms, db_write_duration_ms,
              collector_rss_kib, collector_user_cpu_seconds, collector_system_cpu_seconds,
              loop_delay_ms, created_at_wall
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                seq,
                snap.wall_time,
                snap.wall_iso,
                snap.monotonic_time,
                none_bool_to_int(snap.ac_online),
                snap.total_energy_now_uwh,
                snap.total_energy_full_uwh,
                snap.total_energy_full_design_uwh,
                snap.total_power_now_uw,
                snap.total_computed_percent,
                snap.total_health_percent,
                active_json,
                snap.metrics.sample_duration_ms,
                snap.metrics.collector_rss_kib,
                snap.metrics.collector_user_cpu_seconds,
                snap.metrics.collector_system_cpu_seconds,
                snap.metrics.loop_delay_ms,
                time.time(),
            ),
        )
        assert cur.lastrowid is not None
        sample_id = cur.lastrowid
        for battery in snap.batteries:
            self._insert_battery(conn, session_id, sample_id, battery)
        for supply in snap.power_supplies:
            conn.execute(
                """
                INSERT INTO power_supplies (sample_id, session_id, name, type, online, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_id,
                    session_id,
                    supply.name,
                    supply.type,
                    none_bool_to_int(supply.online),
                    json.dumps(supply.raw, ensure_ascii=False, sort_keys=True),
                ),
            )
        for event in events:
            event.sample_id = sample_id
            event.wall_time = event.wall_time if event.wall_time is not None else snap.wall_time
            event.monotonic_time = event.monotonic_time if event.monotonic_time is not None else snap.monotonic_time
            self._insert_event(conn, session_id, event)
        conn.execute("UPDATE sessions SET sample_count = sample_count + 1 WHERE id = ?", (session_id,))
        db_write_ms = (time.monotonic() - start) * 1000.0
        conn.execute("UPDATE samples SET db_write_duration_ms = ? WHERE id = ?", (db_write_ms, sample_id))
        conn.commit()
        return sample_id

    def _insert_battery(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        sample_id: int,
        battery: BatterySnapshot,
    ) -> None:
        conn.execute(
            """
            INSERT INTO sample_batteries (
              sample_id, session_id, name, present, status, capacity_percent,
              computed_percent, health_percent, capacity_level, energy_now_uwh,
              energy_full_uwh, energy_full_design_uwh, power_now_uw, voltage_now_uv,
              voltage_min_design_uv, cycle_count, technology, manufacturer, model_name,
              serial_number, charge_control_start_threshold, charge_control_end_threshold,
              charge_start_threshold, charge_stop_threshold, charge_behaviour, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                session_id,
                battery.name,
                none_bool_to_int(battery.present),
                battery.status,
                battery.capacity_percent,
                battery.computed_percent,
                battery.health_percent,
                battery.capacity_level,
                battery.energy_now_uwh,
                battery.energy_full_uwh,
                battery.energy_full_design_uwh,
                battery.power_now_uw,
                battery.voltage_now_uv,
                battery.voltage_min_design_uv,
                battery.cycle_count,
                battery.technology,
                battery.manufacturer,
                battery.model_name,
                battery.serial_number,
                battery.charge_control_start_threshold,
                battery.charge_control_end_threshold,
                battery.charge_start_threshold,
                battery.charge_stop_threshold,
                battery.charge_behaviour,
                json.dumps(battery.raw, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _insert_event(self, conn: sqlite3.Connection, session_id: str, event: Event) -> None:
        conn.execute(
            """
            INSERT INTO events (
              session_id, sample_id, wall_time, monotonic_time, event_type,
              severity, battery_name, message, details_json, created_at_wall
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event.sample_id,
                event.wall_time,
                event.monotonic_time,
                event.event_type,
                event.severity,
                event.battery_name,
                event.message,
                event.details_json(),
                time.time(),
            ),
        )

    def insert_event(self, session_id: str, event: Event) -> None:
        conn = self.connect()
        self._insert_event(conn, session_id, event)
        conn.commit()

    def flush_to_disk(self) -> None:
        conn = self.connect()
        conn.commit()
        # The synchronous pragma already controls SQLite's durability. Extra fsyncs
        # are useful in black-box runs where the cost is acceptable.
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if not path.exists():
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass

    def list_sessions(self, limit: int = 50) -> list[sqlite3.Row]:
        conn = self.connect()
        return list(
            conn.execute(
                """
                SELECT id, name, started_at_iso, ended_at_iso, ended_reason,
                       probable_power_loss, sample_count, last_heartbeat_iso
                  FROM sessions
                 ORDER BY started_at_wall DESC
                 LIMIT ?
                """,
                (limit,),
            )
        )

    def latest_session_id(self) -> str | None:
        conn = self.connect()
        row = conn.execute("SELECT id FROM sessions ORDER BY started_at_wall DESC LIMIT 1").fetchone()
        return str(row["id"]) if row else None

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        conn = self.connect()
        return cast(sqlite3.Row | None, conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def fetch_session_series(self, session_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        conn = self.connect()
        sql = """
            SELECT s.seq, s.wall_time, s.wall_iso, s.monotonic_time, s.ac_online,
                   s.total_computed_percent, s.total_energy_now_uwh, s.total_power_now_uw,
                   b.name AS battery_name, b.status, b.capacity_percent, b.computed_percent,
                   b.health_percent, b.energy_now_uwh, b.energy_full_uwh, b.energy_full_design_uwh,
                   b.power_now_uw, b.voltage_now_uv, b.charge_control_start_threshold,
                   b.charge_control_end_threshold
              FROM samples s
              JOIN sample_batteries b ON b.sample_id = s.id
             WHERE s.session_id = ?
             ORDER BY s.seq ASC, b.name ASC
        """
        params: tuple[Any, ...] = (session_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (session_id, limit)
        return list(conn.execute(sql, params))

    def fetch_events(self, session_id: str, limit: int = 500) -> list[sqlite3.Row]:
        conn = self.connect()
        return list(
            conn.execute(
                """
                SELECT id, wall_time, event_type, severity, battery_name, message, details_json
                  FROM events
                 WHERE session_id = ?
                 ORDER BY COALESCE(wall_time, created_at_wall) ASC, id ASC
                 LIMIT ?
                """,
                (session_id, limit),
            )
        )

    def export_rows(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.fetch_session_series(session_id)]


def none_bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0
