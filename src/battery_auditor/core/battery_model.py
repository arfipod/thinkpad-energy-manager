from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.gauge_jumps import (
    LOW_END_GAUGE_JUMP,
    GaugeJumpConfig,
    analyze_session_jumps,
)
from battery_auditor.core.relearn import ENERGY_FULL_RELEARN, RelearnConfig, analyze_session_relearn
from battery_auditor.core.thresholds import STATUS_MISMATCH, analyze_session_thresholds

ACTIVE_DISCHARGING = "ACTIVE_DISCHARGING"
RESERVE = "RESERVE"
CHARGING = "CHARGING"
IDLE = "IDLE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BatteryModelConfig:
    low_end_percent: float = 25.0
    low_end_margin_fraction: float = 0.10
    global_critical_reserve_wh: float = 0.0
    relearn_stable_samples: int = 3
    eta_short_window_seconds: float = 60.0
    eta_medium_window_seconds: float = 300.0
    eta_long_window_seconds: float = 900.0
    min_eta_pairs: int = 2
    ac_transition_exclusion_seconds: float = 5.0
    suspend_gap_seconds: float = 120.0

    @classmethod
    def from_auditor_config(cls, cfg: AuditorConfig) -> BatteryModelConfig:
        return cls(
            low_end_percent=cfg.model_low_end_percent,
            low_end_margin_fraction=cfg.model_low_end_margin_fraction,
            global_critical_reserve_wh=cfg.model_global_critical_reserve_wh,
            relearn_stable_samples=cfg.model_relearn_stable_samples,
            eta_short_window_seconds=cfg.model_eta_short_window_seconds,
            eta_medium_window_seconds=cfg.model_eta_medium_window_seconds,
            eta_long_window_seconds=cfg.model_eta_long_window_seconds,
            min_eta_pairs=cfg.model_min_eta_pairs,
            ac_transition_exclusion_seconds=cfg.model_ac_transition_exclusion_seconds,
            suspend_gap_seconds=cfg.model_suspend_gap_seconds,
        )


@dataclass(frozen=True, slots=True)
class ModelBatterySample:
    name: str
    status: str | None
    raw_percent: float | None
    computed_percent: float | None
    energy_now_wh: float | None
    energy_full_wh: float | None
    power_now_w: float | None


@dataclass(frozen=True, slots=True)
class ModelSample:
    seq: int
    wall_time: float
    wall_iso: str
    monotonic_time: float
    ac_online: bool | None
    total_raw_percent: float | None
    total_energy_now_wh: float | None
    batteries: dict[str, ModelBatterySample]


@dataclass(frozen=True, slots=True)
class BatteryEstimate:
    battery_name: str
    raw_percent: float | None
    computed_percent: float | None
    learned_full_wh: float | None
    usable_energy_wh: float | None
    low_end_confidence: float
    gauge_confidence: float
    active_role: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PackEstimate:
    wall_time: float
    wall_iso: str
    total_raw_percent: float | None
    effective_pack_percent: float | None
    total_energy_now_wh: float | None
    usable_energy_wh: float | None
    learned_pack_full_wh: float | None
    eta_seconds_nominal: float | None
    eta_seconds_pessimistic: float | None
    eta_seconds_optimistic: float | None
    confidence: float
    explanation: list[str]
    batteries: list[BatteryEstimate]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ConsumptionEstimate:
    nominal_w: float | None
    pessimistic_w: float | None
    optimistic_w: float | None
    explanation: list[str]
    confidence: float


def estimate_session(
    db: BatteryDatabase,
    session_id: str,
    cfg: AuditorConfig,
    *,
    config: BatteryModelConfig | None = None,
) -> PackEstimate:
    if db.get_session(session_id) is None:
        raise ValueError(f"Unknown session: {session_id}")
    rows = db.fetch_session_series(session_id)
    samples = samples_from_rows(rows)
    return estimate_samples(
        session_id,
        samples,
        cfg,
        config=config,
        low_end_jump_batteries=_low_end_jump_batteries(db, session_id, cfg),
        relearn_sample_counts=_relearn_sample_counts(db, session_id, cfg),
        threshold_mismatch=any(status.status == STATUS_MISMATCH for status in analyze_session_thresholds(db, session_id, cfg)),
    )


