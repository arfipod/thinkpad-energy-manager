from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from battery_auditor.config import AuditorConfig
from battery_auditor.core.models import wall_iso_from_timestamp

STATUS_RUNNING = "RUNNING"
STATUS_PAUSED = "PAUSED"
STATUS_STOPPED = "STOPPED"
STATUS_STALE = "STALE"
STATUS_UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class CollectorLockInfo:
    path: str
    exists: bool
    held: bool
    pid: int | None = None
    raw: str | None = None
    age_seconds: float | None = None
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    exists: bool
    cmdline: list[str] = field(default_factory=list)
    comm: str | None = None
    collector_like: bool = False
    verify_reason: str = "not_checked"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ControlState:
    path: str
    exists: bool
    paused: bool = False
    updated_wall: float | None = None
    updated_iso: str | None = None
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StopResult:
    ok: bool
    message: str
    pid: int | None = None
    signal_name: str | None = None
    timed_out: bool = False
    unsafe: bool = False


@dataclass(slots=True)
class CollectorStatus:
    state: str
    generated_at_wall: float
    generated_at_iso: str
    pid: int | None
    pid_alive: bool
    pid_is_collector: bool
    current_session_id: str | None
    current_session_name: str | None
    last_heartbeat_age_seconds: float | None
    sample_count: int | None
    lock: CollectorLockInfo
    process: ProcessInfo | None
    control: ControlState
    heartbeats: list[dict[str, Any]]
    open_sessions: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        current_heartbeat = self._current_heartbeat()
        paused = self.state == STATUS_PAUSED or self.control.paused or any(
            bool(item.get("active")) and bool(item.get("paused")) for item in self.heartbeats
        )
        return {
            "state": self.state,
            "collector_state": self.state.lower(),
            "generated_at_wall": self.generated_at_wall,
            "generated_at_iso": self.generated_at_iso,
            "pid": self.pid,
            "pid_alive": self.pid_alive,
            "pid_is_collector": self.pid_is_collector,
            "current_session_id": self.current_session_id,
            "current_session_name": self.current_session_name,
            "active_session_id": self.current_session_id,
            "active_session_name": self.current_session_name,
            "paused": paused,
            "last_heartbeat_iso": None if current_heartbeat is None else current_heartbeat.get("wall_iso"),
            "last_heartbeat_age_seconds": self.last_heartbeat_age_seconds,
            "last_seq": None if current_heartbeat is None else current_heartbeat.get("last_seq"),
            "sample_count": self.sample_count,
            "lock": self.lock.to_dict(),
            "lock_path": self.lock.path,
            "lock_age_seconds": self.lock.age_seconds,
            "process": None if self.process is None else self.process.to_dict(),
            "control": self.control.to_dict(),
            "heartbeats": self.heartbeats,
            "active_heartbeat_count": sum(1 for item in self.heartbeats if item.get("active")),
            "active_heartbeat_files": [item["path"] for item in self.heartbeats if item.get("active")],
            "open_sessions": self.open_sessions,
            "open_session_count": len(self.open_sessions),
            "warnings": self.warnings,
        }

    def _current_heartbeat(self) -> dict[str, Any] | None:
        if self.current_session_id is not None:
            for heartbeat in self.heartbeats:
                if heartbeat.get("session_id") == self.current_session_id:
                    return heartbeat
        for heartbeat in self.heartbeats:
            if heartbeat.get("active"):
                return heartbeat
        return None


def lock_path(cfg: AuditorConfig) -> Path:
    return cfg.data_dir.expanduser() / "collector.lock"


def control_path(cfg: AuditorConfig) -> Path:
    return cfg.data_dir.expanduser() / "collector.control.json"


def heartbeat_path(cfg: AuditorConfig, session_id: str) -> Path:
    return cfg.heartbeat_dir() / f"{session_id}.json"


def heartbeat_active_seconds(cfg: AuditorConfig) -> float:
    return max(30.0, float(cfg.heartbeat_seconds) * 5.0)


