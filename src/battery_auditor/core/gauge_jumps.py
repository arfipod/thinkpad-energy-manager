from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import Event

IMPOSSIBLE_ENERGY_DROP = "IMPOSSIBLE_ENERGY_DROP"
IMPOSSIBLE_ENERGY_GAIN = "IMPOSSIBLE_ENERGY_GAIN"
PERCENTAGE_JUMP = "PERCENTAGE_JUMP"
LOW_END_GAUGE_JUMP = "LOW_END_GAUGE_JUMP"
RECOVERY_JUMP = "RECOVERY_JUMP"
AFTER_RESUME_RECONCILIATION = "AFTER_RESUME_RECONCILIATION"

_TRANSITION_EVENTS = {
    "AC_CONNECTED",
    "AC_DISCONNECTED",
    "BATTERY_SWITCH",
    "CHARGING_STARTED",
    "DISCHARGING_STARTED",
}


class GapClassifier(Protocol):
    def __call__(self, previous: GaugeSample, current: GaugeSample) -> str | None:
        """Return a gap classification such as SUSPEND, or None for normal samples."""


@dataclass(frozen=True, slots=True)
class GaugeJumpConfig:
    absolute_tolerance_wh: float = 0.10
    relative_tolerance: float = 3.0
    percent_jump_threshold: float = 5.0
    low_end_percent: float = 25.0
    transition_window_seconds: float = 5.0
    suspend_gap_seconds: float | None = None

    @classmethod
    def from_auditor_config(cls, cfg: AuditorConfig) -> GaugeJumpConfig:
        return cls(
            absolute_tolerance_wh=cfg.gauge_jump_absolute_tolerance_wh,
            relative_tolerance=cfg.gauge_jump_relative_tolerance,
            percent_jump_threshold=cfg.percent_jump_threshold,
            low_end_percent=cfg.gauge_jump_low_end_percent,
            transition_window_seconds=cfg.gauge_jump_transition_window_seconds,
            suspend_gap_seconds=cfg.gauge_jump_suspend_gap_seconds,
        )


@dataclass(frozen=True, slots=True)
class GaugeBatterySample:
    name: str
    status: str | None
    capacity_percent: float | None
    computed_percent: float | None
    energy_now_wh: float | None
    energy_full_wh: float | None
    power_now_w: float | None


@dataclass(frozen=True, slots=True)
class GaugeSample:
    sample_id: int | None
    seq: int
    wall_time: float
    wall_iso: str
    monotonic_time: float
    ac_online: bool | None
    batteries: dict[str, GaugeBatterySample]


@dataclass(frozen=True, slots=True)
class GaugeJumpFinding:
    session_id: str
    event_type: str
    severity: str
    classification: str
    battery_name: str
    wall_time: float
    wall_iso: str
    monotonic_time: float
    sample_id: int | None
    previous_seq: int
    current_seq: int
    wall_delta_seconds: float
    monotonic_delta_seconds: float
    old_energy_wh: float | None
    new_energy_wh: float | None
    old_percent: float | None
    new_percent: float | None
    old_capacity_percent: float | None
    new_capacity_percent: float | None
    expected_physical_wh_delta: float | None
    expected_max_wh_delta: float | None
    observed_wh_delta: float | None
    observed_capacity_wh_delta: float | None
    observed_capacity_delta_points: float | None
    power_now_w: float | None
    old_status: str | None
    new_status: str | None
    old_ac_online: bool | None
    new_ac_online: bool | None
    transition_event: str | None = None
    gap_classification: str | None = None

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
        observed = "n/a" if self.observed_wh_delta is None else f"{self.observed_wh_delta:+.3f} Wh"
        allowed = "n/a" if self.expected_max_wh_delta is None else f"{self.expected_max_wh_delta:.3f} Wh"
        return (
            f"{self.battery_name}: gauge jump classified as {self.classification}; "
            f"observed {observed}, expected max {allowed}."
        )


