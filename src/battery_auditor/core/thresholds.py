from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from battery_auditor.config import AuditorConfig, TlpThresholdExpectation
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.models import Event

THRESHOLD_MISMATCH = "THRESHOLD_MISMATCH"
THRESHOLD_RESTORED = "THRESHOLD_RESTORED"
THRESHOLD_UNKNOWN = "THRESHOLD_UNKNOWN"

SOURCE_CONFIG = "config"
SOURCE_SYSFS = "sysfs"
SOURCE_TLP_STAT = "tlp_stat"
SOURCE_UPOWER = "upower"

STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ThresholdSample:
    sample_id: int | None
    seq: int
    wall_time: float
    wall_iso: str
    monotonic_time: float
    battery_name: str
    sysfs_start_threshold: int | None
    sysfs_stop_threshold: int | None


@dataclass(frozen=True, slots=True)
class ThresholdStatus:
    battery_name: str
    configured_start_threshold: int | None
    configured_stop_threshold: int | None
    sysfs_start_threshold: int | None
    sysfs_stop_threshold: int | None
    configured_source: str
    sysfs_source: str
    source: str
    status: str
    mismatch: bool
    last_observed_wall_time: float | None
    last_observed_wall_iso: str | None
    last_ok_wall_time: float | None
    last_ok_wall_iso: str | None
    last_mismatch_wall_time: float | None
    last_mismatch_wall_iso: str | None
    last_unknown_wall_time: float | None
    last_unknown_wall_iso: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ThresholdEventFinding:
    event_type: str
    severity: str
    battery_name: str
    wall_time: float
    wall_iso: str
    monotonic_time: float
    sample_id: int | None
    configured_start_threshold: int | None
    configured_stop_threshold: int | None
    sysfs_start_threshold: int | None
    sysfs_stop_threshold: int | None
    previous_status: str | None
    status: str
    source: str = SOURCE_SYSFS

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
        configured = _format_pair(self.configured_start_threshold, self.configured_stop_threshold)
        sysfs = _format_pair(self.sysfs_start_threshold, self.sysfs_stop_threshold)
        if self.event_type == THRESHOLD_RESTORED:
            return f"{self.battery_name}: charge thresholds restored. configured={configured} sysfs={sysfs}."
        if self.event_type == THRESHOLD_UNKNOWN:
            return f"{self.battery_name}: charge threshold readback is unknown. configured={configured} sysfs={sysfs}."
        return f"{self.battery_name}: charge threshold mismatch. configured={configured} sysfs={sysfs}."


def analyze_session_thresholds(db: BatteryDatabase, session_id: str, cfg: AuditorConfig) -> list[ThresholdStatus]:
    if db.get_session(session_id) is None:
        raise ValueError(f"Unknown session: {session_id}")
    return analyze_threshold_samples(samples_from_rows(db.fetch_session_series(session_id)), cfg)


def analyze_threshold_samples(samples: list[ThresholdSample], cfg: AuditorConfig) -> list[ThresholdStatus]:
    by_battery: dict[str, list[ThresholdSample]] = {}
    for sample in samples:
        by_battery.setdefault(sample.battery_name, []).append(sample)
    for battery in cfg.expected_thresholds:
        by_battery.setdefault(battery, [])

    statuses: list[ThresholdStatus] = []
    for battery_name in sorted(by_battery):
        expected = cfg.expected_thresholds.get(battery_name)
        series = by_battery[battery_name]
        latest = series[-1] if series else None
        configured_start = expected.start if expected is not None else None
        configured_stop = expected.stop if expected is not None else None
        latest_status = _sample_status(latest, expected) if latest is not None else STATUS_UNKNOWN
        last_ok = _last_with_status(series, expected, STATUS_OK)
        last_mismatch = _last_with_status(series, expected, STATUS_MISMATCH)
        last_unknown = _last_with_status(series, expected, STATUS_UNKNOWN)
        statuses.append(
            ThresholdStatus(
                battery_name=battery_name,
                configured_start_threshold=configured_start,
                configured_stop_threshold=configured_stop,
                sysfs_start_threshold=latest.sysfs_start_threshold if latest is not None else None,
                sysfs_stop_threshold=latest.sysfs_stop_threshold if latest is not None else None,
                configured_source=SOURCE_CONFIG,
                sysfs_source=SOURCE_SYSFS,
                source=SOURCE_SYSFS,
                status=latest_status,
                mismatch=latest_status == STATUS_MISMATCH,
                last_observed_wall_time=latest.wall_time if latest is not None else None,
                last_observed_wall_iso=latest.wall_iso if latest is not None else None,
                last_ok_wall_time=last_ok.wall_time if last_ok is not None else None,
                last_ok_wall_iso=last_ok.wall_iso if last_ok is not None else None,
                last_mismatch_wall_time=last_mismatch.wall_time if last_mismatch is not None else None,
                last_mismatch_wall_iso=last_mismatch.wall_iso if last_mismatch is not None else None,
                last_unknown_wall_time=last_unknown.wall_time if last_unknown is not None else None,
                last_unknown_wall_iso=last_unknown.wall_iso if last_unknown is not None else None,
            )
        )
    return statuses


