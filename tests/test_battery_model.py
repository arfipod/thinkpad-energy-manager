from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig
from battery_auditor.core.battery_model import (
    ACTIVE_DISCHARGING,
    RESERVE,
    BatteryModelConfig,
    estimate_session,
)
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot


def test_stable_discharge_at_six_watts_with_twelve_wh_usable_has_two_hour_eta(tmp_path: Path) -> None:
    rows = [
        _row(seq, False, [("BAT0", "Discharging", 12.4 - (seq * 0.1), 24.0, 6.0)])
        for seq in range(5)
    ]
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg)

    assert round(estimate.usable_energy_wh or 0.0, 1) == 12.0
    assert estimate.eta_seconds_nominal is not None
    assert abs(estimate.eta_seconds_nominal - 7200.0) < 1.0
    assert estimate.confidence >= 0.8


def test_ac_connected_session_returns_no_discharge_eta_and_low_confidence(tmp_path: Path) -> None:
    rows = [
        _row(seq, True, [("BAT0", "Charging", 12.0 + (seq * 0.1), 24.0, 6.0)])
        for seq in range(4)
    ]
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg)

    assert estimate.eta_seconds_nominal is None
    assert estimate.confidence <= 0.45
    assert any("AC-connected" in reason for reason in estimate.explanation)


def test_low_end_bat1_jump_reduces_confidence(tmp_path: Path) -> None:
    rows = [
        _row(0, False, [("BAT1", "Discharging", 10.0, 50.0, 6.0)]),
        _row(1, False, [("BAT1", "Discharging", 3.0, 50.0, 6.0)], wall_offset=1.0),
    ]
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg)
    battery = estimate.batteries[0]

    assert battery.battery_name == "BAT1"
    assert battery.low_end_confidence < 1.0
    assert battery.gauge_confidence < 1.0
    assert estimate.confidence < 0.8
    assert any("low-end gauge jump" in reason for reason in estimate.explanation)


def test_energy_full_relearn_uses_newest_stable_full_capacity(tmp_path: Path) -> None:
    rows = [
        _row(0, False, [("BAT0", "Discharging", 10.0, 18.28, 6.0)]),
        _row(1, False, [("BAT0", "Discharging", 10.0, 19.91, 6.0)]),
        _row(2, False, [("BAT0", "Discharging", 9.9, 19.91, 6.0)]),
        _row(3, False, [("BAT0", "Discharging", 9.8, 19.91, 6.0)]),
    ]
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg)

    assert estimate.batteries[0].learned_full_wh == 19.91
    assert estimate.learned_pack_full_wh == 19.91
    assert any("energy_full relearn detected" in reason for reason in estimate.explanation)


def test_suspend_gap_is_excluded_from_consumption_estimate(tmp_path: Path) -> None:
    rows = [
        _row(0, False, [("BAT0", "Discharging", 12.0, 24.0, 6.0)], wall_offset=0.0),
        _row(1, False, [("BAT0", "Discharging", 10.0, 24.0, 6.0)], wall_offset=3600.0),
    ]
    cfg_override = BatteryModelConfig(suspend_gap_seconds=120.0)
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg, config=cfg_override)

    assert estimate.eta_seconds_nominal is None
    assert any("Not enough recent discharge-only samples" in reason for reason in estimate.explanation)


def test_phase_routing_marks_bat1_active_and_bat0_reserve(tmp_path: Path) -> None:
    rows = [
        _row(
            seq,
            False,
            [
                ("BAT0", "Not charging", 12.0, 24.0, 0.0),
                ("BAT1", "Discharging", 12.4 - (seq * 0.1), 24.0, 6.0),
            ],
        )
        for seq in range(5)
    ]
    db, cfg = _write_model_session(tmp_path, rows)

    estimate = estimate_session(db, "model-session", cfg)
    roles = {battery.battery_name: battery.active_role for battery in estimate.batteries}

    assert roles["BAT1"] == ACTIVE_DISCHARGING
    assert roles["BAT0"] == RESERVE


def test_estimate_cli_text_and_json(tmp_path: Path, capsys: Any) -> None:
    rows = [
        _row(seq, False, [("BAT0", "Discharging", 12.4 - (seq * 0.1), 24.0, 6.0)])
        for seq in range(5)
    ]
    _db, cfg = _write_model_session(tmp_path, rows)
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "estimate", "--session", "model-session"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "Effective percent" in output
    assert "ETA nominal" in output
    assert "Confidence" in output

    rc = cli_main(["--config", str(config_path), "estimate", "--session", "model-session", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eta_seconds_nominal"] is not None
    assert payload["batteries"][0]["active_role"] == ACTIVE_DISCHARGING


def _write_model_session(tmp_path: Path, rows: list[dict[str, Any]]) -> tuple[BatteryDatabase, AuditorConfig]:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("model-session", "model session", cfg.to_json())
    for row in rows:
        db.insert_snapshot("model-session", int(row["seq"]), _snapshot(**row), [])
    db.end_session("model-session")
    return db, cfg


def _row(
    seq: int,
    ac_online: bool,
    batteries: list[tuple[str, str, float, float, float]],
    *,
    wall_offset: float | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ac_online": ac_online,
        "batteries": batteries,
        "wall_offset": float(seq * 60) if wall_offset is None else wall_offset,
    }


def _snapshot(
    seq: int,
    ac_online: bool,
    batteries: list[tuple[str, str, float, float, float]],
    wall_offset: float,
) -> SystemSnapshot:
    wall_time = 1_700_000_000.0 + wall_offset
    return SystemSnapshot(
        wall_time=wall_time,
        monotonic_time=wall_time,
        power_supplies=[PowerSupplySnapshot(name="AC", type="Mains", online=ac_online)],
        batteries=[
            _battery(name, status, energy_now_wh, energy_full_wh, power_w)
            for name, status, energy_now_wh, energy_full_wh, power_w in batteries
        ],
    )


def _battery(
    name: str,
    status: str,
    energy_now_wh: float,
    energy_full_wh: float,
    power_w: float,
) -> BatterySnapshot:
    return BatterySnapshot(
        name=name,
        present=True,
        status=status,
        capacity_percent=(energy_now_wh / energy_full_wh) * 100.0,
        energy_now_uwh=int(energy_now_wh * 1_000_000),
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
