from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "battery-auditor"


def _default_state_dir() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def _default_config_dir() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / APP_NAME
    return Path.home() / ".config" / APP_NAME


@dataclass(slots=True)
class TlpThresholdExpectation:
    start: int | None = None
    stop: int | None = None


@dataclass(slots=True)
class AuditorConfig:
    """Runtime configuration.

    The defaults are intentionally conservative. The collector does not call
    TLP, UPower, acpi, systemctl, sensors, journalctl, or other expensive
    commands in the hot path. It samples sysfs and writes compact rows.
    """

    data_dir: Path = field(default_factory=_default_state_dir)
    config_dir: Path = field(default_factory=_default_config_dir)
    db_path: Path | None = None
    sysfs_power_supply_dir: Path = Path("/sys/class/power_supply")

    interval_seconds: float = 2.0
    ui_refresh_seconds: float = 5.0
    heartbeat_seconds: float = 2.0
    sleep_monitor_enabled: bool = False
    sleep_monitor_backend: str = "logind"

    sqlite_synchronous: str = "NORMAL"
    sqlite_journal_mode: str = "WAL"
    sqlite_wal_autocheckpoint_pages: int = 200
    blackbox_flush_each_sample: bool = False

    low_total_percent: float = 10.0
    critical_total_percent: float = 5.0
    percent_jump_threshold: float = 5.0
    voltage_sag_percent_threshold: float = 6.0
    sample_delay_warn_factor: float = 2.5

    gauge_jump_absolute_tolerance_wh: float = 0.10
    gauge_jump_relative_tolerance: float = 3.0
    gauge_jump_low_end_percent: float = 25.0
    gauge_jump_transition_window_seconds: float = 5.0
    gauge_jump_suspend_gap_seconds: float | None = None
    relearn_min_absolute_change_wh: float = 0.25
    relearn_min_relative_change_percent: float = 1.0
    relearn_context_window_seconds: float = 3600.0
    relearn_resume_gap_seconds: float = 300.0
    model_low_end_percent: float = 25.0
    model_low_end_margin_fraction: float = 0.10
    model_global_critical_reserve_wh: float = 0.0
    model_relearn_stable_samples: int = 3
    model_eta_short_window_seconds: float = 60.0
    model_eta_medium_window_seconds: float = 300.0
    model_eta_long_window_seconds: float = 900.0
    model_min_eta_pairs: int = 2
    model_ac_transition_exclusion_seconds: float = 5.0
    model_suspend_gap_seconds: float = 120.0
    threshold_restore_on_resume: bool = False
    threshold_restore_on_mismatch: bool = False
    threshold_restore_command: str = "tlp"
    threshold_restore_require_confirmation: bool = True

    expected_thresholds: dict[str, TlpThresholdExpectation] = field(default_factory=dict)

    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return self.db_path.expanduser()
        return self.data_dir.expanduser() / "battery-auditor.sqlite3"

    def heartbeat_dir(self) -> Path:
        return self.data_dir.expanduser() / "heartbeats"

    def to_json(self) -> str:
        def convert(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, TlpThresholdExpectation):
                return asdict(value)
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            return value

        return json.dumps(convert(asdict(self)), ensure_ascii=False, sort_keys=True)


DEFAULT_CONFIG_PATHS = [
    Path("/etc/battery-auditor/config.toml"),
    _default_config_dir() / "config.toml",
]


def _coerce_path(value: Any) -> Path:
    return Path(str(value)).expanduser()


def load_config(paths: list[Path] | None = None) -> AuditorConfig:
    cfg = AuditorConfig()
    for path in paths or DEFAULT_CONFIG_PATHS:
        if not path.exists():
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        apply_config_dict(cfg, data)
    return cfg


