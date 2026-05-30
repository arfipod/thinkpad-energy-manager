from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig, load_config
from battery_auditor.core.analyzer import export_session_csv
from battery_auditor.core.database import BatteryDatabase, repair_database
from battery_auditor.core.sysfs import read_snapshot
from battery_auditor.core.tlp import TlpClient

BLACKBOX_SERVICE = "battery-auditor-blackbox.service"

try:
    import pyqtgraph as pg
    from PySide6.QtCore import QProcess, QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
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
        "  python -m pip install 'battery-auditor[ui]'\n"
        "or install the relevant PySide6 packages plus pyqtgraph."
    ) from exc


class BatteryChart(QWidget):
    """Interactive chart for recorded battery series."""

    def __init__(self) -> None:
        super().__init__()
        self.series: list[tuple[str, list[tuple[float, float]]]] = []
        self.y_label = ""
        self.setMinimumHeight(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "minutes from start")
        self.plot.addLegend(offset=(12, 12))
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        layout.addWidget(self.plot)

    def set_data(self, title: str, y_label: str, series: list[tuple[str, list[tuple[float, float]]]]) -> None:
        self.y_label = y_label
        self.series = series
        self.plot.clear()
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
        for index, (name, points) in enumerate(series):
            if not points:
                continue
            color = palette[index % len(palette)]
            x_values = [x for x, _y in points]
            y_values = [y for _x, y in points]
            self.plot.plot(
                x_values,
                y_values,
                name=name,
                pen=pg.mkPen(color, width=2),
                symbol="o",
                symbolBrush=color,
                symbolPen=color,
                symbolSize=5,
            )

    def _mouse_moved(self, pos: Any) -> None:
        if not self.series or not self.plot.sceneBoundingRect().contains(pos):
            QToolTip.hideText()
            return
        view_point = self.plot.plotItem.vb.mapSceneToView(pos)
        mouse_x = float(view_point.x())
        nearest = self._nearest_values(mouse_x)
        if not nearest:
            QToolTip.hideText()
            return

        lines = [f"t = {nearest[0][1]:.2f} min"]
        for name, _x, value in nearest:
            lines.append(f"{name}: {value:.2f} {self.y_label}")
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


