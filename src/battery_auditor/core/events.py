from __future__ import annotations

from dataclasses import dataclass, field

from battery_auditor.config import AuditorConfig
from battery_auditor.core.models import BatterySnapshot, Event, SystemSnapshot
from battery_auditor.core.thresholds import (
    STATUS_MISMATCH,
    STATUS_OK,
    STATUS_UNKNOWN,
    status_for_snapshot,
)


@dataclass(slots=True)
class EventDetector:
    cfg: AuditorConfig
    previous: SystemSnapshot | None = None
    low_seen: bool = False
    critical_seen: bool = False
    last_active_discharge: str | None = None
    threshold_warned: set[str] = field(default_factory=set)
    threshold_status: dict[str, str] = field(default_factory=dict)

    def process(self, snap: SystemSnapshot) -> list[Event]:
        events: list[Event] = []
        if self.previous is not None:
            events.extend(self._ac_events(self.previous, snap))
            events.extend(self._battery_events(self.previous, snap))
            events.extend(self._loop_events(self.previous, snap))
        events.extend(self._threshold_events(snap))
        events.extend(self._low_events(snap))
        self.previous = snap
        return events

    def _ac_events(self, prev: SystemSnapshot, cur: SystemSnapshot) -> list[Event]:
        if prev.ac_online == cur.ac_online:
            return []
        if cur.ac_online is True:
            return [Event("AC_CONNECTED", "info", "AC adapter connected.")]
        if cur.ac_online is False:
            return [Event("AC_DISCONNECTED", "info", "AC adapter disconnected.")]
        return [Event("AC_STATE_UNKNOWN", "warning", "Could not determine AC adapter state.")]

    def _battery_events(self, prev: SystemSnapshot, cur: SystemSnapshot) -> list[Event]:
        events: list[Event] = []
        prev_by_name = {b.name: b for b in prev.batteries}
        cur_by_name = {b.name: b for b in cur.batteries}

        active_discharge = self._active_discharging_battery(cur)
        if active_discharge != self.last_active_discharge:
            if active_discharge is not None:
                events.append(
                    Event(
                        "BATTERY_SWITCH",
                        "info",
                        f"Active discharging battery: {active_discharge}.",
                        battery_name=active_discharge,
                        details={"previous_active": self.last_active_discharge, "current_active": active_discharge},
                    )
                )
            self.last_active_discharge = active_discharge

        for name, battery in cur_by_name.items():
            prev_b = prev_by_name.get(name)
            if prev_b is None:
                events.append(Event("BATTERY_APPEARED", "info", f"New battery detected: {name}.", name))
                continue
            if prev_b.status != battery.status:
                events.append(
                    Event(
                        "BATTERY_STATUS_CHANGE",
                        "info",
                        f"{name}: status {prev_b.status!r} -> {battery.status!r}.",
                        battery_name=name,
                        details={"old": prev_b.status, "new": battery.status},
                    )
                )
            events.extend(self._jump_events(prev_b, battery))
            events.extend(self._voltage_events(prev_b, battery))

        for name in set(prev_by_name) - set(cur_by_name):
            events.append(Event("BATTERY_DISAPPEARED", "warning", f"Battery {name} stopped appearing in sysfs.", name))
        return events

    def _jump_events(self, prev: BatterySnapshot, cur: BatterySnapshot) -> list[Event]:
        events: list[Event] = []
        threshold = self.cfg.percent_jump_threshold
        if prev.capacity_percent is not None and cur.capacity_percent is not None:
            delta = cur.capacity_percent - prev.capacity_percent
            if abs(delta) >= threshold:
                events.append(
                    Event(
                        "PERCENT_JUMP",
                        "warning",
                        f"{cur.name}: reported percentage jumped by {delta:+.1f} points.",
                        battery_name=cur.name,
                        details={"previous": prev.capacity_percent, "current": cur.capacity_percent, "delta": delta},
                    )
                )
        if prev.computed_percent is not None and cur.computed_percent is not None:
            delta = cur.computed_percent - prev.computed_percent
            if abs(delta) >= threshold:
                events.append(
                    Event(
                        "COMPUTED_PERCENT_JUMP",
                        "warning",
                        f"{cur.name}: Wh-based computed percentage jumped by {delta:+.1f} points.",
                        battery_name=cur.name,
                        details={"previous": prev.computed_percent, "current": cur.computed_percent, "delta": delta},
                    )
                )
        return events

    def _voltage_events(self, prev: BatterySnapshot, cur: BatterySnapshot) -> list[Event]:
        if not prev.voltage_now_uv or not cur.voltage_now_uv:
            return []
        if prev.voltage_now_uv <= 0:
            return []
        delta_pct = ((cur.voltage_now_uv - prev.voltage_now_uv) / prev.voltage_now_uv) * 100.0
        if delta_pct <= -self.cfg.voltage_sag_percent_threshold:
            return [
                Event(
                    "VOLTAGE_SAG",
                    "warning",
                    f"{cur.name}: quick voltage sag of {delta_pct:.1f}%.",
                    battery_name=cur.name,
                    details={"previous_uv": prev.voltage_now_uv, "current_uv": cur.voltage_now_uv, "delta_pct": delta_pct},
                )
            ]
        return []

    def _threshold_events(self, cur: SystemSnapshot) -> list[Event]:
        events: list[Event] = []
        for battery in cur.batteries:
            expected = self.cfg.expected_thresholds.get(battery.name)
            if expected is None:
                continue
            actual_start = battery.charge_control_start_threshold
            actual_stop = battery.charge_control_end_threshold
            if actual_start is None and battery.charge_start_threshold is not None:
                actual_start = battery.charge_start_threshold
            if actual_stop is None and battery.charge_stop_threshold is not None:
                actual_stop = battery.charge_stop_threshold
            mismatch = False
            details: dict[str, int | None] = {
                "expected_start": expected.start,
                "expected_stop": expected.stop,
                "actual_start": actual_start,
                "actual_stop": actual_stop,
            }
            status = status_for_snapshot(battery.name, expected, actual_start, actual_stop)
            previous_status = self.threshold_status.get(battery.name)
            self.threshold_status[battery.name] = status
            if status == STATUS_MISMATCH:
                mismatch = True
            if mismatch and previous_status != STATUS_MISMATCH:
                events.append(
                    Event(
                        "THRESHOLD_MISMATCH",
                        "warning",
                        f"{battery.name}: actual charge thresholds do not match expected thresholds.",
                        battery_name=battery.name,
                        details=details,
                    )
                )
            elif status == STATUS_UNKNOWN and previous_status != STATUS_UNKNOWN:
                events.append(
                    Event(
                        "THRESHOLD_UNKNOWN",
                        "info",
                        f"{battery.name}: charge threshold readback is unknown.",
                        battery_name=battery.name,
                        details=details,
                    )
                )
            elif status == STATUS_OK and previous_status in {STATUS_MISMATCH, STATUS_UNKNOWN}:
                events.append(
                    Event(
                        "THRESHOLD_RESTORED",
                        "info",
                        f"{battery.name}: charge thresholds match expected values again.",
                        battery_name=battery.name,
                        details=details,
                    )
                )
        return events

    def _low_events(self, cur: SystemSnapshot) -> list[Event]:
        pct = cur.total_computed_percent
        if pct is None:
            return []
        events: list[Event] = []
        if pct <= self.cfg.low_total_percent and not self.low_seen:
            self.low_seen = True
            events.append(
                Event(
                    "LOW_BATTERY",
                    "warning",
                    f"Low total level: {pct:.1f}%.",
                    details={"total_computed_percent": pct},
                )
            )
        if pct <= self.cfg.critical_total_percent and not self.critical_seen:
            self.critical_seen = True
            events.append(
                Event(
                    "CRITICAL_BATTERY",
                    "critical",
                    f"Critical total level: {pct:.1f}%.",
                    details={"total_computed_percent": pct},
                )
            )
        return events

    def _loop_events(self, prev: SystemSnapshot, cur: SystemSnapshot) -> list[Event]:
        expected = self.cfg.interval_seconds
        elapsed = cur.monotonic_time - prev.monotonic_time
        if expected > 0 and elapsed > expected * self.cfg.sample_delay_warn_factor:
            return [
                Event(
                    "MISSED_SAMPLE_WINDOW",
                    "warning",
                    f"High effective interval: {elapsed:.2f}s for target {expected:.2f}s.",
                    details={"expected_seconds": expected, "actual_seconds": elapsed},
                )
            ]
        return []

    @staticmethod
    def _active_discharging_battery(snap: SystemSnapshot) -> str | None:
        discharging = [
            b.name
            for b in snap.batteries
            if (b.status or "").lower() == "discharging" and (b.power_now_uw or 0) > 0
        ]
        if not discharging:
            return None
        return "+".join(sorted(discharging))
