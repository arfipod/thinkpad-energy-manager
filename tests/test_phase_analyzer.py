from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot
from battery_auditor.core.phase_analyzer import (
    PHASE_CHARGE_BAT1,
    PHASE_DISCHARGE_BAT0,
    PHASE_DISCHARGE_BAT1,
    analyze_session_phases,
)


def test_dual_battery_phases_and_energy_deltas(tmp_path: Path) -> None:
    db, _cfg = _write_phase_session(tmp_path)

    phases = analyze_session_phases(db, "phase-session")

    assert [phase.classification for phase in phases] == [
        PHASE_DISCHARGE_BAT0,
        PHASE_CHARGE_BAT1,
        PHASE_DISCHARGE_BAT1,
    ]
    assert [phase.active_discharging_battery for phase in phases] == ["BAT0", None, "BAT1"]
    assert [phase.active_charging_battery for phase in phases] == [None, "BAT1", None]
    assert phases[0].energy_delta_wh["BAT0"] == -3.0
    assert phases[0].energy_delta_wh["BAT1"] == 0.0
    assert phases[1].energy_delta_wh["BAT0"] == 0.0
    assert phases[1].energy_delta_wh["BAT1"] == 3.0
    assert phases[2].energy_delta_wh["BAT0"] == 0.0
    assert phases[2].energy_delta_wh["BAT1"] == -3.0
    assert phases[0].total_energy_delta_wh == -3.0
    assert phases[1].total_energy_delta_wh == 3.0
    assert phases[2].total_energy_delta_wh == -3.0


def test_one_sample_status_glitch_does_not_create_phase(tmp_path: Path) -> None:
    db, _cfg = _write_phase_session(tmp_path)

    phases = analyze_session_phases(db, "phase-session")

    assert len(phases) == 3
    assert phases[0].start_seq == 0
    assert phases[0].end_seq == 3
    assert phases[0].sample_count == 4
    assert phases[0].battery_states["BAT1"]["status_counts"]["discharging"] == 1


def test_phase_cli_table_and_json_export(tmp_path: Path, capsys: Any) -> None:
    _db, cfg = _write_phase_session(tmp_path)
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "analyze", "phases", "phase-session"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "DISCHARGE_BAT0" in output
    assert "CHARGE_BAT1" in output

    out_path = tmp_path / "phases.json"
    rc = cli_main(
        [
            "--config",
            str(config_path),
            "analyze",
            "--phases",
            "phase-session",
            "--format",
            "json",
            "--out",
            str(out_path),
        ]
    )

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert [phase["classification"] for phase in payload] == [
        PHASE_DISCHARGE_BAT0,
        PHASE_CHARGE_BAT1,
        PHASE_DISCHARGE_BAT1,
    ]


def _write_phase_session(tmp_path: Path) -> tuple[BatteryDatabase, AuditorConfig]:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("phase-session", "phase session", cfg.to_json())
    rows = [
        # AC off: BAT0 discharges while BAT1 is flat. Seq 2 has a one-sample BAT1 status glitch.
        (0, False, "Discharging", "Not charging", 20.0, 25.0, 60.0, 0.0),
        (1, False, "Discharging", "Not charging", 19.0, 25.0, 60.0, 0.0),
        (2, False, "Discharging", "Discharging", 18.0, 25.0, 60.0, 5.0),
        (3, False, "Discharging", "Not charging", 17.0, 25.0, 60.0, 0.0),
        # AC on: BAT1 charges while BAT0 is flat.
        (4, True, "Not charging", "Charging", 17.0, 25.0, 0.0, 60.0),
        (5, True, "Not charging", "Charging", 17.0, 26.0, 0.0, 60.0),
        (6, True, "Not charging", "Charging", 17.0, 27.0, 0.0, 60.0),
        (7, True, "Not charging", "Charging", 17.0, 28.0, 0.0, 60.0),
        # AC off again: BAT1 discharges while BAT0 is flat.
        (8, False, "Not charging", "Discharging", 17.0, 28.0, 0.0, 60.0),
        (9, False, "Not charging", "Discharging", 17.0, 27.0, 0.0, 60.0),
        (10, False, "Not charging", "Discharging", 17.0, 26.0, 0.0, 60.0),
        (11, False, "Not charging", "Discharging", 17.0, 25.0, 0.0, 60.0),
    ]
    for seq, ac_online, bat0_status, bat1_status, bat0_wh, bat1_wh, bat0_power_w, bat1_power_w in rows:
        db.insert_snapshot(
            "phase-session",
            seq,
            _snapshot(seq, ac_online, bat0_status, bat1_status, bat0_wh, bat1_wh, bat0_power_w, bat1_power_w),
            [],
        )
    db.end_session("phase-session")
    return db, cfg


def _snapshot(
    seq: int,
    ac_online: bool,
    bat0_status: str,
    bat1_status: str,
    bat0_wh: float,
    bat1_wh: float,
    bat0_power_w: float,
    bat1_power_w: float,
) -> SystemSnapshot:
    wall_time = 1_700_000_000.0 + (seq * 60.0)
    return SystemSnapshot(
        wall_time=wall_time,
        monotonic_time=wall_time,
        power_supplies=[PowerSupplySnapshot(name="AC", type="Mains", online=ac_online)],
        batteries=[
            _battery("BAT0", bat0_status, bat0_wh, bat0_power_w),
            _battery("BAT1", bat1_status, bat1_wh, bat1_power_w),
        ],
    )


def _battery(name: str, status: str, energy_wh: float, power_w: float) -> BatterySnapshot:
    energy_full_wh = 30.0
    return BatterySnapshot(
        name=name,
        present=True,
        status=status,
        capacity_percent=(energy_wh / energy_full_wh) * 100.0,
        energy_now_uwh=int(energy_wh * 1_000_000),
        energy_full_uwh=int(energy_full_wh * 1_000_000),
        energy_full_design_uwh=30_000_000,
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
