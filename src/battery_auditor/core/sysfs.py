from __future__ import annotations

import os
import time
from pathlib import Path

from battery_auditor.core.models import BatterySnapshot, PowerSupplySnapshot, SystemSnapshot

BATTERY_FIELDS = {
    "present",
    "status",
    "capacity",
    "capacity_level",
    "energy_now",
    "energy_full",
    "energy_full_design",
    "power_now",
    "voltage_now",
    "voltage_min_design",
    "cycle_count",
    "technology",
    "manufacturer",
    "model_name",
    "serial_number",
    "charge_control_start_threshold",
    "charge_control_end_threshold",
    "charge_start_threshold",
    "charge_stop_threshold",
    "charge_behaviour",
    "type",
}

SUPPLY_FIELDS = {"type", "online"}


def read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def read_dir_fields(path: Path, fields: set[str] | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    candidates: list[Path]
    if fields is None:
        candidates = [p for p in path.iterdir() if p.is_file() or p.is_symlink()]
    else:
        candidates = [path / field for field in fields]
    for candidate in candidates:
        value = read_text_file(candidate)
        if value is not None:
            result[candidate.name] = value
    return result


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def parse_bool01(value: str | None) -> bool | None:
    parsed = parse_int(value)
    if parsed is None:
        return None
    return bool(parsed)


def sanitize_threshold(value: str | None) -> int | None:
    # Some drivers expose strings with a selected option like "[auto] inhibit-charge".
    return parse_int(value)


def discover_supplies(root: Path) -> list[Path]:
    try:
        return sorted([p for p in root.iterdir() if p.is_dir() or p.is_symlink()], key=lambda p: p.name)
    except (FileNotFoundError, PermissionError, OSError):
        return []


def read_battery(path: Path) -> BatterySnapshot:
    raw = read_dir_fields(path, BATTERY_FIELDS)
    return BatterySnapshot(
        name=path.name,
        present=parse_bool01(raw.get("present")),
        status=raw.get("status"),
        capacity_percent=parse_float(raw.get("capacity")),
        capacity_level=raw.get("capacity_level"),
        energy_now_uwh=parse_int(raw.get("energy_now")),
        energy_full_uwh=parse_int(raw.get("energy_full")),
        energy_full_design_uwh=parse_int(raw.get("energy_full_design")),
        power_now_uw=parse_int(raw.get("power_now")),
        voltage_now_uv=parse_int(raw.get("voltage_now")),
        voltage_min_design_uv=parse_int(raw.get("voltage_min_design")),
        cycle_count=parse_int(raw.get("cycle_count")),
        technology=raw.get("technology"),
        manufacturer=raw.get("manufacturer"),
        model_name=raw.get("model_name"),
        serial_number=raw.get("serial_number"),
        charge_control_start_threshold=sanitize_threshold(raw.get("charge_control_start_threshold")),
        charge_control_end_threshold=sanitize_threshold(raw.get("charge_control_end_threshold")),
        charge_start_threshold=sanitize_threshold(raw.get("charge_start_threshold")),
        charge_stop_threshold=sanitize_threshold(raw.get("charge_stop_threshold")),
        charge_behaviour=raw.get("charge_behaviour"),
        raw=raw,
    )


def read_power_supply(path: Path) -> PowerSupplySnapshot:
    raw = read_dir_fields(path, SUPPLY_FIELDS)
    return PowerSupplySnapshot(
        name=path.name,
        type=raw.get("type"),
        online=parse_bool01(raw.get("online")),
        raw=raw,
    )


def read_snapshot(root: Path = Path("/sys/class/power_supply")) -> SystemSnapshot:
    wall_start = time.time()
    mono_start = time.monotonic()
    power_supplies: list[PowerSupplySnapshot] = []
    batteries: list[BatterySnapshot] = []

    for path in discover_supplies(root):
        supply_type = read_text_file(path / "type")
        if (supply_type or "").lower() == "battery":
            batteries.append(read_battery(path))
        else:
            power_supplies.append(read_power_supply(path))

    snap = SystemSnapshot(
        wall_time=wall_start,
        monotonic_time=mono_start,
        power_supplies=power_supplies,
        batteries=batteries,
    )
    snap.metrics.sample_duration_ms = (time.monotonic() - mono_start) * 1000.0
    return snap


def read_process_metrics() -> tuple[int | None, float | None, float | None]:
    """Return rss KiB, user CPU seconds and system CPU seconds for this process."""
    rss_kib: int | None = None
    user_cpu: float | None = None
    system_cpu: float | None = None

    try:
        statm = Path("/proc/self/statm").read_text().split()
        if len(statm) >= 2:
            page_size = os.sysconf("SC_PAGE_SIZE")
            rss_kib = int(statm[1]) * page_size // 1024
    except (OSError, ValueError):
        pass

    try:
        stat = Path("/proc/self/stat").read_text().split()
        if len(stat) > 15:
            ticks = os.sysconf("SC_CLK_TCK")
            user_cpu = int(stat[13]) / ticks
            system_cpu = int(stat[14]) / ticks
    except (OSError, ValueError):
        pass

    return rss_kib, user_cpu, system_cpu
