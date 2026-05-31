from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import Event

ENERGY_FULL_RELEARN = "ENERGY_FULL_RELEARN"
ENERGY_FULL_DESIGN_CHANGE = "ENERGY_FULL_DESIGN_CHANGE"

AFTER_DEEP_DISCHARGE = "AFTER_DEEP_DISCHARGE"
AFTER_FULL_CHARGE = "AFTER_FULL_CHARGE"
AFTER_RESUME = "AFTER_RESUME"
UNKNOWN = "UNKNOWN"

_RESUME_EVENTS = {"SESSION_RESUMED", "PROBABLE_POWER_LOSS"}


@dataclass(frozen=True, slots=True)
class RelearnConfig:
    min_absolute_change_wh: float = 0.25
    min_relative_change_percent: float = 1.0
    context_window_seconds: float = 3600.0
    resume_gap_seconds: float = 300.0
    deep_discharge_percent: float = 10.0
    full_charge_percent: float = 98.0

    @classmethod
    def from_auditor_config(cls, cfg: AuditorConfig) -> "RelearnConfig":
        return cls(
            min_absolute_change_wh=cfg.relearn_min_absolute_change_wh,
            min_relative_change_percent=cfg.relearn_min_relative_change_percent,
            context_window_seconds=cfg.relearn_context_window_seconds,
            resume_gap_seconds=cfg.relearn_resume_gap_seconds,
            deep_discharge_percent=cfg.low_total_percent,
        )


@dataclass(frozen=True, slots=True)
class RelearnBatterySample:
    name: str
    status: str | None
    computed_percent: float | None
    capacity_percent: float | None
    energy_now_wh: float | None
    energy_full_wh: float | None
    energy_full_design_wh: float | None
    health_percent: float | None


@dataclass(frozen=True, slots=True)
class RelearnSample:
    sample_id: int | None
    seq: int
    wall_time: float
    wall_iso: str
    monotonic_time: float
    ac_online: bool | None
    batteries: dict[str, RelearnBatterySample]


@dataclass(frozen=True, slots=True)
class RelearnContextEvent:
    wall_time: float | None
    event_type: str
    battery_name: str | None


@dataclass(frozen=True, slots=True)
class RelearnFinding:
    session_id: str
    event_type: str
    severity: str
    battery_name: str
    wall_time: float
    wall_iso: str
    monotonic_time: float
    sample_id: int | None
    previous_seq: int
    current_seq: int
    old_energy_full_wh: float | None
    new_energy_full_wh: float | None
    delta_wh: float | None
    relative_change_percent: float | None
    old_energy_full_design_wh: float | None
    new_energy_full_design_wh: float | None
    design_delta_wh: float | None
    old_health_percent: float | None
    new_health_percent: float | None
    health_delta_points: float | None
    likely_cause: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_event(self) -> Event:
        return Event(
            event_type=self.event_type,
            severity=self.severity,
            message=self.message,
            battery_name=self.battery_name,
            sample_id=self.sample_id,
            wall_time=self.wall_time,
            monotonic_time=self.monotonic_time,
            details=self.to_dict(),
        )

    @property
    def message(self) -> str:
        if self.event_type == ENERGY_FULL_DESIGN_CHANGE:
            return (
                f"{self.battery_name}: reported design full capacity changed from "
                f"{_format_optional_float(self.old_energy_full_design_wh, precision=2)} Wh to "
                f"{_format_optional_float(self.new_energy_full_design_wh, precision=2)} Wh."
            )
        return (
            f"{self.battery_name}: reported full capacity changed from "
            f"{_format_optional_float(self.old_energy_full_wh, precision=2)} Wh to "
            f"{_format_optional_float(self.new_energy_full_wh, precision=2)} Wh. "
            "This does not mean the battery physically recovered; it means the reported full capacity changed."
        )


def analyze_session_relearn(
    db: BatteryDatabase,
    session_id: str,
    *,
    config: RelearnConfig | None = None,
) -> list[RelearnFinding]:
    if db.get_session(session_id) is None:
        raise ValueError(f"Unknown session: {session_id}")
    samples = samples_from_rows(db.fetch_session_series(session_id))
    events = context_events_from_rows(db.fetch_events(session_id, limit=10_000))
    return analyze_samples_relearn(session_id, samples, events=events, config=config)


def analyze_samples_relearn(
    session_id: str,
    samples: list[RelearnSample],
    *,
    events: list[RelearnContextEvent] | None = None,
    config: RelearnConfig | None = None,
) -> list[RelearnFinding]:
    cfg = config or RelearnConfig()
    context_events = events or []
    findings: list[RelearnFinding] = []
    for index in range(1, len(samples)):
        previous = samples[index - 1]
        current = samples[index]
        for name in sorted(set(previous.batteries) & set(current.batteries)):
            finding = _finding_for_pair(session_id, samples, index, name, cfg, context_events)
            if finding is not None:
                findings.append(finding)
    return findings


