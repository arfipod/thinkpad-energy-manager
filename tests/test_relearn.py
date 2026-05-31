from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot
from battery_auditor.core.relearn import (
    AFTER_DEEP_DISCHARGE,
    ENERGY_FULL_DESIGN_CHANGE,
    ENERGY_FULL_RELEARN,
    analyze_session_relearn,
)


def test_stable_energy_full_produces_no_relearn_event(tmp_path: Path) -> None:
    db, _cfg = _write_relearn_session(
        tmp_path,
        [
            _row(0, energy_now_wh=10.0, energy_full_wh=18.28, energy_full_design_wh=23.4),
            _row(1, energy_now_wh=9.9, energy_full_wh=18.28, energy_full_design_wh=23.4),
        ],
    )

    findings = analyze_session_relearn(db, "relearn-session")

    assert findings == []


def test_energy_full_change_from_field_data_triggers_relearn(tmp_path: Path) -> None:
    db, _cfg = _write_relearn_session(
        tmp_path,
        [
            _row(0, energy_now_wh=1.0, energy_full_wh=18.28, energy_full_design_wh=23.4),
            _row(1, energy_now_wh=1.0, energy_full_wh=19.91, energy_full_design_wh=23.4),
        ],
    )

    findings = analyze_session_relearn(db, "relearn-session")

    assert [finding.event_type for finding in findings] == [ENERGY_FULL_RELEARN]
    finding = findings[0]
    assert finding.battery_name == "BAT0"
    assert finding.old_energy_full_wh == 18.28
    assert finding.new_energy_full_wh == 19.91
    assert round(finding.delta_wh or 0.0, 2) == 1.63
    assert round(finding.old_health_percent or 0.0, 1) == 78.1
    assert round(finding.new_health_percent or 0.0, 1) == 85.1
    assert finding.likely_cause == AFTER_DEEP_DISCHARGE


def test_tiny_rounding_changes_do_not_trigger_relearn(tmp_path: Path) -> None:
    db, _cfg = _write_relearn_session(
        tmp_path,
        [
            _row(0, energy_now_wh=10.0, energy_full_wh=18.28, energy_full_design_wh=23.4),
            _row(1, energy_now_wh=10.0, energy_full_wh=18.30, energy_full_design_wh=23.4),
        ],
    )

    findings = analyze_session_relearn(db, "relearn-session")

    assert findings == []


def test_energy_full_design_change_is_recorded_but_not_relearn(tmp_path: Path) -> None:
    db, _cfg = _write_relearn_session(
        tmp_path,
        [
            _row(0, energy_now_wh=10.0, energy_full_wh=18.28, energy_full_design_wh=23.4),
            _row(1, energy_now_wh=10.0, energy_full_wh=18.28, energy_full_design_wh=24.0),
        ],
    )

    findings = analyze_session_relearn(db, "relearn-session")

    assert [finding.event_type for finding in findings] == [ENERGY_FULL_DESIGN_CHANGE]
    assert findings[0].design_delta_wh == 0.6000000000000014
    assert findings[0].delta_wh == 0.0


def test_relearn_cli_uses_non_physical_recovery_wording(tmp_path: Path, capsys: Any) -> None:
    _db, cfg = _write_relearn_session(
        tmp_path,
        [
            _row(0, energy_now_wh=1.0, energy_full_wh=18.28, energy_full_design_wh=23.4),
            _row(1, energy_now_wh=1.0, energy_full_wh=19.91, energy_full_design_wh=23.4),
        ],
    )
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "analyze", "relearn", "relearn-session"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "ENERGY_FULL_RELEARN" in output
    assert "This does not mean the battery physically recovered" in output

    out_path = tmp_path / "relearn.json"
    rc = cli_main(
        [
            "--config",
            str(config_path),
            "analyze",
            "--relearn",
            "relearn-session",
            "--format",
            "json",
            "--out",
            str(out_path),
        ]
    )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload[0]["event_type"] == ENERGY_FULL_RELEARN
    assert payload[0]["likely_cause"] == AFTER_DEEP_DISCHARGE


def _write_relearn_session(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[BatteryDatabase, AuditorConfig]:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("relearn-session", "relearn session", cfg.to_json())
    for row in rows:
        db.insert_snapshot("relearn-session", int(row["seq"]), _snapshot(**row), [])
    db.end_session("relearn-session")
    return db, cfg


def _row(
    seq: int,
    *,
    energy_now_wh: float,
    energy_full_wh: float,
    energy_full_design_wh: float,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "energy_now_wh": energy_now_wh,
        "energy_full_wh": energy_full_wh,
        "energy_full_design_wh": energy_full_design_wh,
    }


def _snapshot(
    seq: int,
    energy_now_wh: float,
    energy_full_wh: float,
    energy_full_design_wh: float,
) -> SystemSnapshot:
    wall_time = 1_700_000_000.0 + float(seq)
    return SystemSnapshot(
        wall_time=wall_time,
        monotonic_time=wall_time,
        power_supplies=[PowerSupplySnapshot(name="AC", type="Mains", online=False)],
        batteries=[_battery("BAT0", energy_now_wh, energy_full_wh, energy_full_design_wh)],
    )


def _battery(
    name: str,
    energy_now_wh: float,
    energy_full_wh: float,
    energy_full_design_wh: float,
) -> BatterySnapshot:
    return BatterySnapshot(
        name=name,
        present=True,
        status="Discharging",
        capacity_percent=(energy_now_wh / energy_full_wh) * 100.0,
        energy_now_uwh=int(energy_now_wh * 1_000_000),
        energy_full_uwh=int(energy_full_wh * 1_000_000),
        energy_full_design_uwh=int(energy_full_design_wh * 1_000_000),
        power_now_uw=6_000_000,
        voltage_now_uv=11_000_000,
    )


def _write_config(tmp_path: Path, cfg: AuditorConfig) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[general]",
                f"data_dir = {json.dumps(str(cfg.data_dir))}",
                f"db_path = {json.dumps(str(cfg.resolved_db_path()))}",
            ]
        ),
        encoding="utf-8",
    )
    return config_path
