from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig, load_config
from battery_auditor.core.analyzer import export_session_csv
from battery_auditor.core.battery_model import estimate_session, estimate_to_text
from battery_auditor.core.database import BatteryDatabase, repair_database
from battery_auditor.core.runtime import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    CollectorStatus,
    collect_runtime_status,
)
from battery_auditor.core.sysfs import read_snapshot
from battery_auditor.core.system_controls import CommandResult, SystemControls
from battery_auditor.core.thresholds import (
    STATUS_MISMATCH,
    analyze_session_thresholds,
    plan_threshold_restores,
)
from battery_auditor.core.tlp import TlpClient
from battery_auditor.ui.session_manager import SessionManager

BLACKBOX_SERVICE = "thinkpad-energy-manager-blackbox.service"
PERSISTENT_DATABASE_ERROR_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "invalid page number",
    "unsupported database schema",
)

try:
    import pyqtgraph as pg  # type: ignore[import-untyped]
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QColor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSlider,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QToolTip,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - depends on optional UI extra
    raise SystemExit(
        "PySide6 and pyqtgraph are required for the Qt UI. Install with:\n"
        "  python -m pip install 'thinkpad-energy-manager[ui]'\n"
        "or install the relevant PySide6 packages plus pyqtgraph."
    ) from exc


