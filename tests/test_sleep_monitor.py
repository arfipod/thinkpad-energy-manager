from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.collector import BatteryCollector
from battery_auditor.core.sleep_monitor import (
    ABOUT_TO_SLEEP,
    RESUME_SAMPLE_TAKEN,
    RESUMED,
    SLEEP_MONITOR_UNAVAILABLE,
    LogindSleepMonitor,
    SleepMonitor,
    SleepMonitorEvent,
    make_sleep_monitor_event,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_mocked_prepare_for_sleep_events_are_persisted(tmp_path: Path) -> None:
    cfg = AuditorConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        sysfs_power_supply_dir=FIXTURE,
        interval_seconds=10.0,
    )
    collector = BatteryCollector(cfg, sleep_monitor_factory=_fake_prepare_for_sleep_monitor)

    result = collector.run(
        name="sleep-monitor",
        duration_seconds=0.05,
        recover_open_sessions=False,
    )

    event_types = [row["event_type"] for row in collector.db.fetch_events(result.session_id, limit=100)]
    assert ABOUT_TO_SLEEP in event_types
    assert RESUMED in event_types
    assert RESUME_SAMPLE_TAKEN in event_types


def test_logind_monitor_missing_dbus_dependency_does_not_crash(monkeypatch: Any) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "dbus_next":
            raise ImportError("missing dbus-next for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monitor = LogindSleepMonitor(lambda _event: None)

    unavailable = monitor.start()

    assert unavailable is not None
    assert "dbus-next" in unavailable.reason


def test_unavailable_sleep_monitor_records_warning_and_continues(tmp_path: Path) -> None:
    cfg = AuditorConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        sysfs_power_supply_dir=FIXTURE,
        interval_seconds=0.01,
    )
    collector = BatteryCollector(cfg, sleep_monitor_factory=_unavailable_monitor)

    result = collector.run(
        name="sleep-monitor-unavailable",
        duration_seconds=0.03,
        recover_open_sessions=False,
    )

    event_types = [row["event_type"] for row in collector.db.fetch_events(result.session_id, limit=100)]
    assert SLEEP_MONITOR_UNAVAILABLE in event_types
    assert result.samples > 0


def _fake_prepare_for_sleep_monitor(callback: Callable[[SleepMonitorEvent], None]) -> SleepMonitor:
    return _FakeMonitor(callback)


def _unavailable_monitor(_callback: Callable[[SleepMonitorEvent], None]) -> SleepMonitor:
    return _UnavailableMonitor()


class _FakeMonitor(SleepMonitor):
    def __init__(self, callback: Callable[[SleepMonitorEvent], None]) -> None:
        self.callback = callback

    def start(self) -> None:
        self.callback(make_sleep_monitor_event(ABOUT_TO_SLEEP))
        self.callback(make_sleep_monitor_event(RESUMED))
        return None

    def stop(self) -> None:
        return None


class _UnavailableMonitor(SleepMonitor):
    def start(self) -> Any:
        from battery_auditor.core.sleep_monitor import SleepMonitorUnavailable

        return SleepMonitorUnavailable(reason="not available in test")

    def stop(self) -> None:
        return None