def threshold_event_findings(samples: list[ThresholdSample], cfg: AuditorConfig) -> list[ThresholdEventFinding]:
    by_battery: dict[str, list[ThresholdSample]] = {}
    for sample in samples:
        by_battery.setdefault(sample.battery_name, []).append(sample)
    findings: list[ThresholdEventFinding] = []
    for battery_name, series in sorted(by_battery.items()):
        expected = cfg.expected_thresholds.get(battery_name)
        previous_status: str | None = None
        for sample in series:
            status = _sample_status(sample, expected)
            event_type = _event_type_for_status(previous_status, status)
            if event_type is not None:
                findings.append(_finding_from_sample(sample, expected, previous_status, status, event_type))
            previous_status = status
    return findings


def persist_threshold_events(db: BatteryDatabase, session_id: str, findings: list[ThresholdEventFinding]) -> None:
    for finding in findings:
        db.insert_event(session_id, finding.to_event())


def samples_from_rows(rows: list[Any]) -> list[ThresholdSample]:
    samples: list[ThresholdSample] = []
    for row in rows:
        start = row["charge_control_start_threshold"]
        stop = row["charge_control_end_threshold"]
        if start is None:
            start = row["charge_start_threshold"] if "charge_start_threshold" in row.keys() else None
        if stop is None:
            stop = row["charge_stop_threshold"] if "charge_stop_threshold" in row.keys() else None
        sample_id = (
            int(row["sample_id"])
            if "sample_id" in row.keys() and row["sample_id"] is not None
            else None
        )
        samples.append(
            ThresholdSample(
                sample_id=sample_id,
                seq=int(row["seq"]),
                wall_time=float(row["wall_time"]),
                wall_iso=str(row["wall_iso"]),
                monotonic_time=float(row["monotonic_time"]),
                battery_name=str(row["battery_name"]),
                sysfs_start_threshold=_row_int(start),
                sysfs_stop_threshold=_row_int(stop),
            )
        )
    return samples


def thresholds_to_text(statuses: list[ThresholdStatus]) -> str:
    if not statuses:
        return "No threshold data found."
    headers = [
        "Battery",
        "Configured",
        "Current sysfs",
        "Mismatch",
        "Status",
        "Last OK",
        "Last mismatch",
        "Last unknown",
    ]
    rows = [
        [
            status.battery_name,
            _format_pair(status.configured_start_threshold, status.configured_stop_threshold),
            _format_pair(status.sysfs_start_threshold, status.sysfs_stop_threshold),
            "yes" if status.mismatch else "no",
            status.status,
            _short_iso(status.last_ok_wall_iso),
            _short_iso(status.last_mismatch_wall_iso),
            _short_iso(status.last_unknown_wall_iso),
        ]
        for status in statuses
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def thresholds_to_json(statuses: list[ThresholdStatus]) -> str:
    return json.dumps([status.to_dict() for status in statuses], ensure_ascii=False, indent=2)


def status_for_snapshot(
    battery_name: str,
    expected: TlpThresholdExpectation | None,
    sysfs_start_threshold: int | None,
    sysfs_stop_threshold: int | None,
) -> str:
    if expected is None or expected.start is None or expected.stop is None:
        return STATUS_UNKNOWN
    if sysfs_start_threshold is None or sysfs_stop_threshold is None:
        return STATUS_UNKNOWN
    if expected.start == sysfs_start_threshold and expected.stop == sysfs_stop_threshold:
        return STATUS_OK
    return STATUS_MISMATCH


def _sample_status(sample: ThresholdSample | None, expected: TlpThresholdExpectation | None) -> str:
    if sample is None:
        return STATUS_UNKNOWN
    return status_for_snapshot(
        sample.battery_name,
        expected,
        sample.sysfs_start_threshold,
        sample.sysfs_stop_threshold,
    )


def _last_with_status(
    series: list[ThresholdSample],
    expected: TlpThresholdExpectation | None,
    status: str,
) -> ThresholdSample | None:
    for sample in reversed(series):
        if _sample_status(sample, expected) == status:
            return sample
    return None


def _event_type_for_status(previous_status: str | None, status: str) -> str | None:
    if status == STATUS_MISMATCH and previous_status != STATUS_MISMATCH:
        return THRESHOLD_MISMATCH
    if status == STATUS_UNKNOWN and previous_status != STATUS_UNKNOWN:
        return THRESHOLD_UNKNOWN
    if status == STATUS_OK and previous_status in {STATUS_MISMATCH, STATUS_UNKNOWN}:
        return THRESHOLD_RESTORED
    return None


def _finding_from_sample(
    sample: ThresholdSample,
    expected: TlpThresholdExpectation | None,
    previous_status: str | None,
    status: str,
    event_type: str,
) -> ThresholdEventFinding:
    return ThresholdEventFinding(
        event_type=event_type,
        severity="warning" if event_type == THRESHOLD_MISMATCH else "info",
        battery_name=sample.battery_name,
        wall_time=sample.wall_time,
        wall_iso=sample.wall_iso,
        monotonic_time=sample.monotonic_time,
        sample_id=sample.sample_id,
        configured_start_threshold=expected.start if expected is not None else None,
        configured_stop_threshold=expected.stop if expected is not None else None,
        sysfs_start_threshold=sample.sysfs_start_threshold,
        sysfs_stop_threshold=sample.sysfs_stop_threshold,
        previous_status=previous_status,
        status=status,
    )


def _row_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _format_pair(start: int | None, stop: int | None) -> str:
    if start is None or stop is None:
        return "-"
    return f"{start}/{stop}"


def _short_iso(value: str | None) -> str:
    if value is None:
        return "-"
    return value.replace("T", " ").split("+", maxsplit=1)[0]