def analyze_session_jumps(
    db: BatteryDatabase,
    session_id: str,
    *,
    config: GaugeJumpConfig | None = None,
    gap_classifier: GapClassifier | None = None,
) -> list[GaugeJumpFinding]:
    if db.get_session(session_id) is None:
        raise ValueError(f"Unknown session: {session_id}")
    samples = samples_from_rows(db.fetch_session_series(session_id))
    return analyze_samples_jumps(session_id, samples, config=config, gap_classifier=gap_classifier)


def analyze_samples_jumps(
    session_id: str,
    samples: list[GaugeSample],
    *,
    config: GaugeJumpConfig | None = None,
    gap_classifier: GapClassifier | None = None,
) -> list[GaugeJumpFinding]:
    cfg = config or GaugeJumpConfig()
    classifier = gap_classifier or _default_gap_classifier(cfg)
    findings: list[GaugeJumpFinding] = []
    for index in range(1, len(samples)):
        previous = samples[index - 1]
        current = samples[index]
        gap = classifier(previous, current)
        transition = _transition_event(previous, current, cfg)
        names = sorted(set(previous.batteries) & set(current.batteries))
        for name in names:
            finding = _finding_for_pair(session_id, previous, current, name, cfg, gap, transition)
            if finding is not None:
                findings.append(finding)
    return findings


def persist_gauge_jump_events(db: BatteryDatabase, session_id: str, findings: list[GaugeJumpFinding]) -> None:
    for finding in findings:
        db.insert_event(session_id, finding.to_event())