def read_lock_info(cfg: AuditorConfig) -> CollectorLockInfo:
    path = lock_path(cfg)
    exists = path.exists()
    raw: str | None = None
    pid: int | None = None
    parse_error: str | None = None
    age_seconds: float | None = None
    if exists:
        try:
            age_seconds = max(0.0, time.time() - path.stat().st_mtime)
            raw = path.read_text(encoding="utf-8").strip()
            pid = _parse_lock_pid(raw)
        except OSError as exc:
            parse_error = str(exc)
        except ValueError as exc:
            parse_error = str(exc)
    return CollectorLockInfo(
        path=str(path),
        exists=exists,
        held=_is_lock_held(path) if exists else False,
        pid=pid,
        raw=raw,
        age_seconds=age_seconds,
        parse_error=parse_error,
    )


def write_lock_payload(lock_file: Any) -> None:
    payload = {
        "app": "battery-auditor",
        "role": "collector",
        "pid": os.getpid(),
        "started_wall": time.time(),
        "started_iso": wall_iso_from_timestamp(time.time()),
    }
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    lock_file.flush()


def read_process_info(pid: int) -> ProcessInfo:
    exists = pid_exists(pid)
    cmdline = _read_proc_cmdline(pid) if exists else []
    comm = _read_proc_comm(pid) if exists else None
    collector_like, reason = process_looks_like_collector(cmdline, comm)
    if not exists:
        reason = "pid_not_alive"
    return ProcessInfo(
        pid=pid,
        exists=exists,
        cmdline=cmdline,
        comm=comm,
        collector_like=collector_like,
        verify_reason=reason,
    )


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_looks_like_collector(cmdline: list[str], comm: str | None = None) -> tuple[bool, str]:
    lowered = [item.lower() for item in cmdline]
    has_collect = any(item == "collect" for item in lowered)
    has_battery_auditor = any("battery-auditor" in item or "battery_auditor" in item for item in lowered)
    if has_collect and has_battery_auditor:
        return True, "cmdline_matches_collector"
    if comm and ("battery-auditor" in comm.lower() or "battery_auditor" in comm.lower()) and has_collect:
        return True, "comm_matches_collector"
    if not cmdline:
        return False, "cmdline_unavailable"
    if not has_battery_auditor:
        return False, "cmdline_missing_battery_auditor"
    return False, "cmdline_missing_collect"


