from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig, load_config
from battery_auditor.core.analyzer import (
    export_session_csv,
    export_session_json,
    summarize_session,
    summary_to_text,
)
from battery_auditor.core.collector import BatteryCollector
from battery_auditor.core.database import BatteryDatabase, repair_database
from battery_auditor.core.runtime import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    collect_runtime_status,
    stop_collector,
    write_control_state,
)
from battery_auditor.core.sysfs import read_snapshot
from battery_auditor.core.tlp import TlpClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battery-auditor",
        description="Low-impact Linux battery recorder and diagnostics toolkit.",
    )
    parser.add_argument("--config", type=Path, action="append", help="Extra TOML config path.")
    parser.add_argument("--db", type=Path, help="SQLite database path.")
    parser.add_argument("--sysfs", type=Path, help="Power supply sysfs root. Defaults to /sys/class/power_supply.")

    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="Read one sysfs snapshot and print it.")
    once.add_argument("--json", action="store_true", help="Print JSON instead of text.")

    collect = sub.add_parser("collect", help="Start a recording session.")
    collect.add_argument("--name", help="Human-readable session name.")
    collect.add_argument("--interval", type=float, help="Sampling interval in seconds.")
    collect.add_argument("--duration", type=float, help="Stop after N seconds.")
    collect.add_argument(
        "--mode",
        choices=["passive", "diagnostic", "blackbox"],
        default="diagnostic",
        help="Recording profile. blackbox flushes harder for power-loss forensics.",
    )
    collect.add_argument("--no-recover", action="store_true", help="Do not mark previous open sessions as interrupted.")

    status = sub.add_parser("status", help="Show collector runtime status.")
    status.add_argument("--json", action="store_true", help="Print a stable JSON status payload.")

    stop = sub.add_parser("stop", help="Stop the active collector.")
    stop.add_argument("--force", action="store_true", help="Send SIGKILL instead of SIGTERM. Dangerous.")
    stop.add_argument("--timeout", type=float, default=5.0, help="Seconds to wait for the collector to exit.")

    sub.add_parser("pause", help="Pause the active collector without ending the session.")
    sub.add_parser("resume", help="Resume a paused collector.")

    sessions = sub.add_parser("sessions", help="List recording sessions.")
    sessions.add_argument("--limit", type=int, default=50)

    delete_session = sub.add_parser("delete-session", help="Delete one session and its dependent rows.")
    delete_session.add_argument("session_id")

    rename_session = sub.add_parser("rename-session", help="Rename a session.")
    rename_session.add_argument("session_id")
    rename_session.add_argument("--name", required=True)

    note_session = sub.add_parser("note-session", help="Replace a session's notes.")
    note_session.add_argument("session_id")
    note_session.add_argument("--notes", required=True)

    merge_sessions = sub.add_parser("merge-sessions", help="Merge source sessions into a new synthetic session.")
    merge_sessions.add_argument("session_ids", nargs="+")
    merge_sessions.add_argument("--name", required=True)
    merge_sessions.add_argument("--id", dest="merged_session_id", help="Optional merged session id.")

    analyze = sub.add_parser("analyze", help="Analyze a session.")
    analyze.add_argument("session_id", nargs="?", help="Defaults to latest session.")
    analyze.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="Export session samples.")
    export.add_argument("session_id", nargs="?", help="Defaults to latest session.")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--out", type=Path, required=True)

    recover = sub.add_parser("recover", help="Mark open sessions as interrupted/probable power loss.")
    recover.add_argument("--reason", default="manual_recover")

    repair = sub.add_parser("repair-db", help="Rebuild a readable SQLite database from a damaged one.")
    repair.add_argument("--out", type=Path, help="Write the repaired database to this path.")
    repair.add_argument("--replace", action="store_true", help="Back up and replace the configured database.")

    tlp_b = sub.add_parser("tlp-stat", help="Run tlp-stat on demand.")
    tlp_b.add_argument("section", choices=["battery", "config", "system"], default="battery", nargs="?")
    tlp_b.add_argument("--no-sudo", action="store_true")

    setcharge = sub.add_parser("tlp-setcharge", help="Set temporary TLP charge thresholds.")
    setcharge.add_argument("battery", help="BAT0, BAT1, ...")
    setcharge.add_argument("start", type=int)
    setcharge.add_argument("stop", type=int)
    setcharge.add_argument("--no-sudo", action="store_true")

    recal = sub.add_parser("tlp-recalibrate", help="Run TLP recalibration for one battery.")
    recal.add_argument("battery", help="BAT0, BAT1, ...")
    recal.add_argument("--no-sudo", action="store_true")

    return parser