def samples_from_rows(rows: list[Any]) -> list[GaugeSample]:
    builders: dict[int, dict[str, Any]] = {}
    for row in rows:
        row_keys = set(row.keys())
        seq = int(row["seq"])
        sample = builders.get(seq)
        if sample is None:
            sample_id = (
                int(row["sample_id"])
                if "sample_id" in row_keys and row["sample_id"] is not None
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
        sample["batteries"][name] = GaugeBatterySample(
            name=name,
            status=str(row["status"]) if row["status"] is not None else None,
            capacity_percent=_row_float(row["capacity_percent"]),
            computed_percent=_row_float(row["computed_percent"]),
            energy_now_wh=_micro_to_unit(row["energy_now_uwh"]),
            energy_full_wh=_micro_to_unit(row["energy_full_uwh"]),
            power_now_w=_micro_to_unit(row["power_now_uw"]),
        )
    return [
        GaugeSample(
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


def jumps_to_text(findings: list[GaugeJumpFinding]) -> str:
    if not findings:
        return "No gauge jumps found."
    headers = [
        "Time",
        "Battery",
        "Old Wh",
        "New Wh",
        "Old %",
        "New %",
        "Expected max Wh",
        "Observed Wh",
        "Severity",
        "Classification",
    ]
    rows = [
        [
            _short_iso(finding.wall_iso),
            finding.battery_name,
            _format_optional_float(finding.old_energy_wh, precision=3),
            _format_optional_float(finding.new_energy_wh, precision=3),
            _format_optional_float(finding.old_percent, precision=1),
            _format_optional_float(finding.new_percent, precision=1),
            _format_optional_float(finding.expected_max_wh_delta, precision=3),
            _format_optional_float(finding.observed_wh_delta, precision=3, signed=True),
            finding.severity,
            finding.classification,
        ]
        for finding in findings
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def export_jumps_json(findings: list[GaugeJumpFinding], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False, indent=2), encoding="utf-8")


def export_jumps_csv(findings: list[GaugeJumpFinding], output: Path) -> None:
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
    previous: GaugeSample,
    current: GaugeSample,
    battery_name: str,
    cfg: GaugeJumpConfig,
    gap_classification: str | None,
    transition_event: str | None,
) -> GaugeJumpFinding | None:
    old = previous.batteries[battery_name]
    new = current.batteries[battery_name]
    wall_delta_seconds = max(0.0, current.wall_time - previous.wall_time)
    monotonic_delta_seconds = max(0.0, current.monotonic_time - previous.monotonic_time)
    dt_seconds = monotonic_delta_seconds or wall_delta_seconds
    energy_delta = _delta(old.energy_now_wh, new.energy_now_wh)
    capacity_delta = _delta(old.capacity_percent, new.capacity_percent)
    capacity_wh_delta = _capacity_wh_delta(capacity_delta, old, new)
    expected_physical = _expected_physical_delta_wh(old, new, dt_seconds)
    expected_max = _expected_max_delta_wh(expected_physical, cfg)
    low_end = _is_low_end(old.computed_percent, new.computed_percent, cfg)
    percent_jump = (
        capacity_delta is not None
        and (
            abs(capacity_delta) >= cfg.percent_jump_threshold
            or (
                capacity_wh_delta is not None
                and expected_max is not None
                and abs(capacity_wh_delta) > expected_max
            )
        )
    )
    energy_jump = (
        energy_delta is not None
        and expected_max is not None
        and abs(energy_delta) > expected_max
    )
    gap_reconciliation = (
        gap_classification in {"SUSPEND", "RESUME", "AFTER_RESUME"}
        and energy_delta is not None
        and abs(energy_delta) > cfg.absolute_tolerance_wh
    )

    if not energy_jump and not percent_jump and not gap_reconciliation:
        return None

    event_type = _event_type_for_jump(energy_delta, energy_jump, percent_jump, low_end, gap_classification, new)
    classification = event_type
    if gap_classification in {"SUSPEND", "RESUME", "AFTER_RESUME"}:
        classification = AFTER_RESUME_RECONCILIATION
    severity = _severity(event_type, energy_delta, expected_max, transition_event, gap_classification)
    return GaugeJumpFinding(
        session_id=session_id,
        event_type=event_type,
        severity=severity,
        classification=classification,
        battery_name=battery_name,
        wall_time=current.wall_time,
        wall_iso=current.wall_iso,
        monotonic_time=current.monotonic_time,
        sample_id=current.sample_id,
        previous_seq=previous.seq,
        current_seq=current.seq,
        wall_delta_seconds=wall_delta_seconds,
        monotonic_delta_seconds=monotonic_delta_seconds,
        old_energy_wh=old.energy_now_wh,
        new_energy_wh=new.energy_now_wh,
        old_percent=old.computed_percent,
        new_percent=new.computed_percent,
        old_capacity_percent=old.capacity_percent,
        new_capacity_percent=new.capacity_percent,
        expected_physical_wh_delta=expected_physical,
        expected_max_wh_delta=expected_max,
        observed_wh_delta=energy_delta,
        observed_capacity_wh_delta=capacity_wh_delta,
        observed_capacity_delta_points=capacity_delta,
        power_now_w=_max_abs(old.power_now_w, new.power_now_w),
        old_status=old.status,
        new_status=new.status,
        old_ac_online=previous.ac_online,
        new_ac_online=current.ac_online,
        transition_event=transition_event,
        gap_classification=gap_classification,
    )


def _event_type_for_jump(
    energy_delta: float | None,
    energy_jump: bool,
    percent_jump: bool,
    low_end: bool,
    gap_classification: str | None,
    current: GaugeBatterySample,
) -> str:
    if gap_classification in {"SUSPEND", "RESUME", "AFTER_RESUME"}:
        return RECOVERY_JUMP
    if low_end:
        return LOW_END_GAUGE_JUMP
    if energy_jump and energy_delta is not None:
        if energy_delta < 0:
            return IMPOSSIBLE_ENERGY_DROP
        if _normalize_status(current.status) == "discharging":
            return RECOVERY_JUMP
        return IMPOSSIBLE_ENERGY_GAIN
    if percent_jump:
        return PERCENTAGE_JUMP
    return PERCENTAGE_JUMP


def _severity(
    event_type: str,
    energy_delta: float | None,
    expected_max: float | None,
    transition_event: str | None,
    gap_classification: str | None,
) -> str:
    if gap_classification in {"SUSPEND", "RESUME", "AFTER_RESUME"}:
        return "info"
    if transition_event in _TRANSITION_EVENTS:
        return "info"
    if event_type == PERCENTAGE_JUMP:
        return "warning"
    if energy_delta is not None and expected_max is not None and expected_max > 0 and abs(energy_delta) >= expected_max * 10.0:
        return "critical"
    return "warning"


def _transition_event(previous: GaugeSample, current: GaugeSample, cfg: GaugeJumpConfig) -> str | None:
    dt = max(0.0, current.monotonic_time - previous.monotonic_time)
    if dt > cfg.transition_window_seconds:
        return None
    if previous.ac_online != current.ac_online:
        if current.ac_online is True:
            return "AC_CONNECTED"
        if current.ac_online is False:
            return "AC_DISCONNECTED"
    old_active_discharge = _active_battery(previous, "discharging")
    new_active_discharge = _active_battery(current, "discharging")
    if old_active_discharge != new_active_discharge:
        if old_active_discharge is not None and new_active_discharge is not None:
            return "BATTERY_SWITCH"
        if new_active_discharge is not None:
            return "DISCHARGING_STARTED"
    old_active_charge = _active_battery(previous, "charging")
    new_active_charge = _active_battery(current, "charging")
    if old_active_charge != new_active_charge and new_active_charge is not None:
        return "CHARGING_STARTED"
    return None


def _default_gap_classifier(cfg: GaugeJumpConfig) -> GapClassifier:
    def classify(previous: GaugeSample, current: GaugeSample) -> str | None:
        if cfg.suspend_gap_seconds is None:
            return None
        wall_gap = current.wall_time - previous.wall_time
        mono_gap = current.monotonic_time - previous.monotonic_time
        if wall_gap > cfg.suspend_gap_seconds or mono_gap > cfg.suspend_gap_seconds:
            return "SUSPEND"
        return None

    return classify


def _expected_physical_delta_wh(old: GaugeBatterySample, new: GaugeBatterySample, dt_seconds: float) -> float | None:
    power = _max_abs(old.power_now_w, new.power_now_w)
    if power is None or dt_seconds <= 0.0:
        return None
    return power * (dt_seconds / 3600.0)


def _capacity_wh_delta(
    capacity_delta_points: float | None,
    old: GaugeBatterySample,
    new: GaugeBatterySample,
) -> float | None:
    if capacity_delta_points is None:
        return None
    energy_full = new.energy_full_wh if new.energy_full_wh is not None else old.energy_full_wh
    if energy_full is None:
        return None
    return energy_full * (capacity_delta_points / 100.0)


def _expected_max_delta_wh(expected_physical_delta_wh: float | None, cfg: GaugeJumpConfig) -> float | None:
    if expected_physical_delta_wh is None:
        return None
    return max(cfg.absolute_tolerance_wh, expected_physical_delta_wh * cfg.relative_tolerance)


def _active_battery(sample: GaugeSample, status: str) -> str | None:
    names = [
        name
        for name, battery in sample.batteries.items()
        if _normalize_status(battery.status) == status and (battery.power_now_w or 0.0) > 0.0
    ]
    if not names:
        return None
    return "+".join(sorted(names))


def _is_low_end(old_percent: float | None, new_percent: float | None, cfg: GaugeJumpConfig) -> bool:
    values = [value for value in (old_percent, new_percent) if value is not None]
    return bool(values) and min(values) < cfg.low_end_percent


def _delta(old: float | None, new: float | None) -> float | None:
    if old is None or new is None:
        return None
    return new - old


def _max_abs(*values: float | None) -> float | None:
    present = [abs(value) for value in values if value is not None]
    if not present:
        return None
    return max(present)


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
