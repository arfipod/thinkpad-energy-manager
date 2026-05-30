from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import DATA_TABLES, BatteryDatabase, repair_database
from battery_auditor.core.events import EventDetector
from battery_auditor.core.models import Event
from battery_auditor.core.sysfs import read_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_insert_and_fetch_session(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("s1", "test", cfg.to_json())
    detector = EventDetector(cfg)
    snap = read_snapshot(FIXTURE)
    events = detector.process(snap)
    sample_id = db.insert_snapshot("s1", 0, snap, events)
    assert sample_id > 0
    db.end_session("s1")
    sessions = db.list_sessions()
    assert len(sessions) == 1
    rows = db.fetch_session_series("s1")
    assert len(rows) == 2


def test_init_schema_tolerates_locked_journal_mode_pragma(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "test.sqlite3"
    real_connect = sqlite3.connect

    class LockedJournalModeConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        @property
        def row_factory(self) -> Any:
            return self.conn.row_factory

        @row_factory.setter
        def row_factory(self, value: Any) -> None:
            self.conn.row_factory = value

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if sql.startswith("PRAGMA journal_mode"):
                raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.conn, name)

    def locked_connect(*args: Any, **kwargs: Any) -> LockedJournalModeConnection:
        return LockedJournalModeConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", locked_connect)
    cfg = AuditorConfig(data_dir=tmp_path, db_path=db_path, sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)

    db.init_schema()

    assert db.check_integrity(quick=True) == ["ok"]


def test_reader_schema_init_does_not_reconfigure_journal(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "test.sqlite3"
    real_connect = sqlite3.connect

    class RejectJournalModeConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        @property
        def row_factory(self) -> Any:
            return self.conn.row_factory

        @row_factory.setter
        def row_factory(self, value: Any) -> None:
            self.conn.row_factory = value

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if sql.startswith("PRAGMA journal_mode"):
                raise AssertionError("reader connection must not reconfigure journal mode")
            return self.conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.conn, name)

    def rejecting_connect(*args: Any, **kwargs: Any) -> RejectJournalModeConnection:
        return RejectJournalModeConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", rejecting_connect)
    cfg = AuditorConfig(data_dir=tmp_path, db_path=db_path, sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)

    db.init_schema(configure_journal=False)

    assert db.check_integrity(quick=True) == ["ok"]


def test_recover_open_session(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("s1", "test", cfg.to_json())
    recovered = db.recover_open_sessions()
    assert recovered == ["s1"]
    session = db.get_session("s1")
    assert session is not None
    assert session["probable_power_loss"] == 1


def test_repair_database_writes_clean_copy(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("s1", "test", cfg.to_json())
    snap = read_snapshot(FIXTURE)
    sample_id = db.insert_snapshot("s1", 0, snap, [])
    db.close()

    repaired_path = tmp_path / "repaired.sqlite3"
    result = repair_database(cfg.resolved_db_path(), repaired_path)

    assert result.integrity == "ok"
    assert result.repaired_path == repaired_path
    assert result.copied["sessions"] == 1
    assert result.copied["samples"] == 1
    assert result.copied["sample_batteries"] == 2

    repaired = BatteryDatabase(repaired_path, AuditorConfig(db_path=repaired_path))
    rows = repaired.fetch_session_series("s1")
    assert sample_id == 1
    assert len(rows) == 2


def test_delete_session_cascades_dependent_rows(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("s1", "test", cfg.to_json())
    snap = read_snapshot(FIXTURE)
    db.insert_snapshot("s1", 0, snap, [Event("TEST_EVENT", "info", "test")])

    assert db.delete_session("s1") is True

    conn = db.connect()
    for table in DATA_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0


def test_merge_sessions_copies_rows_and_renumbers_seq(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    _insert_test_session(db, cfg, "s1", wall_time=1000.0, source_seq=20)
    _insert_test_session(db, cfg, "s2", wall_time=1005.0, source_seq=7)

    merged_id = db.merge_sessions(["s1", "s2"], "merged", "merged-test")

    assert merged_id == "merged"
    conn = db.connect()
    sample_rows = conn.execute("SELECT id, seq, wall_time FROM samples WHERE session_id = ? ORDER BY seq", ("merged",)).fetchall()
    assert [row["seq"] for row in sample_rows] == [0, 1]
    assert [row["wall_time"] for row in sample_rows] == [1000.0, 1005.0]
    assert conn.execute("SELECT COUNT(*) FROM sample_batteries WHERE session_id = ?", ("merged",)).fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM power_supplies WHERE session_id = ?", ("merged",)).fetchone()[0] == 2


def test_merged_events_include_provenance(tmp_path: Path) -> None:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    _insert_test_session(db, cfg, "s1", wall_time=1000.0, source_seq=1)
    _insert_test_session(db, cfg, "s2", wall_time=1001.0, source_seq=1)

    db.merge_sessions(["s1", "s2"], "merged", "merged-test")

    events = db.fetch_events("merged", limit=100)
    assert any(row["event_type"] == "SOURCE_EVENT" for row in events)
    provenance = [row for row in events if row["event_type"] == "SESSION_MERGED"]
    assert len(provenance) == 1
    assert "s1" in provenance[0]["details_json"]
    assert "s2" in provenance[0]["details_json"]


def _insert_test_session(
    db: BatteryDatabase,
    cfg: AuditorConfig,
    session_id: str,
    *,
    wall_time: float,
    source_seq: int,
) -> None:
    db.start_session(session_id, session_id, cfg.to_json())
    snap = read_snapshot(FIXTURE)
    snap.wall_time = wall_time
    snap.monotonic_time = wall_time
    db.insert_snapshot(session_id, source_seq, snap, [Event("SOURCE_EVENT", "info", f"event from {session_id}")])
    db.end_session(session_id)