class MainWindow(QMainWindow):
    def __init__(self, cfg: AuditorConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.db = BatteryDatabase(cfg.resolved_db_path(), cfg)
        self.db_available = True
        self.db_error: str | None = None
        try:
            self.db.init_schema()
            integrity = self.db.check_integrity(quick=True)
            if integrity != ["ok"]:
                raise sqlite3.DatabaseError("; ".join(integrity))
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
        self.collector_process: QProcess | None = None

        self.setWindowTitle("Battery Auditor")
        self.resize(1080, 760)

        tabs = QTabWidget()
        tabs.addTab(self._build_overview_tab(), "Status")
        tabs.addTab(self._build_recorder_tab(), "Recording")
        tabs.addTab(self._build_charts_tab(), "Charts")
        tabs.addTab(self._build_events_tab(), "Events")
        tabs.addTab(self._build_tlp_tab(), "TLP")
        self.setCentralWidget(tabs)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh_all)
        self.menuBar().addAction(refresh_action)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(int(self.cfg.ui_refresh_seconds * 1000))

        self.refresh_all()

    def _build_overview_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        row = QHBoxLayout()
        self.db_label = QLabel(f"DB: {self.cfg.resolved_db_path()}")
        self.db_status_label = QLabel("")
        refresh = QPushButton("Refresh status")
        repair = QPushButton("Repair database")
        refresh.clicked.connect(self.refresh_live_snapshot)
        repair.clicked.connect(self.repair_database_from_ui)
        self.repair_db_button = repair
        row.addWidget(self.db_label)
        row.addStretch(1)
        row.addWidget(repair)
        row.addWidget(refresh)
        self.live_text = QPlainTextEdit()
        self.live_text.setReadOnly(True)
        layout.addLayout(row)
        layout.addWidget(self.db_status_label)
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

        row = QHBoxLayout()
        self.start_record_button = QPushButton("Start collector")
        self.stop_record_button = QPushButton("Stop collector")
        self.stop_record_button.setEnabled(False)
        self.start_record_button.clicked.connect(self.start_collector_process)
        self.stop_record_button.clicked.connect(self.stop_collector_process)
        row.addWidget(self.start_record_button)
        row.addWidget(self.stop_record_button)
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

        self.collector_output = QTextEdit()
        self.collector_output.setReadOnly(True)
        layout.addWidget(self.collector_output)
        layout.addWidget(QLabel("Note: for a clean black-box test, start the service or CLI and close the UI."))
        return widget

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
        ])
        refresh = QPushButton("Update chart")
        export_csv = QPushButton("Export CSV")
        refresh.clicked.connect(self.refresh_sessions_and_chart)
        export_csv.clicked.connect(self.export_selected_session_csv)
        self.session_combo.currentIndexChanged.connect(self.refresh_chart)
        self.metric_combo.currentIndexChanged.connect(self.refresh_chart)
        controls.addWidget(QLabel("Session"))
        controls.addWidget(self.session_combo, 1)
        controls.addWidget(QLabel("Metric"))
        controls.addWidget(self.metric_combo)
        controls.addWidget(refresh)
        controls.addWidget(export_csv)
        self.chart = BatteryChart()
        layout.addLayout(controls)
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

        self.tlp_output = QPlainTextEdit()
        self.tlp_output.setReadOnly(True)
        layout.addWidget(self.tlp_output)
        return widget

    def refresh_all(self, _checked: bool = False, *, prefer_running_session: bool = False) -> None:
        self.refresh_sessions(prefer_running_session=prefer_running_session)
        self.refresh_live_snapshot()
        self.refresh_chart()
        self.refresh_events()

    def refresh_sessions(self, *, prefer_running_session: bool = False) -> None:
        if not self.db_available:
            self._show_database_error()
            if hasattr(self, "session_combo"):
                self.session_combo.clear()
            return
        current = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        running_index: int | None = None
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        try:
            for row in self.db.list_sessions(limit=200):
                status = row["ended_reason"] or "running"
                sample_count = row["real_sample_count"] if "real_sample_count" in row else row["sample_count"]
                last_sample = row["last_sample_iso"] or "no samples"
                label = f"{status} | {row['started_at_iso']} | {row['id']} | {sample_count} samples | last {last_sample}"
                self.session_combo.addItem(label, row["id"])
                if status == "running" and running_index is None:
                    running_index = self.session_combo.count() - 1
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self.session_combo.clear()
            self.session_combo.blockSignals(False)
            self._show_database_error()
            return
        if prefer_running_session and running_index is not None:
            self.session_combo.setCurrentIndex(running_index)
        elif current:
            index = self.session_combo.findData(current)
            if index >= 0:
                self.session_combo.setCurrentIndex(index)
            elif running_index is not None:
                self.session_combo.setCurrentIndex(running_index)
        elif running_index is not None:
            self.session_combo.setCurrentIndex(running_index)
        self.session_combo.blockSignals(False)

    def refresh_live_snapshot(self) -> None:
        snap = read_snapshot(self.cfg.sysfs_power_supply_dir)
        data = snap.to_dict()
        self.live_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

    def refresh_chart(self) -> None:
        if not self.db_available:
            self.chart.set_data("Database unavailable", "", [])
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            self.chart.set_data("No session", "", [])
            return
        metric = self.metric_combo.currentText()
        try:
            rows = self.db.fetch_session_series(str(session_id))
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self.chart.set_data("Database unavailable", metric, [])
            self._show_database_error()
            return
        if not rows:
            self.chart.set_data("No data", metric, [])
            return
        first_time = float(rows[0]["wall_time"])
        grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for row in rows:
            value = self._metric_value(row, metric)
            if value is None:
                continue
            minutes = (float(row["wall_time"]) - first_time) / 60.0
            grouped[str(row["battery_name"])].append((minutes, float(value)))
        self.chart.set_data(f"{metric} — {session_id}", metric, sorted(grouped.items()))

    def refresh_sessions_and_chart(self, _checked: bool = False, *, prefer_running_session: bool = False) -> None:
        self.refresh_sessions(prefer_running_session=prefer_running_session)
        self.refresh_chart()

    def refresh_events(self) -> None:
        if not self.db_available:
            self.events_table.setRowCount(0)
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            self.events_table.setRowCount(0)
            return
        try:
            events = self.db.fetch_events(str(session_id), limit=1000)
        except sqlite3.DatabaseError as exc:
            self._set_database_unavailable(exc)
            self.events_table.setRowCount(0)
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

    def export_selected_session_csv(self) -> None:
        if not self.db_available:
            QMessageBox.warning(self, "Database unavailable", self._database_error_message())
            return
        session_id = self.session_combo.currentData()
        if not session_id:
            QMessageBox.information(self, "Export CSV", "Select a session before exporting.")
            return

        default_path = Path.home() / f"battery-auditor-{session_id}.csv"
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
        value = row[metric]
        return None if value is None else float(value)

    def start_collector_process(self) -> None:
        if not self.db_available:
            QMessageBox.warning(self, "Database unavailable", self._database_error_message())
            return
        if self.collector_process is not None:
            QMessageBox.information(self, "Collector", "A collector is already running from the UI.")
            return
        if self.record_mode.currentText() == "blackbox":
            reply = QMessageBox.question(
                self,
                "Black-box mode",
                "Black-box mode is more reliable if you close the UI and run it as a service/CLI. Start it from the UI anyway?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        process = QProcess(self)
        args = [
            "-m",
            "battery_auditor.cli",
            "--db",
            str(self.cfg.resolved_db_path()),
            "collect",
            "--name",
            self.record_name.text() or "qt-session",
            "--interval",
            str(self.record_interval.value()),
            "--mode",
            self.record_mode.currentText(),
        ]
        process.setProgram(sys.executable)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(lambda: self._append_process_output(process, stdout=True))
        process.readyReadStandardError.connect(lambda: self._append_process_output(process, stdout=False))
        process.finished.connect(self._collector_finished)
        process.start()
        self.collector_process = process
        self.start_record_button.setEnabled(False)
        self.stop_record_button.setEnabled(True)
        self.collector_output.append("Collector started.")

    def stop_collector_process(self) -> None:
        if self.collector_process is None:
            return
        self.collector_process.terminate()
        if not self.collector_process.waitForFinished(3000):
            self.collector_process.kill()

    def _append_process_output(self, process: QProcess, stdout: bool) -> None:
        data = process.readAllStandardOutput() if stdout else process.readAllStandardError()
        text = bytes(data.data()).decode("utf-8", errors="replace")
        if text:
            self.collector_output.append(f"<pre>{self._html_escape(text.rstrip())}</pre>")

    def _collector_finished(self) -> None:
        self.collector_output.append("Collector stopped.")
        self.collector_process = None
        self.start_record_button.setEnabled(True)
        self.stop_record_button.setEnabled(False)
        self.refresh_all()

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
        active = self._active_heartbeat_files()
        if active:
            QMessageBox.warning(
                self,
                "Repair database",
                "A collector appears to be running. Stop the collector before repairing the database.\n\n"
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
            self._set_database_unavailable(exc)
            self._show_database_error()
            QMessageBox.warning(self, "Repair database", f"Repair failed:\n{exc}")
            return
        except OSError as exc:
            QMessageBox.warning(self, "Repair database", f"Repair failed:\n{exc}")
            return

        self.db = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg)
        self.db.init_schema()
        self.db_available = True
        self.db_error = None
        self.db_status_label.setText(
            f"Database repaired. Backup: {result.backup_path}\n"
            + ", ".join(f"{table}: {result.copied[table]} copied, {result.failed[table]} failed" for table in result.copied)
        )
        QMessageBox.information(self, "Repair database", f"Database repaired.\nBackup: {result.backup_path}")
        self.refresh_all()

    def _set_database_unavailable(self, exc: sqlite3.DatabaseError) -> None:
        self.db_available = False
        self.db_error = str(exc)
        self.db.close()

    def _show_database_error(self) -> None:
        if hasattr(self, "db_status_label"):
            self.db_status_label.setText(self._database_error_message())

    def _database_error_message(self) -> str:
        reason = self.db_error or "unknown SQLite error"
        return (
            f"Database unavailable: {reason}\n"
            f"Path: {self.cfg.resolved_db_path()}\n"
            "Live status can still be refreshed, but recorded sessions are disabled until the database is repaired or moved aside."
        )

    def _active_heartbeat_files(self) -> list[Path]:
        heartbeat_dir = self.cfg.heartbeat_dir()
        if not heartbeat_dir.exists():
            return []
        max_age = max(30.0, self.cfg.heartbeat_seconds * 5)
        now = time.time()
        active: list[Path] = []
        for path in heartbeat_dir.glob("*.json"):
            try:
                if now - path.stat().st_mtime <= max_age:
                    active.append(path)
            except OSError:
                continue
        return sorted(active)


def main() -> int:
    cfg = load_config()
    app = QApplication(sys.argv)
    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
