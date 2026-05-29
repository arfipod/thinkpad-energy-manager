from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any

from battery_auditor.config import AuditorConfig, load_config
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.sysfs import read_snapshot
from battery_auditor.core.tlp import TlpClient

try:
    from PySide6.QtCore import QPointF, QProcess, QRectF, Qt, QTimer
    from PySide6.QtGui import QAction, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDoubleSpinBox,
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
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - depends on optional UI extra
    raise SystemExit(
        "PySide6 is required for the Qt UI. Install with:\n"
        "  python -m pip install 'battery-auditor[ui]'\n"
        "or on Debian, install the relevant python3-pyside6 packages."
    ) from exc


class SimpleLineChart(QWidget):
    """Small Qt-only chart widget.

    This avoids matplotlib/pyqtgraph for the live UI and keeps the UI optional.
    The collector should normally run without this widget during black-box tests.
    """

    def __init__(self) -> None:
        super().__init__()
        self.series: list[tuple[str, list[tuple[float, float]]]] = []
        self.title = ""
        self.y_label = ""
        self.setMinimumHeight(320)

    def set_data(self, title: str, y_label: str, series: list[tuple[str, list[tuple[float, float]]]]) -> None:
        self.title = title
        self.y_label = y_label
        self.series = series
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(12, 12, -12, -12)
        painter.drawText(rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, self.title)
        plot = QRectF(rect.adjusted(50, 34, -20, -44))
        painter.drawRect(plot)

        all_points = [point for _name, points in self.series for point in points]
        if not all_points:
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No hay datos para graficar.")
            return

        min_x = min(x for x, _y in all_points)
        max_x = max(x for x, _y in all_points)
        min_y = min(y for _x, y in all_points)
        max_y = max(y for _x, y in all_points)
        if max_x == min_x:
            max_x += 1.0
        if max_y == min_y:
            max_y += 1.0

        # Give the y axis a small margin.
        y_margin = (max_y - min_y) * 0.08
        min_y -= y_margin
        max_y += y_margin

        painter.drawText(QRectF(0, plot.top(), 48, 20), Qt.AlignmentFlag.AlignRight, f"{max_y:.1f}")
        painter.drawText(QRectF(0, plot.bottom() - 20, 48, 20), Qt.AlignmentFlag.AlignRight, f"{min_y:.1f}")
        painter.drawText(QRectF(plot.left(), plot.bottom() + 6, plot.width(), 18), Qt.AlignmentFlag.AlignCenter, "minutos desde inicio")
        painter.drawText(QRectF(4, plot.center().y() - 10, 44, 20), Qt.AlignmentFlag.AlignCenter, self.y_label)

        palette = self.palette()
        base_color = palette.color(self.foregroundRole())
        pens = [
            QPen(base_color, 2),
            QPen(base_color.darker(140), 2, Qt.PenStyle.DashLine),
            QPen(base_color.lighter(150), 2, Qt.PenStyle.DotLine),
            QPen(base_color.darker(180), 2, Qt.PenStyle.DashDotLine),
        ]

        legend_y = plot.bottom() + 26
        for index, (name, points) in enumerate(self.series):
            if len(points) < 2:
                continue
            painter.setPen(pens[index % len(pens)])
            mapped = [self._map_point(x, y, min_x, max_x, min_y, max_y, plot) for x, y in points]
            for a, b in zip(mapped, mapped[1:], strict=False):
                painter.drawLine(a, b)
            painter.drawText(QPointF(plot.left() + index * 130, legend_y), name)

    @staticmethod
    def _map_point(x: float, y: float, min_x: float, max_x: float, min_y: float, max_y: float, plot: QRectF) -> QPointF:
        px = plot.left() + ((x - min_x) / (max_x - min_x)) * plot.width()
        py = plot.bottom() - ((y - min_y) / (max_y - min_y)) * plot.height()
        return QPointF(px, py)


