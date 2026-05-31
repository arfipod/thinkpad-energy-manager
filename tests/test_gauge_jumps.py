from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.gauge_jumps import (
    AFTER_RESUME_RECONCILIATION,
    IMPOSSIBLE_ENERGY_DROP,
    LOW_END_GAUGE_JUMP,
    RECOVERY_JUMP,
    GaugeJumpConfig,
    analyze_session_jumps,
)
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot


def test_normal_discharge_at_six_watts_over_one_second_does_not_trigger(tmp_path: Path) -> None:
    db, _cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, False, "Discharging", 20.0, 6.0),
            _row(1, False, "Discharging", 20.0 - (6.0 / 3600.0), 6.0),
        ],
    )

    findings = analyze_session_jumps(db, "jump-session")

    assert findings == []


def test_one_wh_drop_in_one_second_at_six_watts_triggers(tmp_path: Path) -> None:
    db, _cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, False, "Discharging", 20.0, 6.0),
            _row(1, False, "Discharging", 19.0, 6.0),
        ],
    )

    findings = analyze_session_jumps(db, "jump-session")

    assert [finding.event_type for finding in findings] == [IMPOSSIBLE_ENERGY_DROP]
    assert findings[0].observed_wh_delta == -1.0
    assert findings[0].expected_max_wh_delta == 0.10


def test_jump_below_twenty_percent_is_low_end_gauge_jump(tmp_path: Path) -> None:
    db, _cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, False, "Discharging", 10.0, 6.0),
            _row(1, False, "Discharging", 3.0, 6.0),
        ],
    )

    findings = analyze_session_jumps(db, "jump-session")

    assert [finding.event_type for finding in findings] == [LOW_END_GAUGE_JUMP]
    assert findings[0].classification == LOW_END_GAUGE_JUMP
    assert findings[0].new_percent == 6.0


def test_jump_across_suspend_gap_is_resume_reconciliation(tmp_path: Path) -> None:
    db, _cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, False, "Discharging", 20.0, 6.0, wall_offset=0.0),
            _row(1, False, "Discharging", 18.0, 6.0, wall_offset=3600.0),
        ],
    )
    config = GaugeJumpConfig(suspend_gap_seconds=30.0)

    findings = analyze_session_jumps(db, "jump-session", config=config)

    assert [finding.event_type for finding in findings] == [RECOVERY_JUMP]
    assert findings[0].classification == AFTER_RESUME_RECONCILIATION
    assert findings[0].severity == "info"


def test_one_sample_after_ac_disconnect_is_downgraded(tmp_path: Path) -> None:
    db, _cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, True, "Not charging", 20.0, 0.0),
            _row(1, False, "Discharging", 19.0, 6.0),
        ],
    )

    findings = analyze_session_jumps(db, "jump-session")

    assert [finding.event_type for finding in findings] == [IMPOSSIBLE_ENERGY_DROP]
    assert findings[0].transition_event == "AC_DISCONNECTED"
    assert findings[0].severity == "info"


def test_jump_cli_outputs_table_and_json_export(tmp_path: Path, capsys: Any) -> None:
    _db, cfg = _write_jump_session(
        tmp_path,
        [
            _row(0, False, "Discharging", 20.0, 6.0),
            _row(1, False, "Discharging", 19.0, 6.0),
        ],
    )
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "analyze", "jumps", "jump-session"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "IMPOSSIBLE_ENERGY_DROP" in output
    assert "Expected max Wh" in output

    out_path = tmp_path / "jumps.json"
    rc = cli_main(
        [
            "--config",
            str(config_path),
            "analyze",
            "--jumps",
            "jump-session",
            "--format",
            "json",
            "--out",
            str(out_path),
        ]
    )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload[0]["event_type"] == IMPOSSIBLE_ENERGY_DROP


def _write_jump_session(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[BatteryDatabase, AuditorConfig]:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("jump-session", "jump session", cfg.to_json())
    for row in rows:
        db.insert_snapshot("jump-session", int(row["seq"]), _snapshot(**row), [])
    db.end_session("jump-session")
    return db, cfg


def _row(
    seq: int,
    ac_online: bool,
    status: str,
    energy_wh: float,
    power_w: float,
    *,
    wall_offset: float | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ac_online": ac_online,
        "status": status,
        "energy_wh": energy_wh,
        "power_w": power_w,
        "wall_offset": float(seq) if wall_offset is None else wall_offset,
    }


def _snapshot(
    seq: int,
    ac_online: bool,
    status: str,
    energy_wh: float,
    power_w: float,
    wall_offset: float,
) -> SystemSnapshot:
    wall_time = 1_700_000_000.0 + wall_offset
    return SystemSnapshot(
        wall_time=wall_time,
        monotonic_time=wall_time,
        power_supplies=[PowerSupplySnapshot(name="AC", type="Mains", online=ac_online)],
        batteries=[_battery("BAT0", status, energy_wh, power_w)],
    )


def _battery(name: str, status: str, energy_wh: float, power_w: float) -> BatterySnapshot:
    energy_full_wh = 50.0
    return BatterySnapshot(
        name=name,
        present=True,
        status=status,
        capacity_percent=(energy_wh / energy_full_wh) * 100.0,
        energy_now_uwh=int(energy_wh * 1_000_000),
        energy_full_uwh=int(energy_full_wh * 1_000_000),
        energy_full_design_uwh=int(50_000_000),
        power_now_uw=int(power_w * 1_000_000),
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
