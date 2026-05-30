from __future__ import annotations

from pathlib import Path

from battery_auditor.core.sysfs import read_snapshot, read_system_load_metrics

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_read_snapshot_from_fixture() -> None:
    snap = read_snapshot(FIXTURE)
    assert snap.ac_online is True
    assert len(snap.batteries) == 2
    by_name = {b.name: b for b in snap.batteries}
    assert by_name["BAT0"].manufacturer == "SANYO"
    assert by_name["BAT0"].model_name == "00HW022"
    assert round(by_name["BAT0"].computed_percent or 0, 1) == 8.9
    assert round(by_name["BAT1"].computed_percent or 0, 1) == 79.8
    assert round(snap.total_computed_percent or 0, 1) == 46.7


def test_health_percent() -> None:
    snap = read_snapshot(FIXTURE)
    by_name = {b.name: b for b in snap.batteries}
    assert round(by_name["BAT0"].health_percent or 0, 1) == 78.3
    assert round(by_name["BAT1"].health_percent or 0, 1) == 90.3


def test_system_load_metrics_return_stable_keys() -> None:
    metrics, counters = read_system_load_metrics()
    second_metrics, _second_counters = read_system_load_metrics(counters)

    assert "system_cpu_percent" in metrics
    assert "system_memory_used_percent" in metrics
    assert "system_disk_write_bytes_per_second" in metrics
    assert "system_cpu_percent" in second_metrics