class MainWindow(QMainWindow):
    def __init__(self, cfg: AuditorConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.db = BatteryDatabase(cfg.resolved_db_path(), cfg)
        self.db.init_schema()
        self.collector_process: QProcess | None = None

        self.setWindowTitle("Battery Auditor")
        self.resize(1080, 760)

        tabs = QTabWidget()
        tabs.addTab(self._build_overview_tab(), "Estado")
        tabs.addTab(self._build_recorder_tab(), "Grabación")
        tabs.addTab(self._build_charts_tab(), "Gráficas")
        tabs.addTab(self._build_events_tab(), "Eventos")
        tabs.addTab(self._build_tlp_tab(), "TLP")
        self.setCentralWidget(tabs)

        refresh_action = QAction("Refrescar", self)
        refresh_action.triggered.connect(self.refresh_all)
        self.menuBar().addAction(refresh_action)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_live_snapshot)
        self.timer.start(int(self.cfg.ui_refresh_seconds * 1000))

        self.refresh_all()

    def _build_overview_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        row = QHBoxLayout()
        self.db_label = QLabel(f"DB: {self.cfg.resolved_db_path()}")
        refresh = QPushButton("Refrescar estado")
        refresh.clicked.connect(self.refresh_live_snapshot)
        row.addWidget(self.db_label)
        row.addStretch(1)
        row.addWidget(refresh)
        self.live_text = QPlainTextEdit()
        self.live_text.setReadOnly(True)
        layout.addLayout(row)
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
        form.addRow("Nombre", self.record_name)
        form.addRow("Intervalo", self.record_interval)
        form.addRow("Modo", self.record_mode)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.start_record_button = QPushButton("Iniciar collector")
        self.stop_record_button = QPushButton("Detener collector")
        self.stop_record_button.setEnabled(False)
        self.start_record_button.clicked.connect(self.start_collector_process)
        self.stop_record_button.clicked.connect(self.stop_collector_process)
        row.addWidget(self.start_record_button)
        row.addWidget(self.stop_record_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.collector_output = QPlainTextEdit()
        self.collector_output.setReadOnly(True)
        layout.addWidget(self.collector_output)
        layout.addWidget(QLabel("Nota: para una prueba black-box limpia, arranca el servicio o CLI y cierra la UI."))
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
        refresh = QPushButton("Actualizar gráfica")
        refresh.clicked.connect(self.refresh_chart)
        self.session_combo.currentIndexChanged.connect(self.refresh_chart)
        self.metric_combo.currentIndexChanged.connect(self.refresh_chart)
        controls.addWidget(QLabel("Sesión"))
        controls.addWidget(self.session_combo, 1)
        controls.addWidget(QLabel("Métrica"))
        controls.addWidget(self.metric_combo)
        controls.addWidget(refresh)
        self.chart = SimpleLineChart()
        layout.addLayout(controls)
        layout.addWidget(self.chart)
        return widget

    def _build_events_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        refresh = QPushButton("Actualizar eventos")
        refresh.clicked.connect(self.refresh_events)
        self.events_table = QTableWidget(0, 5)
        self.events_table.setHorizontalHeaderLabels(["Hora", "Severidad", "Tipo", "Batería", "Mensaje"])
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
        apply = QPushButton("Aplicar setcharge temporal")
        recalibrate = QPushButton("Recalibrar batería")
        apply.clicked.connect(self.apply_tlp_setcharge)
        recalibrate.clicked.connect(self.recalibrate_battery)
        form.addWidget(QLabel("Batería"))
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

    def refresh_all(self) -> None:
        self.refresh_sessions()
        self.refresh_live_snapshot()
        self.refresh_chart()
        self.refresh_events()

    def refresh_sessions(self) -> None:
        current = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for row in self.db.list_sessions(limit=200):
            label = f"{row['started_at_iso']} | {row['id']} | {row['sample_count']} muestras"
            self.session_combo.addItem(label, row["id"])
        if current:
            index = self.session_combo.findData(current)
            if index >= 0:
                self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)

    def refresh_live_snapshot(self) -> None:
        snap = read_snapshot(self.cfg.sysfs_power_supply_dir)
        data = snap.to_dict()
        self.live_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))

    def refresh_chart(self) -> None:
        session_id = self.session_combo.currentData()
        if not session_id:
            self.chart.set_data("Sin sesión", "", [])
            return
        metric = self.metric_combo.currentText()
        rows = self.db.fetch_session_series(str(session_id))
        if not rows:
            self.chart.set_data("Sin datos", metric, [])
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

    def refresh_events(self) -> None:
        session_id = self.session_combo.currentData()
        if not session_id:
            self.events_table.setRowCount(0)
            return
        events = self.db.fetch_events(str(session_id), limit=1000)
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
        if self.collector_process is not None:
            QMessageBox.information(self, "Collector", "Ya hay un collector iniciado desde la UI.")
            return
        if self.record_mode.currentText() == "blackbox":
            reply = QMessageBox.question(
                self,
                "Modo blackbox",
                "El modo blackbox es más fiable si cierras la UI y lo ejecutas como servicio/CLI. ¿Iniciarlo igualmente desde la UI?",
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
        self.collector_output.appendPlainText("Collector iniciado.\n")

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
            self.collector_output.appendPlainText(text.rstrip())

    def _collector_finished(self) -> None:
        self.collector_output.appendPlainText("Collector detenido.\n")
        self.collector_process = None
        self.start_record_button.setEnabled(True)
        self.stop_record_button.setEnabled(False)
        self.refresh_all()

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
            "Recalibrar batería",
            f"Esto ejecutará 'sudo tlp recalibrate {battery}'. Puede tardar y descargar la batería seleccionada. ¿Continuar?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = TlpClient(use_sudo=True).recalibrate(battery)
        self.tlp_output.setPlainText(result.combined_output())


def main() -> int:
    cfg = load_config()
    app = QApplication(sys.argv)
    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