def estimate_samples(
    session_id: str,
    samples: list[ModelSample],
    cfg: AuditorConfig,
    *,
    config: BatteryModelConfig | None = None,
    low_end_jump_batteries: set[str] | None = None,
    relearn_sample_counts: dict[str, int] | None = None,
    threshold_mismatch: bool = False,
) -> PackEstimate:
    model_cfg = config or BatteryModelConfig.from_auditor_config(cfg)
    if not samples:
        return PackEstimate(
            wall_time=0.0,
            wall_iso="",
            total_raw_percent=None,
            effective_pack_percent=None,
            total_energy_now_wh=None,
            usable_energy_wh=None,
            learned_pack_full_wh=None,
            eta_seconds_nominal=None,
            eta_seconds_pessimistic=None,
            eta_seconds_optimistic=None,
            confidence=0.0,
            explanation=["No samples are available."],
            batteries=[],
        )

    latest = samples[-1]
    low_end_batteries = low_end_jump_batteries or set()
    relearn_counts = relearn_sample_counts or {}
    explanations: list[str] = ["Effective percent uses energy_now divided by learned energy_full, not design capacity."]
    active_names = {
        name
        for name, battery in latest.batteries.items()
        if _normalize_status(battery.status) == "discharging" and (battery.power_now_w or 0.0) > 0.0
    }

    battery_estimates: list[BatteryEstimate] = []
    total_energy = 0.0
    usable_before_global = 0.0
    learned_full_total = 0.0
    gauge_confidences: list[float] = []

    for name, battery in sorted(latest.batteries.items()):
        energy_now = battery.energy_now_wh or 0.0
        learned_full = battery.energy_full_wh
        if learned_full is not None:
            learned_full_total += learned_full
        total_energy += energy_now

        low_end_confidence = 1.0
        usable = energy_now
        if _is_low_end(battery, model_cfg) and name in low_end_batteries:
            low_end_confidence = 0.55
            margin = energy_now * model_cfg.low_end_margin_fraction
            usable = max(0.0, usable - margin)
            explanations.append(
                f"{name}: low-end gauge jump history below {model_cfg.low_end_percent:.0f}% reduces confidence and subtracts {margin:.2f} Wh margin."
            )

        gauge_confidence = low_end_confidence
        stable_count = relearn_counts.get(name)
        if stable_count is not None:
            explanations.append(f"{name}: energy_full relearn detected; using latest reported full capacity.")
            if stable_count < model_cfg.relearn_stable_samples:
                gauge_confidence = min(gauge_confidence, 0.75)
                explanations.append(
                    f"{name}: only {stable_count} stable sample(s) after relearn, so gauge confidence is lower."
                )

        role = _battery_role(name, battery, active_names, latest.ac_online)
        battery_estimates.append(
            BatteryEstimate(
                battery_name=name,
                raw_percent=battery.raw_percent,
                computed_percent=battery.computed_percent,
                learned_full_wh=learned_full,
                usable_energy_wh=usable,
                low_end_confidence=low_end_confidence,
                gauge_confidence=gauge_confidence,
                active_role=role,
            )
        )
        gauge_confidences.append(gauge_confidence)
        usable_before_global += usable

    usable_energy = max(0.0, usable_before_global - model_cfg.global_critical_reserve_wh)
    if model_cfg.global_critical_reserve_wh > 0.0:
        explanations.append(f"Global critical reserve subtracts {model_cfg.global_critical_reserve_wh:.2f} Wh.")

    effective_percent = (usable_energy / learned_full_total * 100.0) if learned_full_total > 0 else None
    consumption = _estimate_consumption(samples, model_cfg)
    explanations.extend(consumption.explanation)

    eta_nominal = _eta_seconds(usable_energy, consumption.nominal_w)
    eta_pessimistic = _eta_seconds(usable_energy, consumption.pessimistic_w)
    eta_optimistic = _eta_seconds(usable_energy, consumption.optimistic_w)

    confidence = min(gauge_confidences or [0.0])
    confidence = min(confidence, consumption.confidence)
    if threshold_mismatch:
        confidence = min(confidence, 0.85)
        explanations.append("Configured charge thresholds currently mismatch sysfs readback; model confidence is reduced.")
    if latest.ac_online is True:
        confidence = min(confidence, 0.45)
        explanations.append("Latest sample is AC-connected, so discharge runtime ETA is unavailable or low confidence.")

    return PackEstimate(
        wall_time=latest.wall_time,
        wall_iso=latest.wall_iso,
        total_raw_percent=latest.total_raw_percent,
        effective_pack_percent=effective_percent,
        total_energy_now_wh=total_energy,
        usable_energy_wh=usable_energy,
        learned_pack_full_wh=learned_full_total if learned_full_total > 0 else None,
        eta_seconds_nominal=eta_nominal,
        eta_seconds_pessimistic=eta_pessimistic,
        eta_seconds_optimistic=eta_optimistic,
        confidence=max(0.0, min(1.0, confidence)),
        explanation=_dedupe(explanations),
        batteries=battery_estimates,
    )