class InactivityGuard:
    def __init__(self, parent: QWidget) -> None:
        self.parent = parent
        self._inhibit_process: subprocess.Popen[bytes] | None = None
        self._screensaver_window_id: str | None = None
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self.poke)

    def enable(self) -> None:
        if self.active:
            return
        inhibit = shutil.which("systemd-inhibit")
        if inhibit is not None:
            try:
                self._inhibit_process = subprocess.Popen(
                    [
                        inhibit,
                        "--what=sleep:idle",
                        "--mode=block",
                        "--who=ThinkPad Energy Manager",
                        "--why=Battery measurement in progress",
                        "sleep",
                        "infinity",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError:
                self._inhibit_process = None
        self._screensaver_window_id = str(int(self.parent.winId()))
        self._run_quiet("xdg-screensaver", "suspend", self._screensaver_window_id)
        self._timer.start(20_000)
        self.poke()

    def disable(self) -> None:
        self._timer.stop()
        if self._screensaver_window_id is not None:
            self._run_quiet("xdg-screensaver", "resume", self._screensaver_window_id)
            self._screensaver_window_id = None
        if self._inhibit_process is not None:
            _terminate_process_group(self._inhibit_process)
            self._inhibit_process = None

    @property
    def active(self) -> bool:
        return self._timer.isActive() or (
            self._inhibit_process is not None and self._inhibit_process.poll() is None
        )

    def poke(self) -> None:
        self._run_quiet("xdg-screensaver", "reset")
        self._run_quiet("xset", "s", "reset")

    @staticmethod
    def _run_quiet(*args: str) -> None:
        if shutil.which(args[0]) is None:
            return
        try:
            subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return


class BatteryChart(QWidget):
    """Interactive chart for recorded battery series."""

    def __init__(self) -> None:
        super().__init__()
        self.series: list[tuple[str, list[tuple[float, float]]]] = []
        self.events: list[dict[str, Any]] = []
        self.y_label = ""
        self._data_signature: tuple[Any, ...] | None = None
        self._plot_items: dict[str, Any] = {}
        self.setMinimumHeight(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.zoom_axes_combo = QComboBox()
        self.zoom_axes_combo.addItems(["Both axes", "X axis", "Y axis"])
        self.zoom_axes_combo.setToolTip("Choose which axes respond to mouse drag and wheel zoom.")
        self.zoom_axes_combo.currentIndexChanged.connect(self._apply_zoom_axes)
        self.fit_full_button = QPushButton("Fit full graph")
        self.fit_full_button.setToolTip("Reset the view so every plotted point is visible.")
        self.fit_full_button.clicked.connect(self.fit_full_graph)
        controls.addWidget(QLabel("Zoom"))
        controls.addWidget(self.zoom_axes_combo)
        controls.addWidget(self.fit_full_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "minutes from start")
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.addLegend(offset=(12, 12))
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        layout.addWidget(self.plot)

    def set_data(
        self,
        title: str,
        y_label: str,
        series: list[tuple[str, list[tuple[float, float]]]],
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.y_label = y_label
        self.series = series
        self.events = events or []
        signature = (
            title,
            y_label,
            tuple(name for name, _points in series),
            tuple(
                (
                    float(event["minute"]),
                    str(event.get("type", "")),
                    str(event.get("severity", "")),
                    str(event.get("battery", "")),
                    str(event.get("message", "")),
                )
                for event in self.events
            ),
        )
        structure_changed = signature != self._data_signature
        should_fit = structure_changed
        self._data_signature = signature
        self.plot.setUpdatesEnabled(False)
        try:
            if structure_changed:
                self.plot.clear()
                self._plot_items.clear()
            self.plot.setTitle(title)
            self.plot.setLabel("left", y_label)
            self.plot.setLabel("bottom", "minutes from start")

            palette = [
                "#2563eb",
                "#dc2626",
                "#16a34a",
                "#9333ea",
                "#ea580c",
                "#0891b2",
                "#be123c",
                "#4f46e5",
            ]
            live_names: set[str] = set()
            for index, (name, points) in enumerate(series):
                live_names.add(name)
                color = palette[index % len(palette)]
                x_values = [x for x, _y in points]
                y_values = [y for _x, y in points]
                item = self._plot_items.get(name)
                if item is None:
                    item = self.plot.plot(
                        x_values,
                        y_values,
                        name=name,
                        pen=pg.mkPen(color, width=2),
                        symbol="o",
                        symbolBrush=color,
                        symbolPen=color,
                        symbolSize=5,
                    )
                    self._plot_items[name] = item
                else:
                    item.setData(x_values, y_values)
            for stale_name in set(self._plot_items) - live_names:
                self.plot.removeItem(self._plot_items.pop(stale_name))
            if structure_changed:
                self._plot_events()
            self._apply_zoom_axes()
            if should_fit:
                self.fit_full_graph()
        finally:
            self.plot.setUpdatesEnabled(True)

    def fit_full_graph(self) -> None:
        points = [point for _name, series_points in self.series for point in series_points]
        event_minutes = [float(event["minute"]) for event in self.events]
        if not points and not event_minutes:
            self.plot.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            self.plot.autoRange()
            return
        x_values = [x for x, _y in points] + event_minutes
        y_values = [y for _x, y in points] or [0.0, 1.0]
        self.plot.setRange(
            xRange=self._expanded_range(min(x_values), max(x_values)),
            yRange=self._expanded_range(min(y_values), max(y_values)),
            padding=0.05,
        )

    def _apply_zoom_axes(self, _index: int | None = None) -> None:
        mode = self.zoom_axes_combo.currentText()
        self.plot.setMouseEnabled(x=mode in {"Both axes", "X axis"}, y=mode in {"Both axes", "Y axis"})

    @staticmethod
    def _expanded_range(minimum: float, maximum: float) -> tuple[float, float]:
        if minimum != maximum:
            return (minimum, maximum)
        padding = max(abs(minimum) * 0.05, 1.0)
        return (minimum - padding, maximum + padding)

    def _mouse_moved(self, pos: Any) -> None:
        if not (self.series or self.events) or not self.plot.sceneBoundingRect().contains(pos):
            QToolTip.hideText()
            return
        view_point = self.plot.plotItem.vb.mapSceneToView(pos)
        mouse_x = float(view_point.x())
        nearest = self._nearest_values(mouse_x)
        nearby_events = self._nearby_events(mouse_x)
        if not nearest and not nearby_events:
            QToolTip.hideText()
            return

        anchor_x = nearest[0][1] if nearest else float(nearby_events[0]["minute"])
        lines = [f"t = {anchor_x:.2f} min"]
        for name, _x, value in nearest:
            lines.append(f"{name}: {value:.2f} {self.y_label}")
        for event in nearby_events:
            battery = f" [{event['battery']}]" if event.get("battery") else ""
            lines.append(f"{event['severity']} {event['type']}{battery}: {event['message']}")
        global_pos = self.plot.mapToGlobal(pos.toPoint())
        QToolTip.showText(global_pos, "\n".join(lines), self.plot)

    def _nearest_values(self, mouse_x: float) -> list[tuple[str, float, float]]:
        nearest: list[tuple[str, float, float, float]] = []
        for name, points in self.series:
            if not points:
                continue
            x, y = min(points, key=lambda point: abs(point[0] - mouse_x))
            nearest.append((name, x, y, abs(x - mouse_x)))
        nearest.sort(key=lambda item: item[3])
        if not nearest:
            return []
        anchor_x = nearest[0][1]
        return [(name, x, y) for name, x, y, _distance in nearest if abs(x - anchor_x) <= 0.05]

    def _plot_events(self) -> None:
        for event in self.events:
            minute = float(event["minute"])
            color = self._event_color(str(event["severity"]))
            line = pg.InfiniteLine(
                pos=minute,
                angle=90,
                movable=False,
                pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
            )
            line.setToolTip(self._event_tooltip(event))
            self.plot.addItem(line)
        points = [point for _name, series_points in self.series for point in series_points]
        y_anchor = max((y for _x, y in points), default=1.0)
        for event in self.events[:40]:
            minute = float(event["minute"])
            color = self._event_color(str(event["severity"]))
            label = pg.TextItem(str(event["type"]), color=color, anchor=(0, 1))
            label.setPos(minute, y_anchor)
            label.setAngle(90)
            label.setToolTip(self._event_tooltip(event))
            self.plot.addItem(label)

    def _nearby_events(self, mouse_x: float) -> list[dict[str, Any]]:
        return [event for event in self.events if abs(float(event["minute"]) - mouse_x) <= 0.05][:8]

    @staticmethod
    def _event_color(severity: str) -> str:
        return {
            "critical": "#dc2626",
            "error": "#dc2626",
            "warning": "#f59e0b",
            "info": "#64748b",
        }.get(severity.lower(), "#64748b")

    @staticmethod
    def _event_tooltip(event: dict[str, Any]) -> str:
        battery = f"\nBattery: {event['battery']}" if event.get("battery") else ""
        return (
            f"{event['type']} ({event['severity']})"
            f"{battery}\n"
            f"t = {float(event['minute']):.2f} min\n"
            f"{event['message']}"
        )


class MainWindow(QMainWindow):
    def __init__(self, cfg: AuditorConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self._ensure_database_exists()
        self.db = BatteryDatabase(cfg.resolved_db_path(), cfg, read_only=True)
        self.db_available = True
        self.db_error: str | None = None
        self._database_recovery_disabled = False
        try:
            self.db.init_schema()
            integrity = self.db.check_integrity(quick=True)
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(integrity))
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
        self._chart_session_id: str | None = None
        self._chart_rows: list[Any] = []
        self._chart_last_seq: int | None = None
        self._active_collector_session_id: str | None = None
        self._user_pinned_chart_session = False
        self.inactivity_guard = InactivityGuard(self)
        self.load_process: subprocess.Popen[bytes] | None = None
        self.system_controls = SystemControls()

        self.setWindowTitle("ThinkPad Energy Manager")
        self.resize(1080, 760)

        tabs = QTabWidget()
        tabs.addTab(self._build_overview_tab(), "Status")
        tabs.addTab(self._build_recorder_tab(), "Recording")
        tabs.addTab(self._build_sessions_tab(), "Sessions")
        tabs.addTab(self._build_charts_tab(), "Charts")
        tabs.addTab(self._build_events_tab(), "Events")
        tabs.addTab(self._build_tlp_tab(), "TLP")
        tabs.addTab(self._build_system_controls_tab(), "ThinkPad")
        self.setCentralWidget(tabs)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh_all)
        self.menuBar().addAction(refresh_action)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(int(self.cfg.ui_refresh_seconds * 1000))

        self.refresh_all()
        if self.db_available and hasattr(self, "sessions_manager"):
            self.sessions_manager.refresh()

    def _build_overview_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        row = QHBoxLayout()
        self.db_label = QLabel(f"DB: {self.cfg.resolved_db_path()}")
        self.db_status_label = QLabel("")
        refresh = QPushButton("Refresh status")
        repair = QPushButton("Repair database")
        refresh.clicked.connect(self.refresh_all)
        repair.clicked.connect(self.repair_database_from_ui)
        self.repair_db_button = repair
        row.addWidget(self.db_label)
        row.addStretch(1)
        row.addWidget(repair)
        row.addWidget(refresh)
        self.live_text = QPlainTextEdit()
        self.live_text.setReadOnly(True)
        self.estimate_text = QPlainTextEdit()
        self.estimate_text.setReadOnly(True)
        self.estimate_text.setMaximumHeight(220)
        layout.addLayout(row)
        layout.addWidget(self.db_status_label)
        layout.addWidget(QLabel("Estimate"))
        layout.addWidget(self.estimate_text)
        layout.addWidget(QLabel("Live sysfs snapshot"))
        layout.addWidget(self.live_text)
        return widget

    def _build_recorder_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self.record_name = QLineEdit("manual-qt-session")
        self.record_interval = QDoubleSpinBox()
        self.record_interval.setRange(0.5, 3600.0)
        self.record_interval.setValue(self.cfg.interval_seconds)
        self.record_interval.setSuffix(" s")
        self.record_mode = QComboBox()
        self.record_mode.addItems(["diagnostic", "passive", "blackbox"])
        form.addRow("Name", self.record_name)
        form.addRow("Interval", self.record_interval)
        form.addRow("Mode", self.record_mode)
        layout.addLayout(form)

        status_form = QFormLayout()
        self.collector_state_label = QLabel("UNKNOWN")
        self.collector_pid_label = QLabel("none")
        self.collector_session_label = QLabel("none")
        self.collector_heartbeat_label = QLabel("none")
        self.collector_samples_label = QLabel("unknown")
        status_form.addRow("Collector", self.collector_state_label)
        status_form.addRow("PID", self.collector_pid_label)
        status_form.addRow("Session", self.collector_session_label)
        status_form.addRow("Last heartbeat", self.collector_heartbeat_label)
        status_form.addRow("Samples", self.collector_samples_label)
        layout.addLayout(status_form)

        guard_row = QHBoxLayout()
        self.prevent_idle_checkbox = QCheckBox("Prevent suspend / idle login")
        self.prevent_idle_checkbox.toggled.connect(self.toggle_inactivity_guard)
        self.guard_status_label = QLabel("inactive")
        guard_row.addWidget(self.prevent_idle_checkbox)
        guard_row.addWidget(self.guard_status_label)
        guard_row.addStretch(1)
        layout.addLayout(guard_row)

        row = QHBoxLayout()
        self.start_record_button = QPushButton("Start collector")
        self.pause_record_button = QPushButton("Pause collector")
        self.resume_record_button = QPushButton("Resume collector")
        self.stop_record_button = QPushButton("Stop collector")
        self.force_stop_record_button = QPushButton("Force stop collector")
        self.start_record_button.clicked.connect(self.start_collector_process)
        self.pause_record_button.clicked.connect(self.pause_collector)
        self.resume_record_button.clicked.connect(self.resume_collector)
        self.stop_record_button.clicked.connect(lambda: self.stop_collector_process(force=False))
        self.force_stop_record_button.clicked.connect(lambda: self.stop_collector_process(force=True))
        row.addWidget(self.start_record_button)
        row.addWidget(self.pause_record_button)
        row.addWidget(self.resume_record_button)
        row.addWidget(self.stop_record_button)
        row.addWidget(self.force_stop_record_button)
        row.addStretch(1)
        layout.addLayout(row)

        service_row = QHBoxLayout()
        start_blackbox_service = QPushButton("Start black-box service")
        stop_blackbox_service = QPushButton("Stop black-box service")
        blackbox_service_status = QPushButton("Service status")
        start_blackbox_service.clicked.connect(self.start_blackbox_service)
        stop_blackbox_service.clicked.connect(self.stop_blackbox_service)
        blackbox_service_status.clicked.connect(self.show_blackbox_service_status)
        service_row.addWidget(start_blackbox_service)
        service_row.addWidget(stop_blackbox_service)
        service_row.addWidget(blackbox_service_status)
        service_row.addStretch(1)
        layout.addLayout(service_row)

        load_form = QHBoxLayout()
        cpu_count = os.cpu_count() or 1
        self.load_cpu_workers = QSpinBox()
        self.load_cpu_workers.setRange(0, max(1, cpu_count * 2))
        self.load_cpu_workers.setValue(max(1, cpu_count // 2))
        self.load_cpu_duty = QSpinBox()
        self.load_cpu_duty.setRange(0, 100)
        self.load_cpu_duty.setValue(50)
        self.load_cpu_duty.setSuffix(" %")
        self.load_memory_mib = QSpinBox()
        self.load_memory_mib.setRange(0, self._max_memory_load_mib())
        self.load_memory_mib.setValue(min(256, self.load_memory_mib.maximum()))
        self.load_memory_mib.setSuffix(" MiB")
        self.load_disk_mib_s = QDoubleSpinBox()
        self.load_disk_mib_s.setRange(0.0, 256.0)
        self.load_disk_mib_s.setDecimals(1)
        self.load_disk_mib_s.setValue(4.0)
        self.load_disk_mib_s.setSuffix(" MiB/s")
        self.start_medium_load_button = QPushButton("Start medium load")
        self.start_high_load_button = QPushButton("Start high load")
        self.stop_load_button = QPushButton("Stop load")
        self.load_status_label = QLabel("load stopped")
        self.start_medium_load_button.clicked.connect(lambda: self.start_activity_load("medium"))
        self.start_high_load_button.clicked.connect(lambda: self.start_activity_load("high"))
        self.stop_load_button.clicked.connect(self.stop_activity_load)
        load_form.addWidget(QLabel("CPU workers"))
        load_form.addWidget(self.load_cpu_workers)
        load_form.addWidget(QLabel("CPU duty"))
        load_form.addWidget(self.load_cpu_duty)
        load_form.addWidget(QLabel("Memory"))
        load_form.addWidget(self.load_memory_mib)
        load_form.addWidget(QLabel("Disk write"))
        load_form.addWidget(self.load_disk_mib_s)
        load_form.addWidget(self.start_medium_load_button)
        load_form.addWidget(self.start_high_load_button)
        load_form.addWidget(self.stop_load_button)
        load_form.addWidget(self.load_status_label)
        load_form.addStretch(1)
        layout.addLayout(load_form)

        self.collector_output = QTextEdit()
        self.collector_output.setReadOnly(True)
        layout.addWidget(self.collector_output)
        layout.addWidget(QLabel("Collectors started from the UI are detached CLI processes; closing this window does not stop measurement."))
        return widget

    def _build_sessions_tab(self) -> QWidget:
        self.sessions_manager = SessionManager(
            self.cfg,
            self.db,
            open_in_chart=self.open_session_in_chart,
            refresh_main=lambda: self.refresh_all(prefer_running_session=True),
        )
        return self.sessions_manager

    def _build_charts_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        controls = QHBoxLayout()
        self.session_combo = QComboBox()
        self.metric_combo = QComboBox()
        self.metric_combo.addItems([
            "computed_percent",
            "capacity_percent",
            "energy_now_wh",
            "power_now_w",
            "voltage_now_v",
            "health_percent",
            "system_cpu_percent",
            "system_load_1m",
            "system_memory_used_percent",
            "system_disk_read_mib_s",
            "system_disk_write_mib_s",
            "display_brightness_percent",
            "wifi_enabled",
            "bluetooth_enabled",
            "event_count",
        ])
        refresh = QPushButton("Update chart")
        export_csv = QPushButton("Export CSV")
        refresh.clicked.connect(self.refresh_sessions_and_chart)
        export_csv.clicked.connect(self.export_selected_session_csv)
        self.session_combo.currentIndexChanged.connect(self._session_combo_changed)
        self.metric_combo.currentIndexChanged.connect(self.refresh_chart)
        controls.addWidget(QLabel("Session"))
        controls.addWidget(self.session_combo, 1)
        controls.addWidget(QLabel("Metric"))
        controls.addWidget(self.metric_combo)
        controls.addWidget(refresh)
        controls.addWidget(export_csv)
        self.chart = BatteryChart()
        self.chart_status_label = QLabel("")
        layout.addLayout(controls)
        layout.addWidget(self.chart_status_label)
        layout.addWidget(self.chart)
        return widget

    def _build_events_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        refresh = QPushButton("Update events")
        refresh.clicked.connect(self.refresh_events)
        self.events_table = QTableWidget(0, 5)
        self.events_table.setHorizontalHeaderLabels(["Time", "Severity", "Type", "Battery", "Message"])
        layout.addWidget(refresh)
        layout.addWidget(self.events_table)
        return widget

    def _build_tlp_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        buttons = QHBoxLayout()
        stat_b = QPushButton("tlp-stat -b")
        stat_c = QPushButton("tlp-stat -c")
        stat_s = QPushButton("tlp-stat -s")
        stat_b.clicked.connect(lambda: self.run_tlp_stat("battery"))
        stat_c.clicked.connect(lambda: self.run_tlp_stat("config"))
        stat_s.clicked.connect(lambda: self.run_tlp_stat("system"))
        buttons.addWidget(stat_b)
        buttons.addWidget(stat_c)
        buttons.addWidget(stat_s)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        form = QHBoxLayout()
        self.tlp_battery = QComboBox()
        self.tlp_battery.addItems(["BAT0", "BAT1"])
        self.tlp_start = QSpinBox()
        self.tlp_start.setRange(0, 99)
        self.tlp_start.setValue(75)
        self.tlp_stop = QSpinBox()
        self.tlp_stop.setRange(1, 100)
        self.tlp_stop.setValue(80)
        apply = QPushButton("Apply temporary setcharge")
        recalibrate = QPushButton("Recalibrate battery")
        apply.clicked.connect(self.apply_tlp_setcharge)
        recalibrate.clicked.connect(self.recalibrate_battery)
        form.addWidget(QLabel("Battery"))
        form.addWidget(self.tlp_battery)
        form.addWidget(QLabel("Start"))
        form.addWidget(self.tlp_start)
        form.addWidget(QLabel("Stop"))
        form.addWidget(self.tlp_stop)
        form.addWidget(apply)
        form.addWidget(recalibrate)
        form.addStretch(1)
        layout.addLayout(form)

        threshold_row = QHBoxLayout()
        threshold_refresh = QPushButton("Refresh threshold status")
        threshold_restore = QPushButton("Restore thresholds")
        threshold_refresh.clicked.connect(self.refresh_thresholds)
        threshold_restore.clicked.connect(self.show_threshold_restore_commands)
        threshold_row.addWidget(QLabel("Configured vs current sysfs thresholds"))
        threshold_row.addStretch(1)
        threshold_row.addWidget(threshold_refresh)
        threshold_row.addWidget(threshold_restore)
        layout.addLayout(threshold_row)

        self.thresholds_table = QTableWidget(0, 6)
        self.thresholds_table.setHorizontalHeaderLabels(
            ["Battery", "Configured", "Current sysfs", "Mismatch", "Last OK", "Last mismatch"]
        )
        layout.addWidget(self.thresholds_table)

        self.tlp_output = QPlainTextEdit()
        self.tlp_output.setReadOnly(True)
        layout.addWidget(self.tlp_output)
        return widget

    def _build_system_controls_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        top = QHBoxLayout()
        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self.refresh_system_controls)
        self.system_backend_label = QLabel("")
        top.addWidget(refresh)
        top.addWidget(self.system_backend_label)
        top.addStretch(1)
        layout.addLayout(top)

        brightness_group = QGroupBox("Display brightness")
        brightness_layout = QVBoxLayout(brightness_group)
        brightness_form = QHBoxLayout()
        self.backlight_combo = QComboBox()
        self.backlight_slider = QSlider(Qt.Orientation.Horizontal)
        self.backlight_slider.setRange(0, 100)
        self.backlight_value = QSpinBox()
        self.backlight_value.setRange(0, 100)
        self.backlight_value.setSuffix(" %")
        apply_backlight = QPushButton("Apply")
        self.backlight_combo.currentIndexChanged.connect(self._select_backlight)
        self.backlight_slider.valueChanged.connect(self.backlight_value.setValue)
        self.backlight_value.valueChanged.connect(self.backlight_slider.setValue)
        apply_backlight.clicked.connect(self.apply_backlight)
        brightness_form.addWidget(QLabel("Device"))
        brightness_form.addWidget(self.backlight_combo)
        brightness_form.addWidget(self.backlight_slider, 1)
        brightness_form.addWidget(self.backlight_value)
        brightness_form.addWidget(apply_backlight)
        brightness_layout.addLayout(brightness_form)
        self.backlight_table = QTableWidget(0, 5)
        self.backlight_table.setHorizontalHeaderLabels(["Device", "Current", "Max", "Percent", "Writable"])
        brightness_layout.addWidget(self.backlight_table)
        layout.addWidget(brightness_group)

        lights_group = QGroupBox("ThinkPad and system lights")
        lights_layout = QVBoxLayout(lights_group)
        lights_form = QHBoxLayout()
        self.led_combo = QComboBox()
        self.led_value = QSpinBox()
        self.led_value.setRange(0, 255)
        apply_led = QPushButton("Apply")
        self.led_combo.currentIndexChanged.connect(self._select_led)
        apply_led.clicked.connect(self.apply_led)
        lights_form.addWidget(QLabel("LED"))
        lights_form.addWidget(self.led_combo, 1)
        lights_form.addWidget(QLabel("Brightness"))
        lights_form.addWidget(self.led_value)
        lights_form.addWidget(apply_led)
        lights_layout.addLayout(lights_form)
        self.led_table = QTableWidget(0, 5)
        self.led_table.setHorizontalHeaderLabels(["LED", "Current", "Max", "Trigger", "Writable"])
        lights_layout.addWidget(self.led_table)
        layout.addWidget(lights_group)

        radios_group = QGroupBox("Wireless radios")
        radios_layout = QVBoxLayout(radios_group)
        radios_form = QHBoxLayout()
        self.rfkill_combo = QComboBox()
        enable_radio = QPushButton("Enable")
        disable_radio = QPushButton("Disable")
        enable_radio.clicked.connect(lambda: self.apply_rfkill(True))
        disable_radio.clicked.connect(lambda: self.apply_rfkill(False))
        radios_form.addWidget(QLabel("Radio"))
        radios_form.addWidget(self.rfkill_combo, 1)
        radios_form.addWidget(enable_radio)
        radios_form.addWidget(disable_radio)
        radios_layout.addLayout(radios_form)
        self.rfkill_table = QTableWidget(0, 6)
        self.rfkill_table.setHorizontalHeaderLabels(["Device", "Type", "Name", "Soft block", "Hard block", "Enabled"])
        radios_layout.addWidget(self.rfkill_table)
        layout.addWidget(radios_group)

        power_group = QGroupBox("Energy actions")
        power_layout = QVBoxLayout(power_group)
        screen_row = QHBoxLayout()
        self.screen_timeout_seconds = QSpinBox()
        self.screen_timeout_seconds.setRange(0, 86_400)
        self.screen_timeout_seconds.setValue(600)
        self.screen_timeout_seconds.setSuffix(" s")
        apply_screen_timeout = QPushButton("Set screen timeout")
        apply_screen_timeout.clicked.connect(self.apply_screen_timeout)
        screen_row.addWidget(QLabel("Screen off / idle"))
        screen_row.addWidget(self.screen_timeout_seconds)
        screen_row.addWidget(apply_screen_timeout)
        screen_row.addStretch(1)
        power_layout.addLayout(screen_row)

        sleep_form = QHBoxLayout()
        self.sleep_source = QComboBox()
        self.sleep_source.addItems(["battery", "ac"])
        self.sleep_timeout_seconds = QSpinBox()
        self.sleep_timeout_seconds.setRange(0, 86_400)
        self.sleep_timeout_seconds.setValue(900)
        self.sleep_timeout_seconds.setSuffix(" s")
        self.sleep_action = QComboBox()
        self.sleep_action.addItems(["suspend", "hibernate", "nothing"])
        apply_sleep = QPushButton("Set inactivity action")
        apply_sleep.clicked.connect(self.apply_sleep_timeout)
        sleep_form.addWidget(QLabel("When on"))
        sleep_form.addWidget(self.sleep_source)
        sleep_form.addWidget(QLabel("after"))
        sleep_form.addWidget(self.sleep_timeout_seconds)
        sleep_form.addWidget(self.sleep_action)
        sleep_form.addWidget(apply_sleep)
        sleep_form.addStretch(1)
        power_layout.addLayout(sleep_form)

        action_row = QHBoxLayout()
        suspend = QPushButton("Suspend now")
        hibernate = QPushButton("Hibernate now")
        poweroff = QPushButton("Power off now")
        suspend.clicked.connect(lambda: self.run_power_action("suspend"))
        hibernate.clicked.connect(lambda: self.run_power_action("hibernate"))
        poweroff.clicked.connect(lambda: self.run_power_action("poweroff"))
        self.poweroff_delay_minutes = QSpinBox()
        self.poweroff_delay_minutes.setRange(1, 24 * 60)
        self.poweroff_delay_minutes.setValue(30)
        self.poweroff_delay_minutes.setSuffix(" min")
        schedule_poweroff = QPushButton("Schedule power off")
        cancel_poweroff = QPushButton("Cancel scheduled power off")
        schedule_poweroff.clicked.connect(self.schedule_poweroff)
        cancel_poweroff.clicked.connect(self.cancel_poweroff)
        action_row.addWidget(suspend)
        action_row.addWidget(hibernate)
        action_row.addWidget(poweroff)
        action_row.addWidget(QLabel("Delay"))
        action_row.addWidget(self.poweroff_delay_minutes)
        action_row.addWidget(schedule_poweroff)
        action_row.addWidget(cancel_poweroff)
        action_row.addStretch(1)
        power_layout.addLayout(action_row)
        layout.addWidget(power_group)

        self.system_controls_output = QPlainTextEdit()
        self.system_controls_output.setReadOnly(True)
        layout.addWidget(self.system_controls_output)
        return widget

    def refresh_all(self, _checked: bool = False, *, prefer_running_session: bool = False) -> None:
        if not self.db_available:
            self._try_restore_database()
        status = self.refresh_collector_status()
        if prefer_running_session:
            self._user_pinned_chart_session = False
        active_session_id = self._active_session_id_from_status(status)
        should_follow_active = prefer_running_session or not self._user_pinned_chart_session
        preferred_session_id = active_session_id if should_follow_active else None
        self.refresh_sessions(
            prefer_running_session=prefer_running_session,
            preferred_session_id=preferred_session_id,
        )
        self.refresh_estimate()
        self.refresh_live_snapshot()
        self.refresh_chart()
        self.refresh_events()
        self.refresh_thresholds()
        self.refresh_system_controls()

    def refresh_sessions(
        self,
        *,
        prefer_running_session: bool = False,
        preferred_session_id: str | None = None,
    ) -> None:
        if not self.db_available:
            self._show_database_error()
            return
        current = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        running_index: int | None = None
        items: list[tuple[str, str]] = []
        try:
            for row in self.db.list_sessions(limit=200):
                status = row["ended_reason"] or "running"
                sample_count = row["real_sample_count"]
                last_sample = row["last_sample_iso"] or "no samples"
                label = f"{status} | {row['started_at_iso']} | {row['id']} | {sample_count} samples | last {last_sample}"
                items.append((label, str(row["id"])))
                if status == "running" and running_index is None:
                    running_index = len(items) - 1
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self._show_database_error()
            return
        was_blocked = self.session_combo.blockSignals(True)
        try:
            self._sync_session_combo_items(items)
            preferred_index = self.session_combo.findData(preferred_session_id) if preferred_session_id else -1
            if preferred_index >= 0:
                self.session_combo.setCurrentIndex(preferred_index)
            elif prefer_running_session and running_index is not None:
                self.session_combo.setCurrentIndex(running_index)
            elif current:
                index = self.session_combo.findData(current)
                if index >= 0:
                    self.session_combo.setCurrentIndex(index)
                elif running_index is not None:
                    self.session_combo.setCurrentIndex(running_index)
            elif running_index is not None:
                self.session_combo.setCurrentIndex(running_index)
        finally:
            self.session_combo.blockSignals(was_blocked)

    def _sync_session_combo_items(self, items: list[tuple[str, str]]) -> None:
        same_order = self.session_combo.count() == len(items) and all(
            str(self.session_combo.itemData(index)) == session_id
            for index, (_label, session_id) in enumerate(items)
        )
        if same_order:
            for index, (label, _session_id) in enumerate(items):
                if self.session_combo.itemText(index) != label:
                    self.session_combo.setItemText(index, label)
            return
        self.session_combo.clear()
        for label, session_id in items:
            self.session_combo.addItem(label, session_id)

    def refresh_collector_status(self) -> CollectorStatus:
        db = self.db if self.db_available else None
        status = collect_runtime_status(self.cfg, db)
        payload = status.to_dict()
        self._active_collector_session_id = self._active_session_id_from_status(status)
        if not hasattr(self, "collector_state_label"):
            return status
        self.collector_state_label.setText(str(payload["state"]))
        self.collector_pid_label.setText("none" if payload["pid"] is None else str(payload["pid"]))
        session = payload.get("current_session_id") or "none"
        if payload.get("current_session_name"):
            session = f"{session} ({payload['current_session_name']})"
        self.collector_session_label.setText(str(session))
        age = payload.get("last_heartbeat_age_seconds")
        last_iso = payload.get("last_heartbeat_iso") or "none"
        self.collector_heartbeat_label.setText(last_iso if age is None else f"{last_iso} ({float(age):.1f}s ago)")
        sample_count = payload.get("sample_count")
        self.collector_samples_label.setText("unknown" if sample_count is None else str(sample_count))

        active = status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}
        self.start_record_button.setEnabled(not active)
        self.pause_record_button.setEnabled(status.state == STATUS_RUNNING)
        self.resume_record_button.setEnabled(status.state == STATUS_PAUSED or status.control.paused)
        self.stop_record_button.setEnabled(active)
        self.force_stop_record_button.setEnabled(active)
        self.refresh_activity_status()
        return status

    def refresh_live_snapshot(self) -> None:
        snap = read_snapshot(self.cfg.sysfs_power_supply_dir)
        data = snap.to_dict()
        self.live_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

    def refresh_estimate(self) -> None:
        if not hasattr(self, "estimate_text"):
            return
        if not self.db_available:
            self.estimate_text.setPlainText("Database unavailable.")
            return
        session_id = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        if not session_id:
            self.estimate_text.setPlainText("No session selected.")
            return
        try:
            estimate = estimate_session(self.db, str(session_id), self.cfg)
        except (ValueError, sqlite3.DatabaseError) as exc:
            if isinstance(exc, sqlite3.DatabaseError):
                self._set_database_unavailable(exc)
                self._show_database_error()
            self.estimate_text.setPlainText("Estimate unavailable.")
            return
        self.estimate_text.setPlainText(estimate_to_text(estimate))

    def refresh_chart(self, _checked: bool = False, *, force: bool = False) -> None:
        if not self.db_available:
            self._show_database_error()
            if not self.chart.series and not self.chart.events:
                self.chart.set_data("Database unavailable", "", [])
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            self.chart.set_data("No session", "", [])
            return
        metric = self.metric_combo.currentText()
        session_text = str(session_id)
        try:
            if not force and self._chart_session_id == session_text and self._chart_rows:
                new_rows = self.db.fetch_session_series(session_text, after_seq=self._chart_last_seq)
                if new_rows:
                    self._chart_rows.extend(new_rows)
                    self._chart_last_seq = max(int(row["seq"]) for row in self._chart_rows)
                rows = self._chart_rows
            else:
                rows = self.db.fetch_session_series(session_text)
                self._chart_session_id = session_text
                self._chart_rows = list(rows)
                self._chart_last_seq = max((int(row["seq"]) for row in rows), default=None)
            events = self.db.fetch_events(session_text, limit=1000)
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self._show_database_error()
            if not self.chart.series and not self.chart.events:
                self.chart.set_data("Database unavailable", metric, [])
            return
        if not rows:
            self.chart.set_data("No data", metric, [])
            return
        first_time = float(rows[0]["wall_time"])
        chart_events = self._chart_events(events, first_time)
        grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
        if metric == "event_count":
            grouped["events"] = self._event_count_series(chart_events)
        else:
            seen_system_seq: set[int] = set()
            for row in rows:
                if self._is_system_metric(metric):
                    seq = int(row["seq"])
                    if seq in seen_system_seq:
                        continue
                    seen_system_seq.add(seq)
                value = self._metric_value(row, metric)
                if value is None:
                    continue
                minutes = (float(row["wall_time"]) - first_time) / 60.0
                name = "system" if self._is_system_metric(metric) else str(row["battery_name"])
                grouped[name].append((minutes, float(value)))
        self.chart.set_data(f"{metric} — {session_id}", metric, sorted(grouped.items()), chart_events)
        self._clear_database_error_message()

    def refresh_sessions_and_chart(self, _checked: bool = False, *, prefer_running_session: bool = False) -> None:
        preferred_session_id = self._active_collector_session_id if prefer_running_session else None
        self.refresh_sessions(
            prefer_running_session=prefer_running_session,
            preferred_session_id=preferred_session_id,
        )
        self.refresh_chart(force=True)

    def open_session_in_chart(self, session_id: str) -> None:
        self._user_pinned_chart_session = True
        index = self.session_combo.findData(session_id)
        if index < 0:
            self.refresh_sessions()
            index = self.session_combo.findData(session_id)
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self.refresh_chart(force=True)

    def _session_combo_changed(self, _index: int) -> None:
        current = self.session_combo.currentData()
        if current is not None and str(current) != (self._active_collector_session_id or ""):
            self._user_pinned_chart_session = True
        self.refresh_chart(force=True)

    @staticmethod
    def _chart_events(events: list[Any], first_time: float) -> list[dict[str, Any]]:
        chart_events: list[dict[str, Any]] = []
        for event in events:
            wall_time = event["wall_time"]
            if wall_time is None:
                continue
            chart_events.append(
                {
                    "minute": (float(wall_time) - first_time) / 60.0,
                    "type": str(event["event_type"]),
                    "severity": str(event["severity"]),
                    "battery": None if event["battery_name"] is None else str(event["battery_name"]),
                    "message": str(event["message"]),
                }
            )
        return chart_events

    @staticmethod
    def _event_count_series(events: list[dict[str, Any]]) -> list[tuple[float, float]]:
        counts: dict[float, int] = defaultdict(int)
        for event in events:
            counts[round(float(event["minute"]), 3)] += 1
        return [(minute, float(count)) for minute, count in sorted(counts.items())]

    def refresh_events(self) -> None:
        if not self.db_available:
            self._show_database_error()
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            self.events_table.setRowCount(0)
            return
        try:
            events = self.db.fetch_events(str(session_id), limit=1000)
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self._show_database_error()
            return
        self.events_table.setRowCount(len(events))
        for row_idx, event in enumerate(events):
            values = [
                str(event["wall_time"] or ""),
                str(event["severity"]),
                str(event["event_type"]),
                str(event["battery_name"] or ""),
                str(event["message"]),
            ]
            for col, value in enumerate(values):
                self.events_table.setItem(row_idx, col, QTableWidgetItem(value))
        self.events_table.resizeColumnsToContents()

    def refresh_thresholds(self) -> None:
        if not hasattr(self, "thresholds_table"):
            return
        if not self.db_available:
            self.thresholds_table.setRowCount(0)
            return
        session_id = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        if not session_id:
            self.thresholds_table.setRowCount(0)
            return
        try:
            statuses = analyze_session_thresholds(self.db, str(session_id), self.cfg)
        except (ValueError, sqlite3.DatabaseError) as exc:
            if isinstance(exc, sqlite3.DatabaseError):
                self._set_database_unavailable(exc)
                self._show_database_error()
            self.thresholds_table.setRowCount(0)
            return
        self.thresholds_table.setRowCount(len(statuses))
        for row_idx, status in enumerate(statuses):
            values = [
                status.battery_name,
                self._format_threshold_pair(status.configured_start_threshold, status.configured_stop_threshold),
                self._format_threshold_pair(status.sysfs_start_threshold, status.sysfs_stop_threshold),
                "yes" if status.mismatch else "no",
                self._short_ui_iso(status.last_ok_wall_iso),
                self._short_ui_iso(status.last_mismatch_wall_iso),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if status.status == STATUS_MISMATCH:
                    item.setBackground(QColor("#4a1f1f"))
                self.thresholds_table.setItem(row_idx, col, item)
        self.thresholds_table.resizeColumnsToContents()

    def refresh_system_controls(self) -> None:
        if not hasattr(self, "system_backend_label"):
            return
        status = self.system_controls.backend_status()
        self.system_backend_label.setText(
            "Backends: "
            f"xset={'yes' if status.xset_available else 'no'}, "
            f"gsettings={'yes' if status.gsettings_available else 'no'}, "
            f"systemctl={'yes' if status.systemctl_available else 'no'}, "
            f"shutdown={'yes' if status.shutdown_available else 'no'}"
        )

        backlights = self.system_controls.list_backlights()
        self._backlight_devices = {device.name: device for device in backlights}
        self._sync_device_combo(self.backlight_combo, [(device.name, device.name) for device in backlights])
        self._populate_backlight_table(backlights)
        self._select_backlight()

        leds = self.system_controls.list_leds()
        self._led_devices = {device.name: device for device in leds}
        self._sync_device_combo(self.led_combo, [(device.name, device.name) for device in leds])
        self._populate_led_table(leds)
        self._select_led()

        radios = self.system_controls.list_rfkill()
        self._rfkill_devices = {device.name: device for device in radios}
        self._sync_device_combo(
            self.rfkill_combo,
            [(device.name, f"{device.kind}: {device.label} ({device.name})") for device in radios],
        )
        self._populate_rfkill_table(radios)

    def _populate_backlight_table(self, devices: list[Any]) -> None:
        self.backlight_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            values = [
                device.name,
                str(device.brightness),
                str(device.max_brightness),
                f"{device.percent:.0f}%",
                "yes" if device.writable else "no",
            ]
            self._set_table_row(self.backlight_table, row, values)
        self.backlight_table.resizeColumnsToContents()

    def _populate_led_table(self, devices: list[Any]) -> None:
        self.led_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            values = [
                device.name,
                str(device.brightness),
                str(device.max_brightness),
                device.trigger or "",
                "yes" if device.writable else "no",
            ]
            self._set_table_row(self.led_table, row, values)
        self.led_table.resizeColumnsToContents()

    def _populate_rfkill_table(self, devices: list[Any]) -> None:
        self.rfkill_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            values = [
                device.name,
                device.kind,
                device.label,
                self._format_optional_bool(device.soft_blocked),
                self._format_optional_bool(device.hard_blocked),
                self._format_optional_bool(device.enabled),
            ]
            self._set_table_row(self.rfkill_table, row, values)
        self.rfkill_table.resizeColumnsToContents()

    @staticmethod
    def _set_table_row(table: QTableWidget, row: int, values: list[str]) -> None:
        for col, value in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(value))

    @staticmethod
    def _format_optional_bool(value: bool | None) -> str:
        if value is None:
            return "unknown"
        return "yes" if value else "no"

    @staticmethod
    def _sync_device_combo(combo: QComboBox, items: list[tuple[str, str]]) -> None:
        current = combo.currentData()
        same_order = combo.count() == len(items) and all(
            str(combo.itemData(index)) == key for index, (key, _label) in enumerate(items)
        )
        if same_order:
            for index, (_key, label) in enumerate(items):
                if combo.itemText(index) != label:
                    combo.setItemText(index, label)
            return
        was_blocked = combo.blockSignals(True)
        try:
            combo.clear()
            for key, label in items:
                combo.addItem(label, key)
            if current is not None:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(was_blocked)

    def _select_backlight(self, _index: int | None = None) -> None:
        if not hasattr(self, "_backlight_devices"):
            return
        name = self.backlight_combo.currentData()
        device = self._backlight_devices.get(str(name)) if name is not None else None
        if device is None:
            self.backlight_slider.setEnabled(False)
            self.backlight_value.setEnabled(False)
            return
        value = int(round(device.percent))
        self.backlight_slider.setEnabled(device.writable)
        self.backlight_value.setEnabled(device.writable)
        self.backlight_slider.setValue(value)
        self.backlight_value.setValue(value)

    def _select_led(self, _index: int | None = None) -> None:
        if not hasattr(self, "_led_devices"):
            return
        name = self.led_combo.currentData()
        device = self._led_devices.get(str(name)) if name is not None else None
        if device is None:
            self.led_value.setEnabled(False)
            return
        self.led_value.setEnabled(device.writable)
        self.led_value.setRange(0, max(0, int(device.max_brightness)))
        self.led_value.setValue(int(device.brightness))

    def apply_backlight(self) -> None:
        name = self.backlight_combo.currentData()
        if name is None:
            QMessageBox.information(self, "Display brightness", "No display backlight was detected.")
            return
        try:
            device = self.system_controls.set_backlight_percent(str(name), self.backlight_value.value())
        except (OSError, ValueError) as exc:
            self._append_system_control_message(f"Display brightness failed: {exc}")
            QMessageBox.warning(self, "Display brightness", str(exc))
            return
        self._append_system_control_message(f"{device.name}: brightness set to {device.percent:.0f}%.")
        self.refresh_system_controls()

    def apply_led(self) -> None:
        name = self.led_combo.currentData()
        if name is None:
            QMessageBox.information(self, "ThinkPad lights", "No LED devices were detected.")
            return
        try:
            device = self.system_controls.set_led_brightness(str(name), self.led_value.value())
        except (OSError, ValueError) as exc:
            self._append_system_control_message(f"LED control failed: {exc}")
            QMessageBox.warning(self, "ThinkPad lights", str(exc))
            return
        self._append_system_control_message(f"{device.name}: brightness set to {device.brightness}/{device.max_brightness}.")
        self.refresh_system_controls()

    def apply_rfkill(self, enabled: bool) -> None:
        name = self.rfkill_combo.currentData()
        if name is None:
            QMessageBox.information(self, "Wireless radios", "No rfkill radio devices were detected.")
            return
        try:
            device = self.system_controls.set_rfkill_enabled(str(name), enabled)
        except (OSError, ValueError) as exc:
            self._append_system_control_message(f"Radio control failed: {exc}")
            QMessageBox.warning(self, "Wireless radios", str(exc))
            return
        state = "enabled" if device.enabled else "disabled"
        self._append_system_control_message(f"{device.kind} {device.label}: {state}.")
        self.refresh_system_controls()

    def apply_screen_timeout(self) -> None:
        try:
            results = self.system_controls.set_screen_idle_timeout(self.screen_timeout_seconds.value())
        except (RuntimeError, OSError) as exc:
            self._append_system_control_message(f"Screen timeout failed: {exc}")
            QMessageBox.warning(self, "Screen timeout", str(exc))
            return
        self._append_system_control_results(results)

    def apply_sleep_timeout(self) -> None:
        try:
            result = self.system_controls.set_sleep_timeout(
                self.sleep_source.currentText(),
                self.sleep_timeout_seconds.value(),
                self.sleep_action.currentText(),
            )
        except (OSError, ValueError) as exc:
            self._append_system_control_message(f"Inactivity action failed: {exc}")
            QMessageBox.warning(self, "Inactivity action", str(exc))
            return
        self._append_system_control_results([result])
        if result.returncode != 0:
            QMessageBox.warning(self, "Inactivity action", result.combined_output())

    def run_power_action(self, action: str) -> None:
        reply = QMessageBox.question(
            self,
            "Power action",
            f"This will run 'systemctl {action}' now. Continue?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = self.system_controls.run_power_action(action)
        self._append_system_control_results([result])
        if result.returncode != 0:
            QMessageBox.warning(self, "Power action", result.combined_output())

    def schedule_poweroff(self) -> None:
        result = self.system_controls.schedule_poweroff(self.poweroff_delay_minutes.value())
        self._append_system_control_results([result])
        if result.returncode != 0:
            QMessageBox.warning(self, "Schedule power off", result.combined_output())

    def cancel_poweroff(self) -> None:
        result = self.system_controls.cancel_scheduled_poweroff()
        self._append_system_control_results([result])
        if result.returncode != 0:
            QMessageBox.warning(self, "Cancel power off", result.combined_output())

    def _append_system_control_results(self, results: list[CommandResult]) -> None:
        for result in results:
            command = " ".join(result.command)
            self._append_system_control_message(
                f"[{result.title}] exit {result.returncode}\n$ {command}\n{result.combined_output()}"
            )

    def _append_system_control_message(self, message: str) -> None:
        if hasattr(self, "system_controls_output"):
            self.system_controls_output.appendPlainText(message)

    @staticmethod
    def _format_threshold_pair(start: int | None, stop: int | None) -> str:
        if start is None or stop is None:
            return "-"
        return f"{start}/{stop}"

    @staticmethod
    def _short_ui_iso(value: str | None) -> str:
        if value is None:
            return "-"
        return value.replace("T", " ").split("+", maxsplit=1)[0]

    def export_selected_session_csv(self) -> None:
        if not self.db_available:
            QMessageBox.warning(self, "Database unavailable", self._database_error_message())
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            QMessageBox.information(self, "Export CSV", "Select a session before exporting.")
            return

        default_path = Path.home() / f"thinkpad-energy-manager-{session_id}.csv"
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export session CSV",
            str(default_path),
            "CSV files (*.csv);;All files (*)",
        )
        if not filename:
            return

        output = Path(filename).expanduser()
        if output.suffix.lower() != ".csv":
            output = output.with_suffix(".csv")
        try:
            export_session_csv(self.db, str(session_id), output)
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self._show_database_error()
            QMessageBox.warning(self, "Export CSV", self._database_error_message())
            return
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV", f"Could not write CSV:\n{exc}")
            return

        QMessageBox.information(self, "Export CSV", f"Exported session to:\n{output}")

    @staticmethod
    def _metric_value(row: Any, metric: str) -> float | None:
        if metric == "energy_now_wh":
            return None if row["energy_now_uwh"] is None else float(row["energy_now_uwh"]) / 1_000_000.0
        if metric == "power_now_w":
            return None if row["power_now_uw"] is None else float(row["power_now_uw"]) / 1_000_000.0
        if metric == "voltage_now_v":
            return None if row["voltage_now_uv"] is None else float(row["voltage_now_uv"]) / 1_000_000.0
        if metric == "system_disk_read_mib_s":
            return (
                None
                if row["system_disk_read_bytes_per_second"] is None
                else float(row["system_disk_read_bytes_per_second"]) / (1024.0 * 1024.0)
            )
        if metric == "system_disk_write_mib_s":
            return (
                None
                if row["system_disk_write_bytes_per_second"] is None
                else float(row["system_disk_write_bytes_per_second"]) / (1024.0 * 1024.0)
            )
        value = row[metric]
        return None if value is None else float(value)

    @staticmethod
    def _is_system_metric(metric: str) -> bool:
        return metric.startswith("system_") or metric in {
            "display_brightness_percent",
            "wifi_enabled",
            "bluetooth_enabled",
        }

    def toggle_inactivity_guard(self, enabled: bool) -> None:
        if enabled:
            self.inactivity_guard.enable()
        else:
            self.inactivity_guard.disable()
        self.guard_status_label.setText("active" if self.inactivity_guard.active else "inactive")

    def start_activity_load(self, profile: str) -> None:
        if self.load_process is not None and self.load_process.poll() is None:
            QMessageBox.information(self, "Activity load", "A synthetic load is already running.")
            return
        self._apply_load_profile(profile)
        args = [
            sys.executable,
            "-m",
            "battery_auditor.core.loadgen",
            "--cpu-workers",
            str(self.load_cpu_workers.value()),
            "--cpu-duty",
            f"{self.load_cpu_duty.value() / 100.0:.3f}",
            "--memory-mib",
            str(self.load_memory_mib.value()),
            "--disk-mib-s",
            f"{self.load_disk_mib_s.value():.1f}",
        ]
        log_path = self.cfg.data_dir.expanduser() / "activity-load.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab") as log:
                self.load_process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            QMessageBox.warning(self, "Activity load", f"Could not start synthetic load:\n{exc}")
            self.load_process = None
            return
        self.collector_output.append(f"Synthetic {profile} load started. Log: {log_path}")
        self.refresh_activity_status()

    def stop_activity_load(self) -> None:
        if self.load_process is not None:
            _terminate_process_group(self.load_process)
            self.load_process = None
        self.refresh_activity_status()

    def refresh_activity_status(self) -> None:
        if not hasattr(self, "load_status_label"):
            return
        running = self.load_process is not None and self.load_process.poll() is None
        self.load_status_label.setText("load running" if running else "load stopped")
        self.stop_load_button.setEnabled(running)
        self.start_medium_load_button.setEnabled(not running)
        self.start_high_load_button.setEnabled(not running)
        if hasattr(self, "guard_status_label"):
            self.guard_status_label.setText("active" if self.inactivity_guard.active else "inactive")

    def _apply_load_profile(self, profile: str) -> None:
        cpu_count = os.cpu_count() or 1
        if profile == "high":
            self.load_cpu_workers.setValue(cpu_count)
            self.load_cpu_duty.setValue(90)
            self.load_memory_mib.setValue(min(self.load_memory_mib.maximum(), max(512, self._max_memory_load_mib() // 4)))
            self.load_disk_mib_s.setValue(16.0)
            return
        self.load_cpu_workers.setValue(max(1, cpu_count // 2))
        self.load_cpu_duty.setValue(50)
        self.load_memory_mib.setValue(min(self.load_memory_mib.maximum(), 256))
        self.load_disk_mib_s.setValue(4.0)

    @staticmethod
    def _max_memory_load_mib() -> int:
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    total_kib = int(line.split()[1])
                    return max(128, min(8192, total_kib // 2048))
        except (OSError, ValueError, IndexError):
            pass
        return 4096

    def start_collector_process(self) -> None:
        if not self.db_available:
            QMessageBox.warning(self, "Database unavailable", self._database_error_message())
            return
        status = collect_runtime_status(self.cfg, self.db)
        if status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}:
            QMessageBox.information(self, "Collector", f"A collector already appears active (state={status.state}).")
            return
        if self.record_mode.currentText() == "blackbox":
            reply = QMessageBox.question(
                self,
                "Black-box mode",
                "Black-box mode is more reliable if you close the UI and run it as a service/CLI. Start it from the UI anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        args = [
            sys.executable,
            "-m",
            "battery_auditor.cli",
            "--db",
            str(self.cfg.resolved_db_path()),
            "--sysfs",
            str(self.cfg.sysfs_power_supply_dir),
            "collect",
            "--name",
            self.record_name.text() or "qt-session",
            "--interval",
            str(self.record_interval.value()),
            "--mode",
            self.record_mode.currentText(),
        ]
        log_path = self.cfg.data_dir.expanduser() / "collector-ui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab") as log:
                subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            QMessageBox.warning(self, "Collector", f"Could not start collector:\n{exc}")
            return
        self.collector_output.append(f"Collector started as a detached CLI process. Log: {log_path}")
        QTimer.singleShot(1000, lambda: self.refresh_all(prefer_running_session=True))

    def pause_collector(self) -> None:
        result = self._run_cli_command("pause")
        self._append_cli_result("Pause collector", result)
        self.refresh_all(prefer_running_session=True)

    def resume_collector(self) -> None:
        result = self._run_cli_command("resume")
        self._append_cli_result("Resume collector", result)
        self.refresh_all(prefer_running_session=True)

    def stop_collector_process(self, *, force: bool = False) -> None:
        if force:
            reply = QMessageBox.question(
                self,
                "Force stop collector",
                "Force stop sends SIGKILL. The active session may remain open until recover is run. Continue?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        args = ["stop", "--timeout", "5"]
        if force:
            args.append("--force")
        result = self._run_cli_command(*args)
        self._append_cli_result("Force stop collector" if force else "Stop collector", result)
        self.refresh_all(prefer_running_session=True)

    def _run_cli_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "battery_auditor.cli",
                "--db",
                str(self.cfg.resolved_db_path()),
                "--sysfs",
                str(self.cfg.sysfs_power_supply_dir),
                *args,
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    def _append_cli_result(self, title: str, result: subprocess.CompletedProcess[str]) -> None:
        output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        if not output:
            output = f"{title}: exit code {result.returncode}"
        self.collector_output.append(self._format_service_output(title, result.returncode, output))
        if result.returncode != 0:
            QMessageBox.warning(self, title, output)

    def start_blackbox_service(self) -> None:
        if not self.db_available:
            QMessageBox.warning(self, "Database unavailable", self._database_error_message())
            return
        result = self._run_systemctl("start", BLACKBOX_SERVICE)
        self._append_systemctl_result("Start black-box service", result, warn_on_failure=True)
        if result.returncode == 0:
            self.refresh_all(prefer_running_session=True)

    def stop_blackbox_service(self) -> None:
        result = self._run_systemctl("stop", BLACKBOX_SERVICE)
        self._append_systemctl_result("Stop black-box service", result, warn_on_failure=True)
        if result.returncode == 0:
            self.refresh_sessions_and_chart()

    def show_blackbox_service_status(self) -> None:
        result = self._run_systemctl(
            "show",
            BLACKBOX_SERVICE,
            "--no-pager",
            "--property=Id,Description,LoadState,ActiveState,SubState,UnitFileState,ExecMainPID,MemoryCurrent,CPUUsageNSec,FragmentPath,DropInPaths",
        )
        self._append_systemctl_result("Black-box service status", result, warn_on_failure=True)

    def _run_systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            check=False,
            text=True,
        )

    def _append_systemctl_result(
        self,
        title: str,
        result: subprocess.CompletedProcess[str],
        *,
        warn_on_failure: bool,
    ) -> None:
        output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        if not output:
            output = f"{title}: exit code {result.returncode}"
        self.collector_output.append(self._format_service_output(title, result.returncode, output))
        if warn_on_failure and result.returncode != 0:
            QMessageBox.warning(
                self,
                title,
                output
                + "\n\nIf the service is not installed, run ./scripts/install-user-service.sh first.",
            )

    def _format_service_output(self, title: str, returncode: int, output: str) -> str:
        title_color = "#22c55e" if returncode == 0 else "#ef4444"
        lines = [
            f'<p><b style="color:{title_color}">[{self._html_escape(title)}]</b></p>',
            '<pre style="white-space:pre-wrap; font-family:monospace">',
        ]
        for line in output.splitlines():
            lines.append(self._color_service_line(line))
        lines.append("</pre>")
        return "\n".join(lines)

    def _color_service_line(self, line: str) -> str:
        escaped = self._html_escape(line)
        lower = line.lower()
        if "active: active (running)" in lower or lower == "activestate=active":
            return f'<span style="color:#22c55e">{escaped}</span>'
        if (
            "active: inactive" in lower
            or "active: deactivating" in lower
            or lower == "activestate=inactive"
            or lower == "substate=dead"
        ):
            return f'<span style="color:#a3a3a3">{escaped}</span>'
        if "failed" in lower or "error" in lower or "database unavailable" in lower:
            return f'<span style="color:#ef4444">{escaped}</span>'
        if "warning" in lower or "refusing" in lower:
            return f'<span style="color:#f59e0b">{escaped}</span>'
        if re.match(r"^\s*(loaded|main pid|tasks|memory|cpu|cgroup|invocation):", lower):
            return f'<span style="color:#93c5fd">{escaped}</span>'
        if line.startswith("●"):
            return f'<span style="color:#e5e7eb"><b>{escaped}</b></span>'
        return escaped

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override name
        self.inactivity_guard.disable()
        self.stop_activity_load()
        super().closeEvent(event)

    @staticmethod
    def _html_escape(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def run_tlp_stat(self, section: str) -> None:
        client = TlpClient(use_sudo=True)
        if section == "battery":
            result = client.stat_battery()
        elif section == "config":
            result = client.stat_config()
        else:
            result = client.stat_system()
        self.tlp_output.setPlainText(result.combined_output())

    def apply_tlp_setcharge(self) -> None:
        battery = self.tlp_battery.currentText()
        start = int(self.tlp_start.value())
        stop = int(self.tlp_stop.value())
        try:
            result = TlpClient(use_sudo=True).setcharge(start, stop, battery)
        except ValueError as exc:
            QMessageBox.warning(self, "TLP", str(exc))
            return
        self.tlp_output.setPlainText(result.combined_output())

    def show_threshold_restore_commands(self) -> None:
        plans = plan_threshold_restores(self.cfg)
        if not plans:
            self.tlp_output.setPlainText("No configured thresholds are available to restore.")
            return
        commands = "\n".join(" ".join(plan.command) for plan in plans)
        self.tlp_output.setPlainText(
            "Threshold restore is intentionally manual from the UI so a sudo prompt cannot block the app.\n\n"
            "Commands that would be run:\n"
            f"{commands}\n\n"
            "Run this from a terminal after reviewing the commands:\n"
            "thinkpad-energy-manager thresholds restore --dry-run\n"
            "thinkpad-energy-manager thresholds restore --yes"
        )

    def recalibrate_battery(self) -> None:
        battery = self.tlp_battery.currentText()
        reply = QMessageBox.question(
            self,
            "Recalibrate battery",
            f"This will run 'sudo tlp recalibrate {battery}'. It can take a while and discharge the selected battery. Continue?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = TlpClient(use_sudo=True).recalibrate(battery)
        self.tlp_output.setPlainText(result.combined_output())

    def repair_database_from_ui(self) -> None:
        status = collect_runtime_status(self.cfg, self.db if self.db_available else None)
        active = status.to_dict().get("active_heartbeat_files", [])
        if status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN} or active:
            QMessageBox.warning(
                self,
                "Repair database",
                "A collector appears to be active or ambiguous. Stop it before repairing the database.\n\n"
                + "\n".join(str(path) for path in active[:5]),
            )
            return

        reply = QMessageBox.question(
            self,
            "Repair database",
            "This will rebuild the SQLite database, back up the current file, and replace it with the repaired copy. Continue?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.db.close()
        try:
            result = repair_database(self.cfg.resolved_db_path(), replace=True)
        except sqlite3.DatabaseError as exc:
            self._restore_database_after_failed_repair(exc)
            self._show_repair_failure_state(exc)
            QMessageBox.warning(self, "Repair database", f"Repair failed:\n{exc}")
            return
        except OSError as exc:
            self._restore_database_after_failed_repair(exc)
            self._show_repair_failure_state(exc)
            QMessageBox.warning(self, "Repair database", f"Repair failed:\n{exc}")
            return

        replacement = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg, read_only=True)
        try:
            replacement.init_schema()
        except sqlite3.DatabaseError as exc:
            replacement.close()
            self.db_available = False
            self.db_error = f"Repair succeeded but database could not be reopened: {exc}"
            self._show_database_error()
            QMessageBox.warning(self, "Repair database", self._database_error_message())
            return
        self.db = replacement
        if hasattr(self, "sessions_manager"):
            self.sessions_manager.db = self.db
        self.db_available = True
        self.db_error = None
        self._database_recovery_disabled = False
        self.db_status_label.setText(
            f"Database repaired. Backup: {result.backup_path}\n"
            + ", ".join(f"{table}: {result.copied[table]} copied, {result.failed[table]} failed" for table in result.copied)
        )
        QMessageBox.information(self, "Repair database", f"Database repaired.\nBackup: {result.backup_path}")
        self.refresh_all()

    @staticmethod
    def _active_session_id_from_status(status: CollectorStatus) -> str | None:
        if status.state not in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}:
            return None
        return None if status.current_session_id is None else str(status.current_session_id)

    def _try_restore_database(self) -> None:
        if self._database_recovery_disabled:
            return
        replacement = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg, read_only=True)
        try:
            replacement.init_schema()
            integrity = replacement.check_integrity(quick=True)
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(integrity))
        except sqlite3.DatabaseError as exc:
            replacement.close()
            self.db_error = str(exc)
            self._database_recovery_disabled = self._is_persistent_database_error(exc)
            self._show_database_error()
            return

        self.db.close()
        self.db = replacement
        if hasattr(self, "sessions_manager"):
            self.sessions_manager.db = self.db
        self.db_available = True
        self.db_error = None
        self._database_recovery_disabled = False
        if hasattr(self, "db_status_label"):
            self.db_status_label.setText("")
        self._clear_database_error_message()

    def _restore_database_after_failed_repair(self, original_error: Exception) -> None:
        replacement = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg, read_only=True)
        try:
            replacement.init_schema()
            integrity = replacement.check_integrity(quick=True)
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(integrity))
        except sqlite3.DatabaseError as exc:
            replacement.close()
            self.db_available = False
            self.db_error = f"{original_error}; failed to reopen database after repair failure: {exc}"
            self._database_recovery_disabled = self._is_persistent_database_error(exc)
            return

        self.db = replacement
        if hasattr(self, "sessions_manager"):
            self.sessions_manager.db = self.db
        self.db_available = True
        self.db_error = None
        self._database_recovery_disabled = False

    def _show_repair_failure_state(self, exc: Exception) -> None:
        if not hasattr(self, "db_status_label"):
            return
        if self.db_available:
            self.db_status_label.setText(f"Repair failed; original database reopened: {exc}")
        else:
            self._show_database_error()

    def _ensure_database_exists(self) -> None:
        db = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg)
        try:
            db.init_schema()
        finally:
            db.close()

    def _set_database_unavailable(self, exc: sqlite3.DatabaseError) -> None:
        self.db_available = False
        self.db_error = str(exc)
        self._database_recovery_disabled = self._is_persistent_database_error(exc)
        self.db.close()

    def _show_database_error(self) -> None:
        message = self._database_error_message()
        if hasattr(self, "db_status_label"):
            self.db_status_label.setText(message)
        if hasattr(self, "chart_status_label"):
            suffix = ""
            if self.chart.series or self.chart.events:
                suffix = " Showing the last successfully loaded chart."
            self.chart_status_label.setText(message.replace("\n", " ") + suffix)

    def _clear_database_error_message(self) -> None:
        if hasattr(self, "db_status_label"):
            self.db_status_label.setText("")
        if hasattr(self, "chart_status_label"):
            self.chart_status_label.setText("")

    def _database_error_message(self) -> str:
        reason = self.db_error or "unknown SQLite error"
        return (
            f"Database unavailable: {reason}\n"
            f"Path: {self.cfg.resolved_db_path()}\n"
            "Live status can still be refreshed, but recorded sessions are disabled until the database is repaired or moved aside."
        )

    @staticmethod
    def _is_persistent_database_error(exc: sqlite3.DatabaseError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in PERSISTENT_DATABASE_ERROR_MARKERS)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return


def main() -> int:
    cfg = load_config()
    app = QApplication(sys.argv)
    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
