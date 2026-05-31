from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig, TlpThresholdExpectation, load_config
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot
from battery_auditor.core.thresholds import (
    STATUS_MISMATCH,
    STATUS_OK,
    STATUS_UNKNOWN,
    THRESHOLD_MISMATCH,
    THRESHOLD_RESTORED,
    analyze_session_thresholds,
    samples_from_rows,
    threshold_event_findings,
)


def test_configured_75_80_and_sysfs_75_80_is_ok(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 75, 80)]),
        ],
    )

    statuses = analyze_session_thresholds(db, "threshold-session", cfg)

    by_battery = {status.battery_name: status for status in statuses}
    assert by_battery["BAT0"].status == STATUS_OK
    assert by_battery["BAT0"].mismatch is False
    assert by_battery["BAT0"].last_ok_wall_iso is not None


def test_configured_75_80_and_sysfs_0_100_is_mismatch(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 0, 100)]),
        ],
    )

    statuses = analyze_session_thresholds(db, "threshold-session", cfg)

    assert statuses[0].status == STATUS_MISMATCH
    assert statuses[0].mismatch is True
    assert statuses[0].last_mismatch_wall_iso is not None


def test_missing_sysfs_threshold_fields_are_unknown(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", None, None)]),
        ],
    )

    statuses = analyze_session_thresholds(db, "threshold-session", cfg)

    assert statuses[0].status == STATUS_UNKNOWN
    assert statuses[0].mismatch is False
    assert statuses[0].sysfs_start_threshold is None
    assert statuses[0].sysfs_stop_threshold is None


def test_bat0_ok_and_bat1_mismatch_are_independent(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 75, 80), ("BAT1", 0, 100)]),
        ],
    )

    statuses = {status.battery_name: status for status in analyze_session_thresholds(db, "threshold-session", cfg)}

    assert statuses["BAT0"].status == STATUS_OK
    assert statuses["BAT1"].status == STATUS_MISMATCH


def test_missing_configured_thresholds_are_unknown(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 75, 80)]),
        ],
    )
    cfg.expected_thresholds.clear()

    statuses = analyze_session_thresholds(db, "threshold-session", cfg)

    assert statuses[0].battery_name == "BAT0"
    assert statuses[0].status == STATUS_UNKNOWN


def test_threshold_events_include_mismatch_and_restored(tmp_path: Path) -> None:
    db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 75, 80)]),
            _snapshot_row(1, [("BAT0", 0, 100)]),
            _snapshot_row(2, [("BAT0", 75, 80)]),
        ],
    )
    samples = samples_from_rows(db.fetch_session_series("threshold-session"))

    findings = threshold_event_findings(samples, cfg)

    assert [finding.event_type for finding in findings] == [THRESHOLD_MISMATCH, THRESHOLD_RESTORED]


def test_threshold_cli_status_table_and_json(tmp_path: Path, capsys: Any) -> None:
    _db, cfg = _write_threshold_session(
        tmp_path,
        [
            _snapshot_row(0, [("BAT0", 75, 80), ("BAT1", 0, 100)]),
        ],
    )
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "thresholds", "status", "threshold-session"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "BAT0" in output
    assert "BAT1" in output
    assert "75/80" in output
    assert "0/100" in output
    assert "MISMATCH" in output

    rc = cli_main(["--config", str(config_path), "thresholds", "status", "threshold-session", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["battery_name"]: row["status"] for row in payload} == {
        "BAT0": STATUS_OK,
        "BAT1": STATUS_MISMATCH,
    }


def test_thresholds_config_section_is_supported(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[thresholds.BAT0]",
                "start = 75",
                "stop = 80",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config([config_path])

    assert cfg.expected_thresholds["BAT0"].start == 75
    assert cfg.expected_thresholds["BAT0"].stop == 80


def _write_threshold_session(
    tmp_path: Path,
    rows: list[dict[str, Any]],
) -> tuple[BatteryDatabase, AuditorConfig]:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    for battery in ("BAT0", "BAT1"):
        cfg.expected_thresholds[battery] = TlpThresholdExpectation(start=75, stop=80)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("threshold-session", "threshold session", cfg.to_json())
    for row in rows:
        db.insert_snapshot("threshold-session", int(row["seq"]), _snapshot(**row), [])
    db.end_session("threshold-session")
    return db, cfg


def _snapshot_row(seq: int, batteries: list[tuple[str, int | None, int | None]]) -> dict[str, Any]:
    return {"seq": seq, "batteries": batteries}


def _snapshot(seq: int, batteries: list[tuple[str, int | None, int | None]]) -> SystemSnapshot:
    wall_time = 1_700_000_000.0 + float(seq)
    return SystemSnapshot(
        wall_time=wall_time,
        monotonic_time=wall_time,
        power_supplies=[PowerSupplySnapshot(name="AC", type="Mains", online=False)],
        batteries=[_battery(name, start, stop) for name, start, stop in batteries],
    )


def _battery(name: str, start: int | None, stop: int | None) -> BatterySnapshot:
    return BatterySnapshot(
        name=name,
        present=True,
        status="Discharging",
        capacity_percent=50.0,
        energy_now_uwh=10_000_000,
        energy_full_uwh=20_000_000,
        energy_full_design_uwh=25_000_000,
        power_now_uw=6_000_000,
        voltage_now_uv=11_000_000,
        charge_control_start_threshold=start,
        charge_control_end_threshold=stop,
    )


def _write_config(tmp_path: Path, cfg: AuditorConfig) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[general]",
                f"data_dir = {json.dumps(str(cfg.data_dir))}",
                f"db_path = {json.dumps(str(cfg.resolved_db_path()))}",
                "",
                "[thresholds.BAT0]",
                "start = 75",
                "stop = 80",
                "",
                "[thresholds.BAT1]",
                "start = 75",
                "stop = 80",
            ]
        ),
        encoding="utf-8",
    )
    return config_path
