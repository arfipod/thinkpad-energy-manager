from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.analyzer import (
    export_session_csv,
    export_session_json,
    summarize_session,
    summary_to_text,
)
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.runtime import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    CollectorStatus,
    collect_runtime_status,
)

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QFileDialog,
        QHBoxLayout,
        QInputDialog,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - depends on optional UI extra
    raise SystemExit(
        "PySide6 is required for the Qt UI. Install with:\n"
        "  python -m pip install 'thinkpad-energy-manager[ui]'"
    ) from exc


class SessionManager(QWidget):
    def __init__(
        self,
        cfg: AuditorConfig,
        db: BatteryDatabase,
        *,
        open_in_chart: Callable[[str], None],
        refresh_main: Callable[[], None],
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.open_in_chart = open_in_chart
        self.refresh_main = refresh_main

        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.open_button = QPushButton("Open in chart")
        self.analyze_button = QPushButton("Analyze")
        self.export_csv_button = QPushButton("Export CSV")
        self.export_json_button = QPushButton("Export JSON")
        self.rename_button = QPushButton("Rename")
        self.notes_button = QPushButton("Edit notes")
        self.delete_button = QPushButton("Delete")
        self.merge_button = QPushButton("Merge")
        self.recover_button = QPushButton("Recover open")

        for button in (
            self.refresh_button,
            self.open_button,
            self.analyze_button,
            self.export_csv_button,
            self.export_json_button,
            self.rename_button,
            self.notes_button,
            self.delete_button,
            self.merge_button,
            self.recover_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            [
                "",
                "ID",
                "Name",
                "Status",
                "Started",
                "Ended",
                "Samples",
                "Last sample",
                "Last heartbeat",
                "Power loss",
                "Notes",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        layout.addLayout(actions)
        layout.addWidget(self.table)

        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(self.open_selected_in_chart)
        self.analyze_button.clicked.connect(self.analyze_selected)
        self.export_csv_button.clicked.connect(lambda: self.export_selected("csv"))
        self.export_json_button.clicked.connect(lambda: self.export_selected("json"))
        self.rename_button.clicked.connect(self.rename_selected)
        self.notes_button.clicked.connect(self.edit_notes_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self.merge_button.clicked.connect(self.merge_selected)
        self.recover_button.clicked.connect(self.recover_open_sessions)

    def refresh(self) -> None:
        try:
            status = collect_runtime_status(self.cfg, self.db)
            rows = self.db.list_sessions(limit=1000)
        except sqlite3.DatabaseError as exc:
            QMessageBox.warning(self, "Sessions", f"Database unavailable:\n{exc}")
            return

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            session_id = str(row["id"])
            check = QTableWidgetItem("")
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, session_id)
            self.table.setItem(row_index, 0, check)

            values = [
                session_id,
                str(row["name"] or ""),
                self._row_status(row, status),
                str(row["started_at_iso"] or ""),
                str(row["ended_at_iso"] or ""),
                str(row["real_sample_count"]),
                str(row["last_sample_iso"] or ""),
                str(row["last_heartbeat_iso"] or ""),
                "yes" if row["probable_power_loss"] else "no",
                str(row["notes"] or ""),
            ]
            for col, value in enumerate(values, start=1):
                self.table.setItem(row_index, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def open_selected_in_chart(self) -> None:
        selected = self.selected_session_ids()
        if not selected:
            QMessageBox.information(self, "Open in chart", "Select a session first.")
            return
        self.open_in_chart(selected[0])

    def analyze_selected(self) -> None:
        selected = self.selected_session_ids()
        if len(selected) != 1:
            QMessageBox.information(self, "Analyze", "Select exactly one session.")
            return
        try:
            text = summary_to_text(summarize_session(self.db, selected[0]))
        except (ValueError, sqlite3.DatabaseError) as exc:
            QMessageBox.warning(self, "Analyze", str(exc))
            return
        QMessageBox.information(self, "Analyze", text)

    def export_selected(self, fmt: str) -> None:
        selected = self.selected_session_ids()
        if not selected:
            QMessageBox.information(self, "Export", "Select one or more sessions.")
            return
        suffix = ".csv" if fmt == "csv" else ".json"
        exporter = export_session_csv if fmt == "csv" else export_session_json
        if len(selected) == 1:
            default_path = Path.home() / f"thinkpad-energy-manager-{_safe_filename(selected[0])}{suffix}"
            filename, _selected_filter = QFileDialog.getSaveFileName(
                self,
                f"Export {fmt.upper()}",
                str(default_path),
                f"{fmt.upper()} files (*{suffix});;All files (*)",
            )
            if not filename:
                return
            output = Path(filename).expanduser()
            if output.suffix.lower() != suffix:
                output = output.with_suffix(suffix)
            try:
                exporter(self.db, selected[0], output)
            except (OSError, sqlite3.DatabaseError) as exc:
                QMessageBox.warning(self, "Export", f"Export failed:\n{exc}")
                return
            QMessageBox.information(self, "Export", f"Exported session to:\n{output}")
            return

        directory = QFileDialog.getExistingDirectory(self, f"Export {fmt.upper()} files", str(Path.home()))
        if not directory:
            return
        output_dir = Path(directory).expanduser()
        try:
            for session_id in selected:
                exporter(self.db, session_id, output_dir / f"thinkpad-energy-manager-{_safe_filename(session_id)}{suffix}")
        except (OSError, sqlite3.DatabaseError) as exc:
            QMessageBox.warning(self, "Export", f"Export failed:\n{exc}")
            return
        QMessageBox.information(self, "Export", f"Exported {len(selected)} session(s) to:\n{output_dir}")

    def rename_selected(self) -> None:
        selected = self._exactly_one("Rename")
        if selected is None:
            return
        if self._collector_may_be_writing():
            QMessageBox.warning(self, "Rename", "Stop the active collector before renaming sessions.")
            return
        session = self.db.get_session(selected)
        current = "" if session is None else str(session["name"] or "")
        name, ok = QInputDialog.getText(self, "Rename session", "Name", text=current)
        if not ok:
            return
        write_db = self._open_write_db()
        try:
            if not write_db.rename_session(selected, name):
                QMessageBox.warning(self, "Rename", f"Unknown session: {selected}")
                return
        except sqlite3.DatabaseError as exc:
            QMessageBox.warning(self, "Rename", str(exc))
            return
        finally:
            write_db.close()
        self.refresh()
        self.refresh_main()

    def edit_notes_selected(self) -> None:
        selected = self._exactly_one("Edit notes")
        if selected is None:
            return
        if self._collector_may_be_writing():
            QMessageBox.warning(self, "Edit notes", "Stop the active collector before editing notes.")
            return
        session = self.db.get_session(selected)
        current = "" if session is None else str(session["notes"] or "")
        notes, ok = QInputDialog.getMultiLineText(self, "Edit notes", "Notes", current)
        if not ok:
            return
        write_db = self._open_write_db()
        try:
            if not write_db.update_session_notes(selected, notes):
                QMessageBox.warning(self, "Edit notes", f"Unknown session: {selected}")
                return
        except sqlite3.DatabaseError as exc:
            QMessageBox.warning(self, "Edit notes", str(exc))
            return
        finally:
            write_db.close()
        self.refresh()
        self.refresh_main()

    def delete_selected(self) -> None:
        selected = self.selected_session_ids()
        if not selected:
            QMessageBox.information(self, "Delete", "Select one or more sessions.")
            return
        if self._collector_may_be_writing():
            QMessageBox.warning(self, "Delete", "Stop the active collector before deleting sessions.")
            return
        reply = QMessageBox.question(
            self,
            "Delete sessions",
            f"Delete {len(selected)} session(s) and all dependent rows? This cannot be undone.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        write_db = self._open_write_db()
        try:
            for session_id in selected:
                write_db.delete_session(session_id)
        except sqlite3.DatabaseError as exc:
            QMessageBox.warning(self, "Delete", str(exc))
            return
        finally:
            write_db.close()
        self.refresh()
        self.refresh_main()

    def merge_selected(self) -> None:
        selected = self.selected_session_ids()
        if len(selected) < 2:
            QMessageBox.information(self, "Merge", "Select at least two sessions.")
            return
        if self._collector_may_be_writing():
            QMessageBox.warning(self, "Merge", "Stop the active collector before merging sessions.")
            return
        open_selected = [session_id for session_id in selected if self._is_open_session(session_id)]
        if open_selected:
            QMessageBox.warning(
                self,
                "Merge",
                "Recover or stop open sessions before merging:\n" + "\n".join(open_selected),
            )
            return
        name, ok = QInputDialog.getText(self, "Merge sessions", "New merged session name", text="merged-session")
        if not ok:
            return
        reply = QMessageBox.question(
            self,
            "Merge sessions",
            "Create a new synthetic session from the selected sessions? Source sessions will remain untouched.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        merged_id = f"merged-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        write_db = self._open_write_db()
        try:
            write_db.merge_sessions(selected, merged_id, name)
        except (ValueError, sqlite3.DatabaseError) as exc:
            QMessageBox.warning(self, "Merge", str(exc))
            return
        finally:
            write_db.close()
        self.refresh()
        self.refresh_main()
        self.open_in_chart(merged_id)

    def recover_open_sessions(self) -> None:
        if self._collector_may_be_writing():
            QMessageBox.warning(self, "Recover", "Stop the active collector before recovering open sessions.")
            return
        reply = QMessageBox.question(
            self,
            "Recover open sessions",
            "Mark open sessions as interrupted/probable power loss?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        write_db = self._open_write_db()
        try:
            recovered = write_db.recover_open_sessions(reason="ui_recover")
        except sqlite3.DatabaseError as exc:
            QMessageBox.warning(self, "Recover", str(exc))
            return
        finally:
            write_db.close()
        QMessageBox.information(self, "Recover", f"Recovered {len(recovered)} session(s).")
        self.refresh()
        self.refresh_main()

    def selected_session_ids(self) -> list[str]:
        selected: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return selected

    def _exactly_one(self, title: str) -> str | None:
        selected = self.selected_session_ids()
        if len(selected) != 1:
            QMessageBox.information(self, title, "Select exactly one session.")
            return None
        return selected[0]

    def _row_status(self, row: Any, runtime_status: CollectorStatus) -> str:
        if runtime_status.current_session_id == row["id"] and runtime_status.state in {STATUS_RUNNING, STATUS_PAUSED}:
            return runtime_status.state.lower()
        if row["ended_at_wall"] is None:
            return "stale-open" if runtime_status.current_session_id != row["id"] else runtime_status.state.lower()
        return str(row["ended_reason"] or "ended")

    def _collector_may_be_writing(self) -> bool:
        status = collect_runtime_status(self.cfg, self.db)
        return status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}

    def _refuse_active_session_mutation(self, session_id: str, verb: str) -> bool:
        status = collect_runtime_status(self.cfg, self.db)
        if status.current_session_id == session_id and status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}:
            QMessageBox.warning(self, "Session active", f"Stop the collector before trying to {verb} its active session.")
            return True
        return False

    def _open_write_db(self) -> BatteryDatabase:
        db = BatteryDatabase(self.cfg.resolved_db_path(), self.cfg)
        db.init_schema()
        return db

    def _is_open_session(self, session_id: str) -> bool:
        session = self.db.get_session(session_id)
        return bool(session is not None and session["ended_at_wall"] is None)


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
