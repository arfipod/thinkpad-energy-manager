from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from battery_auditor.core.database import BatteryDatabase


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    sample_count: int
    started_at_iso: str | None
    ended_at_iso: str | None
    ended_reason: str | None
    probable_power_loss: bool
    duration_seconds: float | None
    per_battery: dict[str, dict[str, Any]]
    total: dict[str, Any]
    event_counts: dict[str, int]


def summarize_session(db: BatteryDatabase, session_id: str) -> SessionSummary:
    session = db.get_session(session_id)
    if session is None:
        raise ValueError(f"Unknown session: {session_id}")
    rows = db.fetch_session_series(session_id)
    events = db.fetch_events(session_id, limit=10_000)

    per_battery_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    total_percent: list[float] = []
    total_power: list[float] = []
    system_cpu: list[float] = []
    system_memory: list[float] = []
    system_disk_read: list[float] = []
    system_disk_write: list[float] = []
    first_time: float | None = None
    last_time: float | None = None

    for row in rows:
        first_time = row["wall_time"] if first_time is None else min(first_time, row["wall_time"])
        last_time = row["wall_time"] if last_time is None else max(last_time, row["wall_time"])
        b = str(row["battery_name"])
        for key in ("capacity_percent", "computed_percent", "health_percent"):
            value = row[key]
            if value is not None:
                per_battery_values[b][key].append(float(value))
        for key in ("energy_now_uwh", "power_now_uw", "voltage_now_uv"):
            value = row[key]
            if value is not None:
                per_battery_values[b][key].append(float(value))
        if row["total_computed_percent"] is not None:
            total_percent.append(float(row["total_computed_percent"]))
        if row["total_power_now_uw"] is not None:
            total_power.append(float(row["total_power_now_uw"]))
        if row["system_cpu_percent"] is not None:
            system_cpu.append(float(row["system_cpu_percent"]))
        if row["system_memory_used_percent"] is not None:
            system_memory.append(float(row["system_memory_used_percent"]))
        if row["system_disk_read_bytes_per_second"] is not None:
            system_disk_read.append(float(row["system_disk_read_bytes_per_second"]))
        if row["system_disk_write_bytes_per_second"] is not None:
            system_disk_write.append(float(row["system_disk_write_bytes_per_second"]))

    per_battery: dict[str, dict[str, Any]] = {}
    for battery, values in per_battery_values.items():
        per_battery[battery] = {}
        for key, series in values.items():
            if not series:
                continue
            per_battery[battery][key] = {
                "first": series[0],
                "last": series[-1],
                "min": min(series),
                "max": max(series),
                "mean": mean(series),
            }

    event_counts: dict[str, int] = defaultdict(int)
    for event in events:
        event_counts[str(event["event_type"])] += 1

    duration = (last_time - first_time) if first_time is not None and last_time is not None else None
    return SessionSummary(
        session_id=session_id,
        sample_count=int(session["sample_count"]),
        started_at_iso=session["started_at_iso"],
        ended_at_iso=session["ended_at_iso"],
        ended_reason=session["ended_reason"],
        probable_power_loss=bool(session["probable_power_loss"]),
        duration_seconds=duration,
        per_battery=per_battery,
        total={
            "computed_percent": stats(total_percent),
            "power_now_uw": stats(total_power),
            "system_cpu_percent": stats(system_cpu),
            "system_memory_used_percent": stats(system_memory),
            "system_disk_read_bytes_per_second": stats(system_disk_read),
            "system_disk_write_bytes_per_second": stats(system_disk_write),
        },
        event_counts=dict(sorted(event_counts.items())),
    )


def stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
    }


def summary_to_text(summary: SessionSummary) -> str:
    lines = [
        f"Session: {summary.session_id}",
        f"Started: {summary.started_at_iso}",
        f"Ended: {summary.ended_at_iso or 'open'} ({summary.ended_reason or 'running'})",
        f"Samples: {summary.sample_count}",
        f"Duration: {summary.duration_seconds:.1f}s" if summary.duration_seconds is not None else "Duration: n/a",
        f"Probable power loss: {'yes' if summary.probable_power_loss else 'no'}",
        "",
        "Per battery:",
    ]
    for battery, values in sorted(summary.per_battery.items()):
        lines.append(f"  {battery}:")
        for key, data in values.items():
            lines.append(
                "    "
                + f"{key}: first={data['first']:.3f} last={data['last']:.3f} "
                + f"min={data['min']:.3f} max={data['max']:.3f} mean={data['mean']:.3f}"
            )
    lines.append("")
    lines.append("Events:")
    if summary.event_counts:
        for event_type, count in summary.event_counts.items():
            lines.append(f"  {event_type}: {count}")
    else:
        lines.append("  none")
    return "\n".join(lines)


def export_session_csv(db: BatteryDatabase, session_id: str, output: Path) -> None:
    rows = db.export_rows(session_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as fh:
        if not rows:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_session_json(db: BatteryDatabase, session_id: str, output: Path) -> None:
    rows = db.export_rows(session_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
