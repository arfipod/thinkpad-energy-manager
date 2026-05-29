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

    sqlite_synchronous: str = "NORMAL"
    sqlite_wal_autocheckpoint_pages: int = 200
    blackbox_flush_each_sample: bool = False

    low_total_percent: float = 10.0
    critical_total_percent: float = 5.0
    percent_jump_threshold: float = 5.0
    voltage_sag_percent_threshold: float = 6.0
    sample_delay_warn_factor: float = 2.5

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

    sqlite = data.get("sqlite", {})
    if "synchronous" in sqlite:
        cfg.sqlite_synchronous = str(sqlite["synchronous"]).upper()
    if "wal_autocheckpoint_pages" in sqlite:
        cfg.sqlite_wal_autocheckpoint_pages = int(sqlite["wal_autocheckpoint_pages"])
    if "blackbox_flush_each_sample" in sqlite:
        cfg.blackbox_flush_each_sample = bool(sqlite["blackbox_flush_each_sample"])

    expected = data.get("expected_thresholds", {})
    for battery, values in expected.items():
        cfg.expected_thresholds[str(battery)] = TlpThresholdExpectation(
            start=int(values["start"]) if values.get("start") is not None else None,
            stop=int(values["stop"]) if values.get("stop") is not None else None,
        )
