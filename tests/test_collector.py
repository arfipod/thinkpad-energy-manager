from __future__ import annotations

from pathlib import Path

from battery_auditor.config import AuditorConfig
from battery_auditor.core.collector import BatteryCollector

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_collector_uses_runtime_interval_for_delay_events(tmp_path: Path) -> None:
    cfg = AuditorConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "test.sqlite3",
        sysfs_power_supply_dir=FIXTURE,
        interval_seconds=0.001,
    )
    collector = BatteryCollector(cfg)
    result = collector.run(
        name="interval-override",
        interval_seconds=0.05,
        duration_seconds=0.13,
        recover_open_sessions=False,
    )
    events = collector.db.fetch_events(result.session_id)
    assert not any(row["event_type"] == "MISSED_SAMPLE_WINDOW" for row in events)
