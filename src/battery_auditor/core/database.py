from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from battery_auditor.config import AuditorConfig
from battery_auditor.core.models import BatterySnapshot, Event, SystemSnapshot

SCHEMA_VERSION = 3

DATA_TABLES = ("sessions", "samples", "sample_batteries", "power_supplies", "events")


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
    system_cpu_percent REAL,
    system_load_1m REAL,
    system_memory_total_kib INTEGER,
    system_memory_available_kib INTEGER,
    system_memory_used_percent REAL,
    system_disk_read_bytes_per_second REAL,
    system_disk_write_bytes_per_second REAL,
    display_brightness_percent REAL,
    display_brightness_raw INTEGER,
    display_brightness_max INTEGER,
    wifi_enabled INTEGER,
    bluetooth_enabled INTEGER,
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


@dataclass(slots=True)
class DatabaseRepairResult:
    source_path: Path
    repaired_path: Path
    backup_path: Path | None = None
    replaced: bool = False
    copied: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    integrity: str = ""


class BatteryDatabase:
    def __init__(self, db_path: Path, cfg: AuditorConfig | None = None, *, read_only: bool = False) -> None:
        self.db_path = db_path.expanduser()
        self.cfg = cfg or AuditorConfig(db_path=self.db_path)
        self.conn: sqlite3.Connection | None = None
        self.read_only = read_only

    def connect(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        if self.read_only:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, timeout=30.0, uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute(f"PRAGMA synchronous = {self.cfg.sqlite_synchronous}")
            conn.execute(f"PRAGMA wal_autocheckpoint = {int(self.cfg.sqlite_wal_autocheckpoint_pages)}")
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def init_schema(self, *, configure_journal: bool = True) -> None:
        if self.read_only:
            self._verify_schema_version()
            return
        conn = self.connect()
        if configure_journal:
            try:
                conn.execute(f"PRAGMA journal_mode = {self.cfg.sqlite_journal_mode}")
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        conn.executescript(SCHEMA_SQL)
        self._migrate_schema(current_version)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    def _verify_schema_version(self) -> None:
        conn = self.connect()
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Unsupported database schema version: {version}")

    def _migrate_schema(self, current_version: int) -> None:
        if current_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Unsupported database schema version: {current_version}")
        if current_version < 2:
            self._add_missing_columns(
                "samples",
                {
                    "system_cpu_percent": "REAL",
                    "system_load_1m": "REAL",
                    "system_memory_total_kib": "INTEGER",
                    "system_memory_available_kib": "INTEGER",
                    "system_memory_used_percent": "REAL",
                    "system_disk_read_bytes_per_second": "REAL",
                    "system_disk_write_bytes_per_second": "REAL",
                },
            )
        if current_version < 3:
            self._add_missing_columns(
                "samples",
                {
                    "display_brightness_percent": "REAL",
                    "display_brightness_raw": "INTEGER",
                    "display_brightness_max": "INTEGER",
                    "wifi_enabled": "INTEGER",
                    "bluetooth_enabled": "INTEGER",
                },
            )

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        conn = self.connect()
        existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, column_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")

    def check_integrity(self, *, quick: bool = True) -> list[str]:
        pragma = "quick_check" if quick else "integrity_check"
        conn = self.connect()
        return [str(row[0]) for row in conn.execute(f"PRAGMA {pragma}")]

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
                    "The session was left open. It may have ended because of shutdown, forced suspend, or power loss.",
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
              loop_delay_ms, system_cpu_percent, system_load_1m, system_memory_total_kib,
              system_memory_available_kib, system_memory_used_percent,
              system_disk_read_bytes_per_second, system_disk_write_bytes_per_second,
              display_brightness_percent, display_brightness_raw, display_brightness_max,
              wifi_enabled, bluetooth_enabled,
              created_at_wall
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                snap.metrics.system_cpu_percent,
                snap.metrics.system_load_1m,
                snap.metrics.system_memory_total_kib,
                snap.metrics.system_memory_available_kib,
                snap.metrics.system_memory_used_percent,
                snap.metrics.system_disk_read_bytes_per_second,
                snap.metrics.system_disk_write_bytes_per_second,
                snap.metrics.display_brightness_percent,
                snap.metrics.display_brightness_raw,
                snap.metrics.display_brightness_max,
                none_bool_to_int(snap.metrics.wifi_enabled),
                none_bool_to_int(snap.metrics.bluetooth_enabled),
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
                SELECT id, name, started_at_wall, started_at_iso, ended_at_wall, ended_at_iso, ended_reason,
                       probable_power_loss, sample_count, last_heartbeat_wall, last_heartbeat_iso,
                       notes,
                       (
                         SELECT MAX(wall_iso)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS last_sample_iso,
                       (
                         SELECT MAX(wall_time)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS last_sample_wall,
                       (
                         SELECT COUNT(*)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS real_sample_count
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

    def list_open_sessions(self) -> list[sqlite3.Row]:
        conn = self.connect()
        return list(
            conn.execute(
                """
                SELECT id, name, started_at_wall, started_at_iso, ended_at_wall, ended_at_iso,
                       ended_reason, probable_power_loss, sample_count, last_heartbeat_wall,
                       last_heartbeat_iso, notes,
                       (
                         SELECT MAX(wall_iso)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS last_sample_iso,
                       (
                         SELECT MAX(wall_time)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS last_sample_wall,
                       (
                         SELECT COUNT(*)
                           FROM samples
                          WHERE samples.session_id = sessions.id
                       ) AS real_sample_count
                  FROM sessions
                 WHERE ended_at_wall IS NULL
                 ORDER BY started_at_wall DESC
                """
            )
        )

    def delete_session(self, session_id: str) -> bool:
        conn = self.connect()
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0

    def rename_session(self, session_id: str, name: str) -> bool:
        conn = self.connect()
        cur = conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, session_id))
        conn.commit()
        return cur.rowcount > 0

    def update_session_notes(self, session_id: str, notes: str) -> bool:
        conn = self.connect()
        cur = conn.execute("UPDATE sessions SET notes = ? WHERE id = ?", (notes, session_id))
        conn.commit()
        return cur.rowcount > 0

    def fetch_session_series(
        self,
        session_id: str,
        limit: int | None = None,
        after_seq: int | None = None,
    ) -> list[sqlite3.Row]:
        conn = self.connect()
        sql = """
            SELECT s.id AS sample_id, s.seq, s.wall_time, s.wall_iso, s.monotonic_time, s.ac_online,
                   s.total_computed_percent, s.total_energy_now_uwh, s.total_power_now_uw,
                   s.system_cpu_percent, s.system_load_1m, s.system_memory_total_kib,
                   s.system_memory_available_kib, s.system_memory_used_percent,
                   s.system_disk_read_bytes_per_second, s.system_disk_write_bytes_per_second,
                   s.display_brightness_percent, s.display_brightness_raw, s.display_brightness_max,
                   s.wifi_enabled, s.bluetooth_enabled,
                   b.name AS battery_name, b.status, b.capacity_percent, b.computed_percent,
                   b.health_percent, b.energy_now_uwh, b.energy_full_uwh, b.energy_full_design_uwh,
                   b.power_now_uw, b.voltage_now_uv, b.charge_control_start_threshold,
                   b.charge_control_end_threshold, b.charge_start_threshold, b.charge_stop_threshold
              FROM samples s
              JOIN sample_batteries b ON b.sample_id = s.id
             WHERE s.session_id = ?
        """
        params_list: list[Any] = [session_id]
        if after_seq is not None:
            sql += " AND s.seq > ?"
            params_list.append(after_seq)
        sql += " ORDER BY s.seq ASC, b.name ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params_list.append(limit)
        return list(conn.execute(sql, tuple(params_list)))

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

    def merge_sessions(self, source_session_ids: list[str], merged_session_id: str, name: str) -> str:
        from battery_auditor.core.models import wall_iso_from_timestamp

        source_ids = list(dict.fromkeys(source_session_ids))
        if not source_ids:
            raise ValueError("At least one source session is required.")
        conn = self.connect()
        if self.get_session(merged_session_id) is not None:
            raise ValueError(f"Session already exists: {merged_session_id}")

        placeholders = ", ".join("?" for _ in source_ids)
        source_rows = list(conn.execute(f"SELECT * FROM sessions WHERE id IN ({placeholders})", tuple(source_ids)))
        by_id = {str(row["id"]): row for row in source_rows}
        missing = [session_id for session_id in source_ids if session_id not in by_id]
        if missing:
            raise ValueError(f"Unknown source session(s): {', '.join(missing)}")

        sample_rows: list[sqlite3.Row] = []
        for session_id in source_ids:
            sample_rows.extend(
                conn.execute("SELECT * FROM samples WHERE session_id = ?", (session_id,)).fetchall()
            )
        source_order = {session_id: index for index, session_id in enumerate(source_ids)}
        sample_rows.sort(
            key=lambda row: (
                float(row["wall_time"]),
                source_order.get(str(row["session_id"]), 0),
                int(row["seq"]),
                int(row["id"]),
            )
        )

        now = time.time()
        started_wall_values = [float(row["started_at_wall"]) for row in source_rows]
        started_wall = min(started_wall_values) if started_wall_values else now
        ended_candidates = [
            float(row["ended_at_wall"])
            for row in source_rows
            if row["ended_at_wall"] is not None
        ] + [float(row["wall_time"]) for row in sample_rows]
        ended_wall = max(ended_candidates) if ended_candidates else started_wall
        last_heartbeat_candidates = [
            float(row["last_heartbeat_wall"])
            for row in source_rows
            if row["last_heartbeat_wall"] is not None
        ]
        last_heartbeat_wall = max(last_heartbeat_candidates) if last_heartbeat_candidates else None
        last_heartbeat_iso = wall_iso_from_timestamp(last_heartbeat_wall) if last_heartbeat_wall is not None else None
        probable_power_loss = 1 if any(int(row["probable_power_loss"]) for row in source_rows) else 0
        config_json = json.dumps(
            {
                "synthetic": True,
                "merged_from": source_ids,
                "created_at_iso": wall_iso_from_timestamp(now),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        system_json = json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "synthetic": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        with conn:
            conn.execute(
                """
                INSERT INTO sessions (
                  id, name, hostname, kernel, started_at_wall, started_at_iso,
                  started_at_monotonic, ended_at_wall, ended_at_iso, ended_reason,
                  probable_power_loss, last_heartbeat_wall, last_heartbeat_iso,
                  last_heartbeat_monotonic, sample_count, config_json, system_json, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)
                """,
                (
                    merged_session_id,
                    name,
                    platform.node(),
                    platform.release(),
                    started_wall,
                    wall_iso_from_timestamp(started_wall),
                    min(float(row["started_at_monotonic"]) for row in source_rows),
                    ended_wall,
                    wall_iso_from_timestamp(ended_wall),
                    "merged",
                    probable_power_loss,
                    last_heartbeat_wall,
                    last_heartbeat_iso,
                    config_json,
                    system_json,
                    f"Merged from: {', '.join(source_ids)}",
                ),
            )

            sample_id_map: dict[int, int] = {}
            for new_seq, sample in enumerate(sample_rows):
                new_sample_id = self._copy_sample(conn, sample, merged_session_id, new_seq)
                sample_id_map[int(sample["id"])] = new_sample_id
                self._copy_sample_children(conn, "sample_batteries", int(sample["id"]), new_sample_id, merged_session_id)
                self._copy_sample_children(conn, "power_supplies", int(sample["id"]), new_sample_id, merged_session_id)

            self._copy_merged_events(conn, source_ids, merged_session_id, sample_id_map)
            conn.execute(
                "UPDATE sessions SET sample_count = (SELECT COUNT(*) FROM samples WHERE session_id = ?) WHERE id = ?",
                (merged_session_id, merged_session_id),
            )
            self._insert_event(
                conn,
                merged_session_id,
                Event(
                    "SESSION_MERGED",
                    "info",
                    f"Merged from sessions: {', '.join(source_ids)}.",
                    wall_time=now,
                    monotonic_time=time.monotonic(),
                    details={"source_session_ids": source_ids},
                ),
            )
            overlap_details = self._merge_overlap_details(conn, source_ids)
            if overlap_details:
                self._insert_event(
                    conn,
                    merged_session_id,
                    Event(
                        "SESSION_MERGE_OVERLAP_WARNING",
                        "warning",
                        "Source sessions have overlapping wall-clock ranges. Times were preserved.",
                        wall_time=now,
                        monotonic_time=time.monotonic(),
                        details=overlap_details,
                    ),
                )
        return merged_session_id

    def _copy_sample(
        self,
        conn: sqlite3.Connection,
        sample: sqlite3.Row,
        merged_session_id: str,
        new_seq: int,
    ) -> int:
        columns = [
            "session_id",
            "seq",
            "wall_time",
            "wall_iso",
            "monotonic_time",
            "ac_online",
            "total_energy_now_uwh",
            "total_energy_full_uwh",
            "total_energy_full_design_uwh",
            "total_power_now_uw",
            "total_computed_percent",
            "total_health_percent",
            "active_batteries",
            "sample_duration_ms",
            "db_write_duration_ms",
            "collector_rss_kib",
            "collector_user_cpu_seconds",
            "collector_system_cpu_seconds",
            "loop_delay_ms",
            "system_cpu_percent",
            "system_load_1m",
            "system_memory_total_kib",
            "system_memory_available_kib",
            "system_memory_used_percent",
            "system_disk_read_bytes_per_second",
            "system_disk_write_bytes_per_second",
            "display_brightness_percent",
            "display_brightness_raw",
            "display_brightness_max",
            "wifi_enabled",
            "bluetooth_enabled",
            "created_at_wall",
        ]
        values = [
            merged_session_id if column == "session_id" else new_seq if column == "seq" else sample[column]
            for column in columns
        ]
        cur = conn.execute(
            f"INSERT INTO samples ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def _copy_sample_children(
        self,
        conn: sqlite3.Connection,
        table: str,
        old_sample_id: int,
        new_sample_id: int,
        merged_session_id: str,
    ) -> None:
        columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})") if row["name"] != "id"]
        insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        for row in conn.execute(f"SELECT * FROM {table} WHERE sample_id = ? ORDER BY id ASC", (old_sample_id,)):
            values = [
                new_sample_id if column == "sample_id" else merged_session_id if column == "session_id" else row[column]
                for column in columns
            ]
            conn.execute(insert_sql, values)

    def _copy_merged_events(
        self,
        conn: sqlite3.Connection,
        source_session_ids: list[str],
        merged_session_id: str,
        sample_id_map: dict[int, int],
    ) -> None:
        columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(events)") if row["name"] != "id"]
        insert_sql = f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        for session_id in source_session_ids:
            events = conn.execute(
                """
                SELECT *
                  FROM events
                 WHERE session_id = ?
                 ORDER BY COALESCE(wall_time, created_at_wall) ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
            for event in events:
                old_sample_id = event["sample_id"]
                values = [
                    merged_session_id
                    if column == "session_id"
                    else sample_id_map.get(int(old_sample_id))
                    if column == "sample_id" and old_sample_id is not None
                    else event[column]
                    for column in columns
                ]
                conn.execute(insert_sql, values)

    def _merge_overlap_details(self, conn: sqlite3.Connection, source_session_ids: list[str]) -> dict[str, Any] | None:
        ranges: list[dict[str, Any]] = []
        for session_id in source_session_ids:
            row = conn.execute(
                "SELECT MIN(wall_time) AS start_wall, MAX(wall_time) AS end_wall FROM samples WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None or row["start_wall"] is None or row["end_wall"] is None:
                continue
            ranges.append({"session_id": session_id, "start_wall": row["start_wall"], "end_wall": row["end_wall"]})
        overlaps: list[dict[str, Any]] = []
        for left_index, left in enumerate(ranges):
            for right in ranges[left_index + 1 :]:
                if float(left["start_wall"]) <= float(right["end_wall"]) and float(right["start_wall"]) <= float(left["end_wall"]):
                    overlaps.append({"left": left["session_id"], "right": right["session_id"]})
        if not overlaps:
            return None
        return {"source_ranges": ranges, "overlaps": overlaps}


def none_bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def repair_database(
    source_path: Path,
    output_path: Path | None = None,
    *,
    replace: bool = False,
) -> DatabaseRepairResult:
    source = source_path.expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    repaired = (output_path.expanduser() if output_path is not None else source.with_name(f"{source.name}.repaired-{stamp}"))
    backup: Path | None = None

    if repaired.exists():
        repaired.unlink()

    BatteryDatabase(repaired, AuditorConfig(db_path=repaired)).init_schema()
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(repaired)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA foreign_keys = OFF")

    copied: dict[str, int] = {}
    failed: dict[str, int] = {}
    for table in DATA_TABLES:
        table_copied, table_failed = _copy_repairable_rows(src, dst, table)
        copied[table] = table_copied
        failed[table] = table_failed
        dst.commit()

    _remove_orphaned_rows(dst)
    _restore_sqlite_sequences(dst)
    dst.execute("PRAGMA foreign_keys = ON")
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    integrity = _single_pragma_value(dst, "PRAGMA integrity_check")
    if integrity != "ok":
        raise sqlite3.DatabaseError(f"Repaired database failed integrity_check: {integrity}")
    fk_issues = list(dst.execute("PRAGMA foreign_key_check"))
    if fk_issues:
        raise sqlite3.DatabaseError(f"Repaired database has {len(fk_issues)} foreign key issue(s)")
    dst.close()
    src.close()

    if replace:
        backup = source.parent / "backups" / f"{source.name}.corrupt-{stamp}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup.with_name(backup.name + suffix))
                sidecar.unlink()
        repaired.replace(source)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(repaired) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    return DatabaseRepairResult(
        source_path=source,
        repaired_path=source if replace else repaired,
        backup_path=backup,
        replaced=replace,
        copied=copied,
        failed=failed,
        integrity=integrity,
    )


def _copy_repairable_rows(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
) -> tuple[int, int]:
    columns = [str(row["name"]) for row in dst.execute(f"PRAGMA table_info({table})")]
    column_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})"

    try:
        bounds = src.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
    except sqlite3.DatabaseError:
        return 0, 1
    if bounds is None or bounds[0] is None or bounds[1] is None:
        return 0, 0

    copied = 0
    failed = 0
    for rowid in range(int(bounds[0]), int(bounds[1]) + 1):
        try:
            row = src.execute(f"SELECT {column_sql} FROM {table} WHERE rowid = ?", (rowid,)).fetchone()
            if row is None:
                continue
            dst.execute(insert_sql, [row[column] for column in columns])
            copied += 1
        except sqlite3.DatabaseError:
            failed += 1
    return copied, failed


def _restore_sqlite_sequences(conn: sqlite3.Connection) -> None:
    for table in DATA_TABLES:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not any(row["name"] == "id" for row in rows):
            continue
        max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        if max_id is not None:
            conn.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, max_id))
    conn.commit()


def _remove_orphaned_rows(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM sample_batteries
         WHERE sample_id NOT IN (SELECT id FROM samples)
            OR session_id NOT IN (SELECT id FROM sessions);

        DELETE FROM power_supplies
         WHERE sample_id NOT IN (SELECT id FROM samples)
            OR session_id NOT IN (SELECT id FROM sessions);

        DELETE FROM events
         WHERE session_id NOT IN (SELECT id FROM sessions)
            OR (sample_id IS NOT NULL AND sample_id NOT IN (SELECT id FROM samples));

        DELETE FROM samples
         WHERE session_id NOT IN (SELECT id FROM sessions);

        UPDATE sessions
           SET sample_count = (
               SELECT COUNT(*)
                 FROM samples
                WHERE samples.session_id = sessions.id
           );
        """
    )
    conn.commit()


def _single_pragma_value(conn: sqlite3.Connection, sql: str) -> str:
    row = conn.execute(sql).fetchone()
    return str(row[0]) if row is not None else ""
