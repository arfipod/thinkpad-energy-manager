from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

Micro = int


def wall_iso_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone().isoformat(timespec="seconds")


def percent(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100.0


@dataclass(slots=True)
class PowerSupplySnapshot:
    name: str
    type: str | None = None
    online: bool | None = None
    raw: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class BatterySnapshot:
    name: str
    present: bool | None = None
    status: str | None = None
    capacity_percent: float | None = None
    capacity_level: str | None = None
    energy_now_uwh: Micro | None = None
    energy_full_uwh: Micro | None = None
    energy_full_design_uwh: Micro | None = None
    power_now_uw: Micro | None = None
    voltage_now_uv: Micro | None = None
    voltage_min_design_uv: Micro | None = None
    cycle_count: int | None = None
    technology: str | None = None
    manufacturer: str | None = None
    model_name: str | None = None
    serial_number: str | None = None
    charge_control_start_threshold: int | None = None
    charge_control_end_threshold: int | None = None
    charge_start_threshold: int | None = None
    charge_stop_threshold: int | None = None
    charge_behaviour: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def computed_percent(self) -> float | None:
        return percent(self.energy_now_uwh, self.energy_full_uwh)

    @property
    def health_percent(self) -> float | None:
        return percent(self.energy_full_uwh, self.energy_full_design_uwh)

    @property
    def energy_now_wh(self) -> float | None:
        if self.energy_now_uwh is None:
            return None
        return self.energy_now_uwh / 1_000_000.0

    @property
    def energy_full_wh(self) -> float | None:
        if self.energy_full_uwh is None:
            return None
        return self.energy_full_uwh / 1_000_000.0

    @property
    def power_now_w(self) -> float | None:
        if self.power_now_uw is None:
            return None
        return self.power_now_uw / 1_000_000.0

    @property
    def voltage_now_v(self) -> float | None:
        if self.voltage_now_uv is None:
            return None
        return self.voltage_now_uv / 1_000_000.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class CollectorMetrics:
    sample_duration_ms: float | None = None
    db_write_duration_ms: float | None = None
    collector_rss_kib: int | None = None
    collector_user_cpu_seconds: float | None = None
    collector_system_cpu_seconds: float | None = None
    loop_delay_ms: float | None = None
    system_cpu_percent: float | None = None
    system_load_1m: float | None = None
    system_memory_total_kib: int | None = None
    system_memory_available_kib: int | None = None
    system_memory_used_percent: float | None = None
    system_disk_read_bytes_per_second: float | None = None
    system_disk_write_bytes_per_second: float | None = None


@dataclass(slots=True)
class SystemSnapshot:
    wall_time: float
    monotonic_time: float
    power_supplies: list[PowerSupplySnapshot]
    batteries: list[BatterySnapshot]
    metrics: CollectorMetrics = field(default_factory=CollectorMetrics)

    @property
    def wall_iso(self) -> str:
        return wall_iso_from_timestamp(self.wall_time)

    @property
    def ac_online(self) -> bool | None:
        mains = [s for s in self.power_supplies if (s.type or "").lower() in {"mains", "usb", "usb_c"}]
        if not mains:
            return None
        return any(s.online is True for s in mains)

    @property
    def total_energy_now_uwh(self) -> int | None:
        values = [b.energy_now_uwh for b in self.batteries if b.present is not False and b.energy_now_uwh is not None]
        if not values:
            return None
        return int(sum(values))

    @property
    def total_energy_full_uwh(self) -> int | None:
        values = [b.energy_full_uwh for b in self.batteries if b.present is not False and b.energy_full_uwh is not None]
        if not values:
            return None
        return int(sum(values))

    @property
    def total_energy_full_design_uwh(self) -> int | None:
        values = [b.energy_full_design_uwh for b in self.batteries if b.present is not False and b.energy_full_design_uwh is not None]
        if not values:
            return None
        return int(sum(values))

    @property
    def total_power_now_uw(self) -> int | None:
        values = [b.power_now_uw for b in self.batteries if b.present is not False and b.power_now_uw is not None]
        if not values:
            return None
        return int(sum(values))

    @property
    def total_computed_percent(self) -> float | None:
        return percent(self.total_energy_now_uwh, self.total_energy_full_uwh)

    @property
    def total_health_percent(self) -> float | None:
        return percent(self.total_energy_full_uwh, self.total_energy_full_design_uwh)

    @property
    def active_batteries(self) -> list[str]:
        return [b.name for b in self.batteries if (b.status or "").lower() in {"charging", "discharging"} and (b.power_now_uw or 0) > 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_time": self.wall_time,
            "wall_iso": self.wall_iso,
            "monotonic_time": self.monotonic_time,
            "ac_online": self.ac_online,
            "total_energy_now_uwh": self.total_energy_now_uwh,
            "total_energy_full_uwh": self.total_energy_full_uwh,
            "total_energy_full_design_uwh": self.total_energy_full_design_uwh,
            "total_power_now_uw": self.total_power_now_uw,
            "total_computed_percent": self.total_computed_percent,
            "total_health_percent": self.total_health_percent,
            "power_supplies": [asdict(s) for s in self.power_supplies],
            "batteries": [asdict(b) | {"computed_percent": b.computed_percent, "health_percent": b.health_percent} for b in self.batteries],
            "metrics": asdict(self.metrics),
        }


@dataclass(slots=True)
class Event:
    event_type: str
    severity: str
    message: str
    battery_name: str | None = None
    sample_id: int | None = None
    wall_time: float | None = None
    monotonic_time: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def details_json(self) -> str:
        return json.dumps(self.details, ensure_ascii=False, sort_keys=True)