def load_runtime_config(args: argparse.Namespace) -> AuditorConfig:
    paths = None
    if args.config:
        paths = args.config
    cfg = load_config(paths=paths)
    if args.db:
        cfg.db_path = args.db
    if args.sysfs:
        cfg.sysfs_power_supply_dir = args.sysfs
    return cfg


def read_db_from_cfg(cfg: AuditorConfig) -> BatteryDatabase:
    if not cfg.resolved_db_path().exists():
        write_db = write_db_from_cfg(cfg)
        write_db.close()
    db = BatteryDatabase(cfg.resolved_db_path(), cfg, read_only=True)
    db.init_schema()
    return db


def status_db_from_cfg(cfg: AuditorConfig) -> BatteryDatabase | None:
    if not cfg.resolved_db_path().exists():
        return None
    db = BatteryDatabase(cfg.resolved_db_path(), cfg, read_only=True)
    try:
        db.init_schema()
    except Exception:  # noqa: BLE001 - runtime status should survive a damaged DB
        db.close()
        return None
    return db


def write_db_from_cfg(cfg: AuditorConfig) -> BatteryDatabase:
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    return db


def command_once(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    snap = read_snapshot(cfg.sysfs_power_supply_dir)
    if args.json:
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"Time: {snap.wall_iso}")
    print(f"AC online: {snap.ac_online}")
    total = snap.total_computed_percent
    if total is not None:
        print(f"Total: {total:.1f}% ({_uwh_to_wh(snap.total_energy_now_uwh):.2f} Wh / {_uwh_to_wh(snap.total_energy_full_uwh):.2f} Wh)")
    for b in snap.batteries:
        computed = f"{b.computed_percent:.1f}%" if b.computed_percent is not None else "n/a"
        health = f"{b.health_percent:.1f}%" if b.health_percent is not None else "n/a"
        print(
            f"{b.name}: status={b.status or 'n/a'} reported={b.capacity_percent} computed={computed} "
            f"health={health} energy={_uwh_to_wh(b.energy_now_uwh):.2f}/{_uwh_to_wh(b.energy_full_uwh):.2f}Wh "
            f"power={_uw_to_w(b.power_now_uw):.2f}W voltage={_uv_to_v(b.voltage_now_uv):.3f}V"
        )
    return 0


