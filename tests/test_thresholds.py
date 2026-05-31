from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig, TlpThresholdExpectation, load_config
from battery_auditor.core.collector import BatteryCollector
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot
from battery_auditor.core.sleep_monitor import (
    RESUMED,
    SleepMonitor,
    SleepMonitorEvent,
    SleepMonitorUnavailable,
    make_sleep_monitor_event,
)
from battery_auditor.core.thresholds import (
    STATUS_MISMATCH,
    STATUS_OK,
    STATUS_UNKNOWN,
    THRESHOLD_MISMATCH,
    THRESHOLD_RESTORE_DRY_RUN,
    THRESHOLD_RESTORE_FAILED,
    THRESHOLD_RESTORE_SUCCESS,
    THRESHOLD_RESTORED,
    analyze_session_thresholds,
    restore_thresholds,
    samples_from_rows,
    threshold_event_findings,
)
from battery_auditor.core.tlp import CommandResult

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


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


def test_threshold_restore_dry_run_returns_planned_commands_without_success(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)

    results = restore_thresholds(cfg, dry_run=True)

    assert {result.event_type for result in results} == {THRESHOLD_RESTORE_DRY_RUN}
    assert THRESHOLD_RESTORE_SUCCESS not in {result.event_type for result in results}
    assert ["sudo", "tlp", "setcharge", "75", "80", "BAT0"] in [result.command for result in results]
    assert ["sudo", "tlp", "setcharge", "70", "85", "BAT1"] in [result.command for result in results]


def test_threshold_restore_command_uses_configured_bat0_values(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    calls: list[list[str]] = []

    results = restore_thresholds(cfg, batteries=["BAT0"], runner=_recording_runner(calls))

    assert calls == [["sudo", "tlp", "setcharge", "75", "80", "BAT0"]]
    assert results[-1].event_type == THRESHOLD_RESTORE_SUCCESS


def test_threshold_restore_command_uses_configured_bat1_values(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    calls: list[list[str]] = []

    results = restore_thresholds(cfg, batteries=["BAT1"], runner=_recording_runner(calls))

    assert calls == [["sudo", "tlp", "setcharge", "70", "85", "BAT1"]]
    assert results[-1].event_type == THRESHOLD_RESTORE_SUCCESS


def test_threshold_restore_failure_produces_failed_event(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)

    results = restore_thresholds(
        cfg,
        batteries=["BAT0"],
        runner=lambda command: CommandResult(command, 1, "", "sudo refused"),
    )

    assert results[-1].event_type == THRESHOLD_RESTORE_FAILED
    assert results[-1].reason == "sudo refused"


def test_threshold_cli_restore_dry_run_requires_no_command_execution(tmp_path: Path, capsys: Any) -> None:
    cfg = _restore_cfg(tmp_path)
    config_path = _write_config(tmp_path, cfg)

    rc = cli_main(["--config", str(config_path), "thresholds", "restore", "BAT0", "--dry-run"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "THRESHOLD_RESTORE_DRY_RUN" in output
    assert "sudo tlp setcharge 75 80 BAT0" in output
    assert THRESHOLD_RESTORE_SUCCESS not in output


def test_threshold_mismatch_auto_restore_requires_opt_in(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    cfg.sysfs_power_supply_dir = FIXTURE
    cfg.threshold_restore_on_mismatch = False
    calls: list[list[str]] = []

    BatteryCollector(cfg, threshold_restore_runner=_recording_runner(calls)).run(
        name="no-auto-restore",
        interval_seconds=0.01,
        duration_seconds=0.02,
        recover_open_sessions=False,
    )

    assert calls == []


def test_threshold_mismatch_auto_restore_runs_when_enabled(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    cfg.sysfs_power_supply_dir = FIXTURE
    cfg.threshold_restore_on_mismatch = True
    calls: list[list[str]] = []

    BatteryCollector(cfg, threshold_restore_runner=_recording_runner(calls)).run(
        name="auto-restore",
        interval_seconds=0.01,
        duration_seconds=0.02,
        recover_open_sessions=False,
    )

    assert ["sudo", "tlp", "setcharge", "75", "80", "BAT0"] in calls
    assert ["sudo", "tlp", "setcharge", "70", "85", "BAT1"] in calls


def test_resume_auto_restore_requires_opt_in(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    cfg.sysfs_power_supply_dir = FIXTURE
    cfg.threshold_restore_on_resume = False
    calls: list[list[str]] = []

    BatteryCollector(
        cfg,
        sleep_monitor_factory=_resume_monitor,
        threshold_restore_runner=_recording_runner(calls),
    ).run(
        name="resume-no-auto-restore",
        interval_seconds=10.0,
        duration_seconds=0.03,
        recover_open_sessions=False,
    )

    assert calls == []


def test_resume_auto_restore_runs_when_enabled(tmp_path: Path) -> None:
    cfg = _restore_cfg(tmp_path)
    cfg.sysfs_power_supply_dir = FIXTURE
    cfg.threshold_restore_on_resume = True
    calls: list[list[str]] = []

    BatteryCollector(
        cfg,
        sleep_monitor_factory=_resume_monitor,
        threshold_restore_runner=_recording_runner(calls),
    ).run(
        name="resume-auto-restore",
        interval_seconds=10.0,
        duration_seconds=0.03,
        recover_open_sessions=False,
    )

    assert ["sudo", "tlp", "setcharge", "75", "80", "BAT0"] in calls
    assert ["sudo", "tlp", "setcharge", "70", "85", "BAT1"] in calls


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


def _restore_cfg(tmp_path: Path) -> AuditorConfig:
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3")
    cfg.expected_thresholds["BAT0"] = TlpThresholdExpectation(start=75, stop=80)
    cfg.expected_thresholds["BAT1"] = TlpThresholdExpectation(start=70, stop=85)
    return cfg


def _recording_runner(calls: list[list[str]]) -> Callable[[list[str]], CommandResult]:
    def runner(command: list[str]) -> CommandResult:
        calls.append(command)
        return CommandResult(command, 0, "ok", "")

    return runner


def _resume_monitor(callback: Callable[[SleepMonitorEvent], None]) -> SleepMonitor:
    return _ResumeMonitor(callback)


class _ResumeMonitor(SleepMonitor):
    def __init__(self, callback: Callable[[SleepMonitorEvent], None]) -> None:
        self.callback = callback

    def start(self) -> SleepMonitorUnavailable | None:
        self.callback(make_sleep_monitor_event(RESUMED))
        return None

    def stop(self) -> None:
        return None


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