def persist_relearn_events(db: BatteryDatabase, session_id: str, findings: list[RelearnFinding]) -> None:
    for finding in findings:
        db.insert_event(session_id, finding.to_event())


def samples_from_rows(rows: list[Any]) -> list[RelearnSample]:
    builders: dict[int, dict[str, Any]] = {}
    for row in rows:
        seq = int(row["seq"])
        sample = builders.get(seq)
        if sample is None:
            sample_id = (
                int(row["sample_id"])
                if "sample_id" in row.keys() and row["sample_id"] is not None
                else None
            )
            sample = {
                "sample_id": sample_id,
                "seq": seq,
                "wall_time": float(row["wall_time"]),
                "wall_iso": str(row["wall_iso"]),
                "monotonic_time": float(row["monotonic_time"]),
                "ac_online": _row_bool(row["ac_online"]),
                "batteries": {},
            }
            builders[seq] = sample
        name = str(row["battery_name"])
        sample["batteries"][name] = RelearnBatterySample(
            name=name,
            status=str(row["status"]) if row["status"] is not None else None,
            computed_percent=_row_float(row["computed_percent"]),
            capacity_percent=_row_float(row["capacity_percent"]),
            energy_now_wh=_micro_to_unit(row["energy_now_uwh"]),
            energy_full_wh=_micro_to_unit(row["energy_full_uwh"]),
            energy_full_design_wh=_micro_to_unit(row["energy_full_design_uwh"]),
            health_percent=_row_float(row["health_percent"]),
        )
    return [
        RelearnSample(
            sample_id=builders[seq]["sample_id"],
            seq=builders[seq]["seq"],
            wall_time=builders[seq]["wall_time"],
            wall_iso=builders[seq]["wall_iso"],
            monotonic_time=builders[seq]["monotonic_time"],
            ac_online=builders[seq]["ac_online"],
            batteries=builders[seq]["batteries"],
        )
        for seq in sorted(builders)
    ]


def context_events_from_rows(rows: list[Any]) -> list[RelearnContextEvent]:
    return [
        RelearnContextEvent(
            wall_time=_row_float(row["wall_time"]),
            event_type=str(row["event_type"]),
            battery_name=str(row["battery_name"]) if row["battery_name"] is not None else None,
        )
        for row in rows
    ]


