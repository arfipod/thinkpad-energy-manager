from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from battery_auditor.cli import main as cli_main
from battery_auditor.config import AuditorConfig
from battery_auditor.core.database import BatteryDatabase
from battery_auditor.core.runtime import (
    collect_runtime_status,
    control_path,
    lock_path,
    read_control_state,
    write_control_state,
    write_heartbeat,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sysfs_sample"


def test_status_with_no_collector(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)

    rc = cli_main(["--config", str(config_path), "status", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "STOPPED"
    assert payload["collector_state"] == "stopped"
    assert payload["pid"] is None
    assert payload["open_session_count"] == 0


def test_status_with_fake_heartbeat_and_open_session(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)
    cfg = _cfg(tmp_path)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    db.start_session("s1", "fake-open", cfg.to_json())
    write_heartbeat(
        cfg,
        session_id="s1",
        pid=os.getpid(),
        paused=True,
        sample_count=7,
        last_seq=6,
        wall_time=time.time(),
        monotonic_time=time.monotonic(),
    )

    rc = cli_main(["--config", str(config_path), "status", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "UNKNOWN"
    assert payload["active_session_id"] == "s1"
    assert payload["active_session_name"] == "fake-open"
    assert payload["active_heartbeat_count"] == 1
    assert payload["open_session_count"] == 1
    assert payload["last_seq"] == 6
    assert payload["paused"] is True


def test_stop_refuses_unrelated_locked_pid(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                f"path = pathlib.Path({str(lock_path(cfg))!r}); "
                "path.parent.mkdir(parents=True, exist_ok=True); "
                "fh = path.open('w', encoding='utf-8'); "
                f"fh.write(str({sleeper.pid})); fh.flush(); "
                "fcntl.flock(fh.fileno(), fcntl.LOCK_EX); "
                "print('ready', flush=True); "
                "time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"

        rc = cli_main(["--config", str(config_path), "stop", "--timeout", "0.1"])

        output = capsys.readouterr()
        assert rc == 2
        assert "Refusing to signal" in output.out
        assert sleeper.poll() is None
    finally:
        holder.terminate()
        sleeper.terminate()
        holder.wait(timeout=5)
        sleeper.wait(timeout=5)


def test_pause_resume_control_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    paused = write_control_state(cfg, paused=True)
    assert paused.paused is True
    assert control_path(cfg).exists()
    assert read_control_state(cfg).paused is True

    resumed = write_control_state(cfg, paused=False)
    assert resumed.paused is False
    assert read_control_state(cfg).paused is False


def test_collector_pause_resume_records_events_and_heartbeat(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    cfg = _cfg(tmp_path)
    db = BatteryDatabase(cfg.resolved_db_path(), cfg)
    db.init_schema()
    env = os.environ.copy()
    repo_src = Path(__file__).parents[1] / "src"
    env["PYTHONPATH"] = str(repo_src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "battery_auditor.cli",
            "--config",
            str(config_path),
            "collect",
            "--name",
            "pause-resume",
            "--interval",
            "0.05",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        session_id = _wait_for_open_session(db)
        write_control_state(cfg, paused=True)
        _wait_for_event(db, session_id, "SESSION_PAUSED")
        paused_status = collect_runtime_status(cfg, db).to_dict()
        assert paused_status["paused"] is True

        write_control_state(cfg, paused=False)
        _wait_for_event(db, session_id, "SESSION_RESUMED")

        proc.terminate()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _cfg(tmp_path: Path) -> AuditorConfig:
    return AuditorConfig(
        data_dir=tmp_path / "state",
        db_path=tmp_path / "state" / "battery.sqlite3",
        sysfs_power_supply_dir=FIXTURE,
        heartbeat_seconds=0.2,
    )


def _write_config(tmp_path: Path) -> Path:
    cfg = _cfg(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[general]",
                f"data_dir = {json.dumps(str(cfg.data_dir))}",
                f"db_path = {json.dumps(str(cfg.resolved_db_path()))}",
                f"sysfs_power_supply_dir = {json.dumps(str(FIXTURE))}",
                "",
                "[sampling]",
                "heartbeat_seconds = 0.2",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _wait_for_open_session(db: BatteryDatabase, *, timeout_seconds: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = db.list_open_sessions()
        if rows:
            return str(rows[0]["id"])
        time.sleep(0.05)
    raise AssertionError("collector did not open a session")


def _wait_for_event(
    db: BatteryDatabase,
    session_id: str,
    event_type: str,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(row["event_type"] == event_type for row in db.fetch_events(session_id, limit=1000)):
            return
        time.sleep(0.05)
    raise AssertionError(f"collector did not record {event_type}")