def read_control_state(cfg: AuditorConfig) -> ControlState:
    path = control_path(cfg)
    if not path.exists():
        return ControlState(path=str(path), exists=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("control file must contain a JSON object")
        updated_wall = _optional_float(data.get("updated_wall"))
        updated_iso = data.get("updated_iso")
        return ControlState(
            path=str(path),
            exists=True,
            paused=bool(data.get("paused", False)),
            updated_wall=updated_wall,
            updated_iso=str(updated_iso) if updated_iso is not None else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ControlState(path=str(path), exists=True, parse_error=str(exc))


def write_control_state(cfg: AuditorConfig, *, paused: bool) -> ControlState:
    cfg.data_dir.expanduser().mkdir(parents=True, exist_ok=True)
    path = control_path(cfg)
    now = time.time()
    payload = {
        "version": 1,
        "paused": paused,
        "updated_wall": now,
        "updated_iso": wall_iso_from_timestamp(now),
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return read_control_state(cfg)


def write_heartbeat(
    cfg: AuditorConfig,
    *,
    session_id: str,
    pid: int,
    paused: bool,
    sample_count: int,
    last_seq: int | None,
    wall_time: float,
    monotonic_time: float,
    extra: dict[str, Any] | None = None,
) -> None:
    cfg.heartbeat_dir().mkdir(parents=True, exist_ok=True)
    path = heartbeat_path(cfg, session_id)
    tmp_path = path.with_suffix(".json.tmp")
    payload: dict[str, Any] = {
        "version": 1,
        "pid": pid,
        "paused": paused,
        "session_id": session_id,
        "sample_count": sample_count,
        "seq": last_seq,
        "last_seq": last_seq,
        "wall_time": wall_time,
        "wall_iso": wall_iso_from_timestamp(wall_time),
        "monotonic_time": monotonic_time,
    }
    if extra:
        payload.update(extra)
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def remove_heartbeat(cfg: AuditorConfig, session_id: str) -> None:
    with suppress(FileNotFoundError):
        heartbeat_path(cfg, session_id).unlink()


def read_heartbeats(cfg: AuditorConfig) -> list[dict[str, Any]]:
    heartbeat_dir = cfg.heartbeat_dir()
    if not heartbeat_dir.exists():
        return []
    now = time.time()
    active_seconds = heartbeat_active_seconds(cfg)
    heartbeats: list[dict[str, Any]] = []
    for path in sorted(heartbeat_dir.glob("*.json")):
        payload: dict[str, Any]
        parse_error: str | None = None
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("heartbeat file must contain a JSON object")
            payload = dict(parsed)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            payload = {}
            parse_error = str(exc)
        stat_mtime: float | None = None
        with suppress(OSError):
            stat_mtime = path.stat().st_mtime
        wall_time = _optional_float(payload.get("wall_time"))
        reference_time = wall_time if wall_time is not None else stat_mtime
        age = None if reference_time is None else max(0.0, now - reference_time)
        heartbeat = {
            "path": str(path),
            "session_id": _optional_str(payload.get("session_id")),
            "pid": _optional_int(payload.get("pid")),
            "paused": bool(payload.get("paused", False)),
            "sample_count": _optional_int(payload.get("sample_count")),
            "seq": _optional_int(payload.get("seq")),
            "last_seq": _optional_int(payload.get("last_seq")),
            "wall_time": wall_time,
            "wall_iso": _optional_str(payload.get("wall_iso")),
            "monotonic_time": _optional_float(payload.get("monotonic_time")),
            "age_seconds": age,
            "active": False if age is None else age <= active_seconds,
            "parse_error": parse_error,
        }
        heartbeats.append(heartbeat)
    heartbeats.sort(key=lambda item: (item["age_seconds"] is None, item["age_seconds"] or 0.0))
    return heartbeats


def collect_runtime_status(cfg: AuditorConfig, db: Any | None = None) -> CollectorStatus:
    generated_at_wall = time.time()
    lock = read_lock_info(cfg)
    process = read_process_info(lock.pid) if lock.pid is not None else None
    control = read_control_state(cfg)
    heartbeats = read_heartbeats(cfg)
    active_heartbeats = [item for item in heartbeats if item.get("active")]
    open_sessions = _list_open_sessions(db)
    warnings: list[str] = []

    current_session_id = _current_session_id(active_heartbeats, open_sessions)
    current_session = _find_session(open_sessions, current_session_id)
    if current_session is None:
        current_session = _get_session(db, current_session_id)
    current_session_name = _optional_str(current_session.get("name")) if current_session else None
    current_heartbeat = _find_heartbeat(active_heartbeats, current_session_id) or (active_heartbeats[0] if active_heartbeats else None)
    last_heartbeat_age = (
        _optional_float(current_heartbeat.get("age_seconds")) if current_heartbeat is not None else None
    )
    sample_count = _sample_count(current_session, current_heartbeat)

    pid_alive = bool(process and process.exists)
    pid_is_collector = bool(process and process.collector_like)
    paused = control.paused or bool(current_heartbeat and current_heartbeat.get("paused"))

    if control.parse_error:
        warnings.append(f"Control file could not be parsed: {control.parse_error}")
    if lock.parse_error:
        warnings.append(f"Collector lock could not be parsed: {lock.parse_error}")
    if process and process.exists and not process.collector_like:
        warnings.append(f"Lock PID does not look like a Battery Auditor collector: {process.verify_reason}")

    state = _derive_state(
        lock=lock,
        pid_alive=pid_alive,
        pid_is_collector=pid_is_collector,
        paused=paused,
        active_heartbeat_count=len(active_heartbeats),
        open_session_count=len(open_sessions),
    )

    return CollectorStatus(
        state=state,
        generated_at_wall=generated_at_wall,
        generated_at_iso=wall_iso_from_timestamp(generated_at_wall),
        pid=lock.pid,
        pid_alive=pid_alive,
        pid_is_collector=pid_is_collector,
        current_session_id=current_session_id,
        current_session_name=current_session_name,
        last_heartbeat_age_seconds=last_heartbeat_age,
        sample_count=sample_count,
        lock=lock,
        process=process,
        control=control,
        heartbeats=heartbeats,
        open_sessions=open_sessions,
        warnings=warnings,
    )


def stop_collector(cfg: AuditorConfig, *, force: bool = False, timeout_seconds: float = 5.0) -> StopResult:
    status = collect_runtime_status(cfg)
    pid = status.pid
    if pid is None:
        return StopResult(ok=True, message="No collector lock PID was found.")
    if not status.lock.held:
        return StopResult(
            ok=True,
            pid=pid,
            message="No active collector lock is held; refusing to signal a stale PID.",
        )
    if not status.pid_alive:
        return StopResult(ok=True, pid=pid, message="Collector PID is no longer alive.")
    if not status.pid_is_collector:
        reason = status.process.verify_reason if status.process is not None else "unknown_process"
        return StopResult(
            ok=False,
            pid=pid,
            unsafe=True,
            message=f"Refusing to signal PID {pid}: process does not look like a Battery Auditor collector ({reason}).",
        )

    sig = signal.SIGKILL if force else signal.SIGTERM
    signal_name = "SIGKILL" if force else "SIGTERM"
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return StopResult(ok=True, pid=pid, signal_name=signal_name, message="Collector already exited.")
    except PermissionError as exc:
        return StopResult(ok=False, pid=pid, signal_name=signal_name, message=f"Permission denied: {exc}")

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = collect_runtime_status(cfg)
        if not current.lock.held or current.pid != pid or not pid_exists(pid):
            return StopResult(ok=True, pid=pid, signal_name=signal_name, message=f"Sent {signal_name} to collector PID {pid}.")
    return StopResult(
        ok=False,
        pid=pid,
        signal_name=signal_name,
        timed_out=True,
        message=f"Sent {signal_name} to collector PID {pid}, but it did not exit within {timeout_seconds:.1f}s.",
    )


def _derive_state(
    *,
    lock: CollectorLockInfo,
    pid_alive: bool,
    pid_is_collector: bool,
    paused: bool,
    active_heartbeat_count: int,
    open_session_count: int,
) -> str:
    if lock.held and pid_alive and pid_is_collector:
        return STATUS_PAUSED if paused else STATUS_RUNNING
    if lock.held and pid_alive and not pid_is_collector:
        return STATUS_UNKNOWN
    if active_heartbeat_count and not (lock.held and pid_alive):
        return STATUS_UNKNOWN
    if lock.exists or open_session_count:
        return STATUS_STALE
    return STATUS_STOPPED


def _list_open_sessions(db: Any | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        return [dict(row) for row in db.list_open_sessions()]
    except Exception:  # noqa: BLE001 - status should stay available if DB is damaged
        return []


def _current_session_id(active_heartbeats: list[dict[str, Any]], open_sessions: list[dict[str, Any]]) -> str | None:
    for heartbeat in active_heartbeats:
        session_id = _optional_str(heartbeat.get("session_id"))
        if session_id:
            return session_id
    if open_sessions:
        return _optional_str(open_sessions[0].get("id"))
    return None


def _find_session(open_sessions: list[dict[str, Any]], session_id: str | None) -> dict[str, Any] | None:
    if session_id is None:
        return None
    for session in open_sessions:
        if session.get("id") == session_id:
            return session
    return None


def _get_session(db: Any | None, session_id: str | None) -> dict[str, Any] | None:
    if db is None or session_id is None:
        return None
    try:
        row = db.get_session(session_id)
    except Exception:  # noqa: BLE001 - runtime status should tolerate DB damage
        return None
    return None if row is None else dict(row)


def _find_heartbeat(heartbeats: list[dict[str, Any]], session_id: str | None) -> dict[str, Any] | None:
    if session_id is None:
        return None
    for heartbeat in heartbeats:
        if heartbeat.get("session_id") == session_id:
            return heartbeat
    return None


def _sample_count(session: dict[str, Any] | None, heartbeat: dict[str, Any] | None) -> int | None:
    if session is not None:
        value = _optional_int(session.get("sample_count"))
        if value is not None:
            return value
    if heartbeat is not None:
        value = _optional_int(heartbeat.get("sample_count"))
        if value is not None:
            return value
        last_seq = _optional_int(heartbeat.get("last_seq"))
        if last_seq is not None and last_seq >= 0:
            return last_seq + 1
    return None


def _parse_lock_pid(raw: str) -> int | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return int(raw)
    if isinstance(data, dict):
        pid = data.get("pid")
        return _optional_int(pid)
    if isinstance(data, int):
        return data
    raise ValueError("collector lock must contain a PID or JSON object with a pid")


def _is_lock_held(path: Path) -> bool:
    try:
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _read_proc_cmdline(pid: int) -> list[str]:
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        data = path.read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in data.split(b"\0") if part]


def _read_proc_comm(pid: int) -> str | None:
    path = Path("/proc") / str(pid) / "comm"
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