def command_collect(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    mode_interval = {"passive": 10.0, "diagnostic": cfg.interval_seconds, "blackbox": 1.0}
    interval = args.interval if args.interval is not None else mode_interval[args.mode]
    if args.mode == "blackbox":
        cfg.sqlite_synchronous = "FULL"
        cfg.sqlite_journal_mode = "TRUNCATE"
        cfg.blackbox_flush_each_sample = True
    collector = BatteryCollector(cfg)
    result = collector.run(
        name=args.name,
        interval_seconds=interval,
        duration_seconds=args.duration,
        blackbox=args.mode == "blackbox",
        recover_open_sessions=not args.no_recover,
    )
    print(f"Session {result.session_id} ended: reason={result.reason}, samples={result.samples}")
    return 0


def command_status(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    db = status_db_from_cfg(cfg)
    status = collect_runtime_status(cfg, db)
    if args.json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(_format_status_text(status.to_dict()))
    return 0


def command_stop(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    result = stop_collector(cfg, force=args.force, timeout_seconds=args.timeout)
    print(result.message)
    if result.ok:
        return 0
    return 2 if result.unsafe else 1


def command_pause(_args: argparse.Namespace, cfg: AuditorConfig) -> int:
    db = status_db_from_cfg(cfg)
    status = collect_runtime_status(cfg, db)
    if status.state == STATUS_PAUSED:
        print("Collector is already paused.")
        return 0
    if not (status.state == STATUS_RUNNING and status.lock.held and status.pid_alive and status.pid_is_collector):
        print(f"No running collector can be paused (state={status.state}).", file=sys.stderr)
        return 1
    write_control_state(cfg, paused=True)
    print("Pause requested.")
    return 0


def command_resume(_args: argparse.Namespace, cfg: AuditorConfig) -> int:
    status = collect_runtime_status(cfg, status_db_from_cfg(cfg))
    if status.state != STATUS_PAUSED and not status.control.paused:
        write_control_state(cfg, paused=False)
        print("Collector is not paused. Pause state is clear.")
        return 0
    write_control_state(cfg, paused=False)
    print("Resume requested.")
    return 0


def command_sessions(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    db = read_db_from_cfg(cfg)
    rows = db.list_sessions(limit=args.limit)
    if not rows:
        print("No sessions.")
        return 0
    for row in rows:
        status = row["ended_reason"] or "running"
        loss = " probable-power-loss" if row["probable_power_loss"] else ""
        print(
            f"{row['id']} | {row['started_at_iso']} → {row['ended_at_iso'] or 'open'} | "
            f"samples={row['sample_count']} | {status}{loss} | {row['name'] or ''}"
        )
    return 0


def command_delete_session(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to delete sessions while an active collector may be writing.", file=sys.stderr)
        return 2
    db = write_db_from_cfg(cfg)
    if db.delete_session(args.session_id):
        print(f"Deleted session {args.session_id}.")
        return 0
    print(f"Unknown session: {args.session_id}", file=sys.stderr)
    return 2


def command_rename_session(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to rename sessions while an active collector may be writing.", file=sys.stderr)
        return 2
    db = write_db_from_cfg(cfg)
    if db.rename_session(args.session_id, args.name):
        print(f"Renamed session {args.session_id}.")
        return 0
    print(f"Unknown session: {args.session_id}", file=sys.stderr)
    return 2


def command_note_session(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to edit notes while an active collector may be writing.", file=sys.stderr)
        return 2
    db = write_db_from_cfg(cfg)
    if db.update_session_notes(args.session_id, args.notes):
        print(f"Updated notes for session {args.session_id}.")
        return 0
    print(f"Unknown session: {args.session_id}", file=sys.stderr)
    return 2


def command_merge_sessions(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to merge sessions while an active collector may be writing.", file=sys.stderr)
        return 2
    db = write_db_from_cfg(cfg)
    open_ids = {str(row["id"]) for row in db.list_open_sessions()}
    selected_open = [session_id for session_id in args.session_ids if session_id in open_ids]
    if selected_open:
        print(
            "Refusing to merge open session(s). Run recover first if the collector is no longer alive: "
            + ", ".join(selected_open),
            file=sys.stderr,
        )
        return 2
    merged_id = args.merged_session_id or _new_merged_session_id()
    try:
        db.merge_sessions(list(args.session_ids), merged_id, args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Merged {len(args.session_ids)} session(s) into {merged_id}.")
    return 0


def command_analyze(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    db = read_db_from_cfg(cfg)
    session_id = args.session_id or db.latest_session_id()
    if session_id is None:
        print("No sessions found.", file=sys.stderr)
        return 2
    summary = summarize_session(db, session_id)
    if args.json:
        print(json.dumps(summary, default=lambda o: getattr(o, "__dict__", str(o)), ensure_ascii=False, indent=2))
    else:
        print(summary_to_text(summary))
    return 0


def command_export(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    db = read_db_from_cfg(cfg)
    session_id = args.session_id or db.latest_session_id()
    if session_id is None:
        print("No sessions found.", file=sys.stderr)
        return 2
    if args.format == "csv":
        export_session_csv(db, session_id, args.out)
    else:
        export_session_json(db, session_id, args.out)
    print(f"Exported {session_id} to {args.out}")
    return 0


def command_recover(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to recover open sessions while an active collector may be writing.", file=sys.stderr)
        return 2
    db = write_db_from_cfg(cfg)
    recovered = db.recover_open_sessions(reason=args.reason)
    if recovered:
        print("Recovered sessions:")
        for session_id in recovered:
            print(f"  {session_id}")
    else:
        print("No open sessions to recover.")
    return 0


def command_repair_db(args: argparse.Namespace, cfg: AuditorConfig) -> int:
    if _active_collector_may_be_writing(cfg):
        print("Refusing to repair the database while an active collector may be writing.", file=sys.stderr)
        return 2
    result = repair_database(cfg.resolved_db_path(), output_path=args.out, replace=args.replace)
    print(f"Source: {result.source_path}")
    print(f"Repaired: {result.repaired_path}")
    if result.backup_path is not None:
        print(f"Backup: {result.backup_path}")
    print(f"Integrity: {result.integrity}")
    print("Rows:")
    for table in result.copied:
        print(f"  {table}: copied={result.copied[table]} failed={result.failed[table]}")
    if not result.replaced:
        print("Original database was not replaced. Re-run with --replace after stopping collectors to swap it in.")
    return 0


def command_tlp_stat(args: argparse.Namespace) -> int:
    client = TlpClient(use_sudo=not args.no_sudo)
    if args.section == "battery":
        result = client.stat_battery()
    elif args.section == "config":
        result = client.stat_config()
    else:
        result = client.stat_system()
    print(result.combined_output())
    return result.returncode


def command_tlp_setcharge(args: argparse.Namespace) -> int:
    client = TlpClient(use_sudo=not args.no_sudo)
    result = client.setcharge(args.start, args.stop, args.battery)
    print(result.combined_output())
    return result.returncode


def command_tlp_recalibrate(args: argparse.Namespace) -> int:
    client = TlpClient(use_sudo=not args.no_sudo)
    result = client.recalibrate(args.battery)
    print(result.combined_output())
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_runtime_config(args)
    commands: dict[str, Any] = {
        "once": command_once,
        "collect": command_collect,
        "status": command_status,
        "stop": command_stop,
        "pause": command_pause,
        "resume": command_resume,
        "sessions": command_sessions,
        "delete-session": command_delete_session,
        "rename-session": command_rename_session,
        "note-session": command_note_session,
        "merge-sessions": command_merge_sessions,
        "analyze": command_analyze,
        "export": command_export,
        "recover": command_recover,
        "repair-db": command_repair_db,
    }
    if args.command in commands:
        return int(commands[args.command](args, cfg))
    if args.command == "tlp-stat":
        return command_tlp_stat(args)
    if args.command == "tlp-setcharge":
        return command_tlp_setcharge(args)
    if args.command == "tlp-recalibrate":
        return command_tlp_recalibrate(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


def _uwh_to_wh(value: int | None) -> float:
    return 0.0 if value is None else value / 1_000_000.0


def _uw_to_w(value: int | None) -> float:
    return 0.0 if value is None else value / 1_000_000.0


def _uv_to_v(value: int | None) -> float:
    return 0.0 if value is None else value / 1_000_000.0


def _format_status_text(payload: dict[str, Any]) -> str:
    session = payload.get("current_session_id") or "none"
    name = payload.get("current_session_name")
    if name:
        session = f"{session} ({name})"
    age = payload.get("last_heartbeat_age_seconds")
    heartbeat = "none" if age is None else f"{float(age):.1f}s ago"
    lines = [
        f"Collector: {payload['state']}",
        f"PID: {payload.get('pid') or 'none'} alive={payload.get('pid_alive')} verified={payload.get('pid_is_collector')}",
        f"Session: {session}",
        f"Heartbeat: {heartbeat}",
        f"Samples: {payload.get('sample_count') if payload.get('sample_count') is not None else 'unknown'}",
        f"Open DB sessions: {payload.get('open_session_count', 0)}",
    ]
    warnings = payload.get("warnings") or []
    lines.extend(f"Warning: {warning}" for warning in warnings)
    return "\n".join(lines)


def _active_collector_may_be_writing(cfg: AuditorConfig) -> bool:
    status = collect_runtime_status(cfg, status_db_from_cfg(cfg))
    return status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN}


def _session_is_active_collector_session(cfg: AuditorConfig, db: BatteryDatabase, session_id: str) -> bool:
    session = db.get_session(session_id)
    if session is None or session["ended_at_wall"] is not None:
        return False
    status = collect_runtime_status(cfg, db)
    return status.state in {STATUS_RUNNING, STATUS_PAUSED, STATUS_UNKNOWN} and status.current_session_id == session_id


def _new_merged_session_id() -> str:
    return f"merged-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


if __name__ == "__main__":
    raise SystemExit(main())