def samples_from_rows(rows: list[Any]) -> list[ModelSample]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        seq = int(row["seq"])
        sample = grouped.get(seq)
        if sample is None:
            sample = {
                "seq": seq,
                "wall_time": float(row["wall_time"]),
                "wall_iso": str(row["wall_iso"]),
                "monotonic_time": float(row["monotonic_time"]),
                "ac_online": _row_bool(row["ac_online"]),
                "total_raw_percent": _row_float(row["total_computed_percent"]),
                "total_energy_now_wh": _micro_to_unit(row["total_energy_now_uwh"]),
                "batteries": {},
            }
            grouped[seq] = sample
        name = str(row["battery_name"])
        sample["batteries"][name] = ModelBatterySample(
            name=name,
            status=str(row["status"]) if row["status"] is not None else None,
            raw_percent=_row_float(row["capacity_percent"]),
            computed_percent=_row_float(row["computed_percent"]),
            energy_now_wh=_micro_to_unit(row["energy_now_uwh"]),
            energy_full_wh=_micro_to_unit(row["energy_full_uwh"]),
            power_now_w=_micro_to_unit(row["power_now_uw"]),
        )
    return [
        ModelSample(
            seq=grouped[seq]["seq"],
            wall_time=grouped[seq]["wall_time"],
            wall_iso=grouped[seq]["wall_iso"],
            monotonic_time=grouped[seq]["monotonic_time"],
            ac_online=grouped[seq]["ac_online"],
            total_raw_percent=grouped[seq]["total_raw_percent"],
            total_energy_now_wh=grouped[seq]["total_energy_now_wh"],
            batteries=grouped[seq]["batteries"],
        )
        for seq in sorted(grouped)
    ]


def estimate_to_text(estimate: PackEstimate) -> str:
    lines = [
        f"Time: {estimate.wall_iso or 'n/a'}",
        f"Raw total percent: {_format_optional_float(estimate.total_raw_percent, precision=1)}%",
        f"Effective percent: {_format_optional_float(estimate.effective_pack_percent, precision=1)}%",
        f"Usable energy: {_format_optional_float(estimate.usable_energy_wh, precision=2)} Wh",
        f"Learned pack full: {_format_optional_float(estimate.learned_pack_full_wh, precision=2)} Wh",
        f"ETA nominal: {_format_duration(estimate.eta_seconds_nominal)}",
        f"ETA pessimistic: {_format_duration(estimate.eta_seconds_pessimistic)}",
        f"ETA optimistic: {_format_duration(estimate.eta_seconds_optimistic)}",
        f"Confidence: {estimate.confidence:.2f}",
        "",
        "Batteries:",
    ]
    for battery in estimate.batteries:
        lines.append(
            "  "
            + f"{battery.battery_name}: role={battery.active_role} raw={_format_optional_float(battery.raw_percent, precision=1)}% "
            + f"computed={_format_optional_float(battery.computed_percent, precision=1)}% "
            + f"usable={_format_optional_float(battery.usable_energy_wh, precision=2)} Wh "
            + f"gauge_confidence={battery.gauge_confidence:.2f}"
        )
    lines.append("")
    lines.append("Reasons:")
    lines.extend(f"  - {item}" for item in estimate.explanation)
    return "\n".join(lines)


def estimate_to_json(estimate: PackEstimate) -> str:
    return json.dumps(estimate.to_dict(), ensure_ascii=False, indent=2)


def _estimate_consumption(samples: list[ModelSample], cfg: BatteryModelConfig) -> _ConsumptionEstimate:
    latest = samples[-1]
    pairs = _discharge_pairs(samples, cfg)
    windows = [
        ("short", cfg.eta_short_window_seconds),
        ("medium", cfg.eta_medium_window_seconds),
        ("long", cfg.eta_long_window_seconds),
    ]
    medians: dict[str, float] = {}
    counts: dict[str, int] = {}
    for name, seconds in windows:
        values = [watts for end_time, watts in pairs if end_time >= latest.wall_time - seconds]
        counts[name] = len(values)
        if len(values) >= cfg.min_eta_pairs:
            medians[name] = float(median(values))

    if not medians:
        return _ConsumptionEstimate(
            nominal_w=None,
            pessimistic_w=None,
            optimistic_w=None,
            explanation=["Not enough recent discharge-only samples for ETA."],
            confidence=0.35,
        )

    if "medium" in medians:
        nominal = medians["medium"]
        nominal_source = "medium"
    elif "short" in medians:
        nominal = medians["short"]
        nominal_source = "short"
    else:
        nominal = medians["long"]
        nominal_source = "long"
    explanation = [
        "ETA uses discharge-only samples and excludes AC, charging, AC transitions, and probable suspend gaps.",
        f"Nominal ETA uses {nominal_source} window consumption ({nominal:.2f} W).",
    ]
    available = list(medians.values())
    confidence = 0.85 if "medium" in medians else 0.65
    return _ConsumptionEstimate(
        nominal_w=nominal,
        pessimistic_w=max(available),
        optimistic_w=min(available),
        explanation=explanation,
        confidence=confidence,
    )


