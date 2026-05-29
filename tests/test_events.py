from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from battery_auditor.config import AuditorConfig, TlpThresholdExpectation
from battery_auditor.core.events import EventDetector
from battery_auditor.core.sysfs import read_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_threshold_mismatch_event() -> None:
    cfg = AuditorConfig(sysfs_power_supply_dir=FIXTURE)
    cfg.expected_thresholds["BAT0"] = TlpThresholdExpectation(start=75, stop=80)
    detector = EventDetector(cfg)
    snap = read_snapshot(FIXTURE)
    events = detector.process(snap)
    assert any(e.event_type == "THRESHOLD_MISMATCH" and e.battery_name == "BAT0" for e in events)


def test_percent_jump_event() -> None:
    cfg = AuditorConfig(sysfs_power_supply_dir=FIXTURE, percent_jump_threshold=5)
    detector = EventDetector(cfg)
    snap1 = read_snapshot(FIXTURE)
    detector.process(snap1)
    snap2 = deepcopy(snap1)
    snap2.batteries[0].capacity_percent = 1.0
    events = detector.process(snap2)
    assert any(e.event_type == "PERCENT_JUMP" for e in events)


def test_sample_delay_uses_effective_interval() -> None:
    cfg = AuditorConfig(sysfs_power_supply_dir=FIXTURE, interval_seconds=10.0)
    detector = EventDetector(cfg)
    snap1 = read_snapshot(FIXTURE)
    detector.process(snap1)
    snap2 = deepcopy(snap1)
    snap2.monotonic_time = snap1.monotonic_time + 10.0
    events = detector.process(snap2)
    assert not any(e.event_type == "MISSED_SAMPLE_WINDOW" for e in events)
