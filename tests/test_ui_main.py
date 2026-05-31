from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication  # noqa: E402

from battery_auditor.config import AuditorConfig  # noqa: E402
from battery_auditor.core.database import BatteryDatabase  # noqa: E402
from battery_auditor.core.sysfs import read_snapshot  # noqa: E402
from battery_auditor.ui.main import MainWindow  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_main_window_keeps_last_chart_on_database_error(tmp_path: Path, monkeypatch: Any) -> None:
    app = QApplication.instance() or QApplication([])
    cfg = AuditorConfig(data_dir=tmp_path, db_path=tmp_path / "test.sqlite3", sysfs_power_supply_dir=FIXTURE)
    writer = BatteryDatabase(cfg.resolved_db_path(), cfg)
    writer.init_schema()
    writer.start_session("s1", "test", cfg.to_json())
    writer.insert_snapshot("s1", 0, read_snapshot(FIXTURE), [])
    writer.close()

    window = MainWindow(cfg)
    window.refresh_chart(force=True)
    assert window.chart.series
    original_items = dict(window.chart._plot_items)

    def fail_fetch(*args: object, **kwargs: object) -> list[Any]:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(window.db, "fetch_session_series", fail_fetch)

    window.refresh_chart(force=True)

    assert window.db_available is False
    assert window._database_recovery_disabled is True
    assert window.chart._plot_items == original_items
    assert "last successfully loaded chart" in window.chart_status_label.text()

    window.close()
    app.processEvents()