def _discharge_pairs(samples: list[ModelSample], cfg: BatteryModelConfig) -> list[tuple[float, float]]:
    transition_times = {
        samples[index].wall_time
        for index in range(1, len(samples))
        if samples[index - 1].ac_online != samples[index].ac_online
    }
    pairs: list[tuple[float, float]] = []
    for index in range(1, len(samples)):
        previous = samples[index - 1]
        current = samples[index]
        if previous.ac_online is not False or current.ac_online is not False:
            continue
        if _has_charging(previous) or _has_charging(current):
            continue
        if any(
            abs(previous.wall_time - transition) <= cfg.ac_transition_exclusion_seconds
            or abs(current.wall_time - transition) <= cfg.ac_transition_exclusion_seconds
            for transition in transition_times
        ):
            continue
        dt = current.monotonic_time - previous.monotonic_time
        wall_dt = current.wall_time - previous.wall_time
        if dt <= 0.0 or wall_dt <= 0.0:
            continue
        if dt > cfg.suspend_gap_seconds or wall_dt > cfg.suspend_gap_seconds:
            continue
        previous_energy = _total_energy(previous)
        current_energy = _total_energy(current)
        if previous_energy is None or current_energy is None:
            continue
        delta = current_energy - previous_energy
        if delta >= -0.001:
            continue
        watts = -delta / (dt / 3600.0)
        if watts > 0:
            pairs.append((current.wall_time, watts))
    return pairs


def _low_end_jump_batteries(db: BatteryDatabase, session_id: str, cfg: AuditorConfig) -> set[str]:
    findings = analyze_session_jumps(db, session_id, config=GaugeJumpConfig.from_auditor_config(cfg))
    return {finding.battery_name for finding in findings if finding.event_type == LOW_END_GAUGE_JUMP}


def _relearn_sample_counts(db: BatteryDatabase, session_id: str, cfg: AuditorConfig) -> dict[str, int]:
    rows = db.fetch_session_series(session_id)
    samples = samples_from_rows(rows)
    seq_by_time = {sample.wall_time: sample.seq for sample in samples}
    latest_seq = samples[-1].seq if samples else 0
    findings = analyze_session_relearn(db, session_id, config=RelearnConfig.from_auditor_config(cfg))
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.event_type != ENERGY_FULL_RELEARN:
            continue
        seq = seq_by_time.get(finding.wall_time, finding.current_seq)
        counts[finding.battery_name] = max(0, latest_seq - seq + 1)
    return counts


def _battery_role(
    name: str,
    battery: ModelBatterySample,
    active_discharging_names: set[str],
    ac_online: bool | None,
) -> str:
    status = _normalize_status(battery.status)
    if status == "charging":
        return CHARGING
    if status == "discharging" and (battery.power_now_w or 0.0) > 0.0:
        return ACTIVE_DISCHARGING
    if active_discharging_names and name not in active_discharging_names and (battery.energy_now_wh or 0.0) > 0.0:
        return RESERVE
    if status in {"not charging", "full"} or ac_online is not None:
        return IDLE
    return UNKNOWN


def _is_low_end(battery: ModelBatterySample, cfg: BatteryModelConfig) -> bool:
    percent = battery.computed_percent if battery.computed_percent is not None else battery.raw_percent
    return percent is not None and percent < cfg.low_end_percent


def _has_charging(sample: ModelSample) -> bool:
    return any(_normalize_status(battery.status) == "charging" for battery in sample.batteries.values())


def _total_energy(sample: ModelSample) -> float | None:
    values = [battery.energy_now_wh for battery in sample.batteries.values() if battery.energy_now_wh is not None]
    if not values:
        return None
    return sum(values)


def _eta_seconds(usable_energy_wh: float | None, watts: float | None) -> float | None:
    if usable_energy_wh is None or watts is None or watts <= 0.0:
        return None
    return (usable_energy_wh / watts) * 3600.0


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


def _format_optional_float(value: float | None, *, precision: int) -> str:
    if value is None:
        return "-"
    return f"{value:.{precision}f}"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    rounded = int(round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