def apply_config_dict(cfg: AuditorConfig, data: dict[str, Any]) -> None:
    general = data.get("general", {})
    if "data_dir" in general:
        cfg.data_dir = _coerce_path(general["data_dir"])
    if "db_path" in general:
        cfg.db_path = _coerce_path(general["db_path"])
    if "sysfs_power_supply_dir" in general:
        cfg.sysfs_power_supply_dir = _coerce_path(general["sysfs_power_supply_dir"])

    sampling = data.get("sampling", {})
    for key in (
        "interval_seconds",
        "ui_refresh_seconds",
        "heartbeat_seconds",
        "low_total_percent",
        "critical_total_percent",
        "percent_jump_threshold",
        "voltage_sag_percent_threshold",
        "sample_delay_warn_factor",
    ):
        if key in sampling:
            setattr(cfg, key, float(sampling[key]))

    sleep_monitor = data.get("sleep_monitor", {})
    if "enabled" in sleep_monitor:
        cfg.sleep_monitor_enabled = bool(sleep_monitor["enabled"])
    if "backend" in sleep_monitor:
        cfg.sleep_monitor_backend = str(sleep_monitor["backend"])

    analysis = data.get("analysis", {})
    for key in (
        "gauge_jump_absolute_tolerance_wh",
        "gauge_jump_relative_tolerance",
        "gauge_jump_low_end_percent",
        "gauge_jump_transition_window_seconds",
        "relearn_min_absolute_change_wh",
        "relearn_min_relative_change_percent",
        "relearn_context_window_seconds",
        "relearn_resume_gap_seconds",
        "model_low_end_percent",
        "model_low_end_margin_fraction",
        "model_global_critical_reserve_wh",
        "model_eta_short_window_seconds",
        "model_eta_medium_window_seconds",
        "model_eta_long_window_seconds",
        "model_ac_transition_exclusion_seconds",
        "model_suspend_gap_seconds",
    ):
        if key in analysis:
            setattr(cfg, key, float(analysis[key]))
    if "model_relearn_stable_samples" in analysis:
        cfg.model_relearn_stable_samples = int(analysis["model_relearn_stable_samples"])
    if "model_min_eta_pairs" in analysis:
        cfg.model_min_eta_pairs = int(analysis["model_min_eta_pairs"])
    if "gauge_jump_suspend_gap_seconds" in analysis:
        value = analysis["gauge_jump_suspend_gap_seconds"]
        cfg.gauge_jump_suspend_gap_seconds = None if value is None else float(value)

    sqlite = data.get("sqlite", {})
    if "synchronous" in sqlite:
        cfg.sqlite_synchronous = str(sqlite["synchronous"]).upper()
    if "journal_mode" in sqlite:
        cfg.sqlite_journal_mode = str(sqlite["journal_mode"]).upper()
    if "wal_autocheckpoint_pages" in sqlite:
        cfg.sqlite_wal_autocheckpoint_pages = int(sqlite["wal_autocheckpoint_pages"])
    if "blackbox_flush_each_sample" in sqlite:
        cfg.blackbox_flush_each_sample = bool(sqlite["blackbox_flush_each_sample"])

    thresholds = data.get("thresholds", {})
    if "restore_on_resume" in thresholds:
        cfg.threshold_restore_on_resume = bool(thresholds["restore_on_resume"])
    if "restore_on_mismatch" in thresholds:
        cfg.threshold_restore_on_mismatch = bool(thresholds["restore_on_mismatch"])
    if "restore_command" in thresholds:
        cfg.threshold_restore_command = str(thresholds["restore_command"])
    if "require_confirmation" in thresholds:
        cfg.threshold_restore_require_confirmation = bool(thresholds["require_confirmation"])

    expected = thresholds if thresholds else data.get("expected_thresholds", {})
    for battery, values in expected.items():
        if not isinstance(values, dict):
            continue
        cfg.expected_thresholds[str(battery)] = TlpThresholdExpectation(
            start=int(values["start"]) if values.get("start") is not None else None,
            stop=int(values["stop"]) if values.get("stop") is not None else None,
        )
