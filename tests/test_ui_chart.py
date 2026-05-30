from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication  # noqa: E402

from battery_auditor.ui.main import BatteryChart  # noqa: E402


def test_battery_chart_zoom_modes_and_fit_full_graph() -> None:
    app = QApplication.instance() or QApplication([])
    chart = BatteryChart()

    chart.set_data(
        "computed_percent",
        "%",
        [
            ("BAT0", [(0.0, 45.0), (10.0, 55.0)]),
            ("BAT1", [(0.0, 20.0), (10.0, 30.0)]),
        ],
    )

    assert _mouse_enabled(chart) == [True, True]

    chart.zoom_axes_combo.setCurrentText("X axis")
    assert _mouse_enabled(chart) == [True, False]

    chart.zoom_axes_combo.setCurrentText("Y axis")
    assert _mouse_enabled(chart) == [False, True]

    chart.zoom_axes_combo.setCurrentText("Both axes")
    chart.plot.setXRange(2.0, 3.0, padding=0.0)
    chart.plot.setYRange(46.0, 47.0, padding=0.0)
    chart.fit_full_graph()

    x_range, y_range = chart.plot.plotItem.vb.viewRange()
    assert x_range[0] <= 0.0
    assert x_range[1] >= 10.0
    assert y_range[0] <= 20.0
    assert y_range[1] >= 55.0

    app.processEvents()


def _mouse_enabled(chart: BatteryChart) -> list[bool]:
    return list(chart.plot.plotItem.vb.state["mouseEnabled"])