def relearn_to_text(findings: list[RelearnFinding]) -> str:
    note = "This does not mean the battery physically recovered; it means the reported full capacity changed."
    if not findings:
        return f"No capacity relearning found.\n\n{note}"
    headers = [
        "Time",
        "Battery",
        "Old full Wh",
        "New full Wh",
        "Delta Wh",
        "Old design Wh",
        "New design Wh",
        "Old health %",
        "New health %",
        "Cause",
        "Type",
    ]
    rows = [
        [
            _short_iso(finding.wall_iso),
            finding.battery_name,
            _format_optional_float(finding.old_energy_full_wh, precision=2),
            _format_optional_float(finding.new_energy_full_wh, precision=2),
            _format_optional_float(finding.delta_wh, precision=2, signed=True),
            _format_optional_float(finding.old_energy_full_design_wh, precision=2),
            _format_optional_float(finding.new_energy_full_design_wh, precision=2),
            _format_optional_float(finding.old_health_percent, precision=1),
            _format_optional_float(finding.new_health_percent, precision=1),
            finding.likely_cause,
            finding.event_type,
        ]
        for finding in findings
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = [note, ""]
    lines.append("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def export_relearn_json(findings: list[RelearnFinding], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2), encoding="utf-8")


def export_relearn_csv(findings: list[RelearnFinding], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = [finding.to_dict() for finding in findings]
    with output.open("w", newline="", encoding="utf-8") as fh:
        if not rows:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _finding_for_pair(
    session_id: str,
    samples: list[RelearnSample],
    index: int,
    battery_name: str,
    cfg: RelearnConfig,
    events: list[RelearnContextEvent],
) -> RelearnFinding | None:
    previous = samples[index - 1]
    current = samples[index]
    old = previous.batteries[battery_name]
    new = current.batteries[battery_name]
    delta = _delta(old.energy_full_wh, new.energy_full_wh)
    design_delta = _delta(old.energy_full_design_wh, new.energy_full_design_wh)
    health_delta = _delta(old.health_percent, new.health_percent)
    relative_change = _relative_change_percent(delta, old.energy_full_wh)
    design_changed = design_delta is not None and design_delta != 0.0

    if delta is not None and _is_relearn_delta(delta, relative_change, cfg):
        return RelearnFinding(
            session_id=session_id,
            event_type=ENERGY_FULL_RELEARN,
            severity="warning",
            battery_name=battery_name,
            wall_time=current.wall_time,
            wall_iso=current.wall_iso,
            monotonic_time=current.monotonic_time,
            sample_id=current.sample_id,
            previous_seq=previous.seq,
            current_seq=current.seq,
            old_energy_full_wh=old.energy_full_wh,
            new_energy_full_wh=new.energy_full_wh,
            delta_wh=delta,
            relative_change_percent=relative_change,
            old_energy_full_design_wh=old.energy_full_design_wh,
            new_energy_full_design_wh=new.energy_full_design_wh,
            design_delta_wh=design_delta,
            old_health_percent=old.health_percent,
            new_health_percent=new.health_percent,
            health_delta_points=health_delta,
            likely_cause=_likely_cause(samples, index, battery_name, cfg, events),
        )

    if design_changed:
        return RelearnFinding(
            session_id=session_id,
            event_type=ENERGY_FULL_DESIGN_CHANGE,
            severity="info",
            battery_name=battery_name,
            wall_time=current.wall_time,
            wall_iso=current.wall_iso,
            monotonic_time=current.monotonic_time,
            sample_id=current.sample_id,
            previous_seq=previous.seq,
            current_seq=current.seq,
            old_energy_full_wh=old.energy_full_wh,
            new_energy_full_wh=new.energy_full_wh,
            delta_wh=delta,
            relative_change_percent=relative_change,
            old_energy_full_design_wh=old.energy_full_design_wh,
            new_energy_full_design_wh=new.energy_full_design_wh,
            design_delta_wh=design_delta,
            old_health_percent=old.health_percent,
            new_health_percent=new.health_percent,
            health_delta_points=health_delta,
            likely_cause=UNKNOWN,
        )
    return None


def _is_relearn_delta(delta: float, relative_change_percent: float | None, cfg: RelearnConfig) -> bool:
    if abs(delta) < cfg.min_absolute_change_wh:
        return False
    if relative_change_percent is None:
        return True
    return abs(relative_change_percent) >= cfg.min_relative_change_percent


def _likely_cause(
    samples: list[RelearnSample],
    index: int,
    battery_name: str,
    cfg: RelearnConfig,
    events: list[RelearnContextEvent],
) -> str:
    previous = samples[index - 1]
    current = samples[index]
    if current.monotonic_time - previous.monotonic_time >= cfg.resume_gap_seconds:
        return AFTER_RESUME
    if _has_nearby_resume_event(events, current, battery_name, cfg):
        return AFTER_RESUME

    window_start = current.wall_time - cfg.context_window_seconds
    window_end = current.wall_time + cfg.context_window_seconds
    nearby = [
        sample.batteries[battery_name]
        for sample in samples
        if window_start <= sample.wall_time <= window_end and battery_name in sample.batteries
    ]
    percents = [_sample_percent(sample) for sample in nearby]
    if any(percent is not None and percent <= cfg.deep_discharge_percent for percent in percents):
        return AFTER_DEEP_DISCHARGE
    if any(
        (percent is not None and percent >= cfg.full_charge_percent)
        or _normalize_status(sample.status) == "full"
        for sample, percent in zip(nearby, percents, strict=True)
    ):
        return AFTER_FULL_CHARGE
    return UNKNOWN


def _has_nearby_resume_event(
    events: list[RelearnContextEvent],
    sample: RelearnSample,
    battery_name: str,
    cfg: RelearnConfig,
) -> bool:
    for event in events:
        if event.wall_time is None or event.event_type not in _RESUME_EVENTS:
            continue
        if event.battery_name not in {None, battery_name}:
            continue
        if abs(event.wall_time - sample.wall_time) <= cfg.context_window_seconds:
            return True
    return False


def _sample_percent(sample: RelearnBatterySample) -> float | None:
    return sample.computed_percent if sample.computed_percent is not None else sample.capacity_percent


def _relative_change_percent(delta: float | None, old_value: float | None) -> float | None:
    if delta is None or old_value is None or old_value == 0.0:
        return None
    return (delta / old_value) * 100.0


def _delta(old: float | None, new: float | None) -> float | None:
    if old is None or new is None:
        return None
    return new - old


def _normalize_status(status: str | None) -> str | None:
    if status is None:
        return None
    cleaned = status.strip().lower()
    return cleaned or None


def _row_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _row_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _micro_to_unit(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) / 1_000_000.0


def _short_iso(value: str) -> str:
    return value.replace("T", " ").split("+", maxsplit=1)[0]


def _format_optional_float(value: float | None, *, precision: int, signed: bool = False) -> str:
    if value is None:
        return "-"
    sign = "+" if signed else ""
    return f"{value:{sign}.{precision}f}"
