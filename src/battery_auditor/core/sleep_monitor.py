from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable

from battery_auditor.core.models import wall_iso_from_timestamp

ABOUT_TO_SLEEP = "ABOUT_TO_SLEEP"
RESUMED = "RESUMED"
SLEEP_MONITOR_UNAVAILABLE = "SLEEP_MONITOR_UNAVAILABLE"
RESUME_SAMPLE_TAKEN = "RESUME_SAMPLE_TAKEN"


@dataclass(frozen=True, slots=True)
class SleepMonitorEvent:
    event_type: str
    wall_time: float
    wall_iso: str
    monotonic_time: float
    backend: str = "logind"


@dataclass(frozen=True, slots=True)
class SleepMonitorUnavailable:
    reason: str
    backend: str = "logind"


class SleepMonitor:
    def start(self) -> SleepMonitorUnavailable | None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class DisabledSleepMonitor(SleepMonitor):
    def start(self) -> SleepMonitorUnavailable | None:
        return None

    def stop(self) -> None:
        return None


class LogindSleepMonitor(SleepMonitor):
    def __init__(self, callback: Callable[[SleepMonitorEvent], None]) -> None:
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested = threading.Event()
        self._started = threading.Event()
        self._unavailable: SleepMonitorUnavailable | None = None

    def start(self) -> SleepMonitorUnavailable | None:
        if self._thread is not None:
            return self._unavailable
        try:
            import dbus_next  # noqa: F401
        except ImportError as exc:
            return SleepMonitorUnavailable(reason=f"dbus-next is not installed: {exc}")

        self._thread = threading.Thread(target=self._run_thread, name="battery-auditor-sleep-monitor", daemon=True)
        self._thread.start()
        self._started.wait(timeout=2.0)
        return self._unavailable

    def stop(self) -> None:
        self._stop_requested.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._loop = None

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
            self._started.set()
            loop.run_forever()
        except Exception as exc:  # noqa: BLE001 - monitor must never crash collector
            self._unavailable = SleepMonitorUnavailable(reason=str(exc))
            self._started.set()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _connect(self) -> None:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await bus.introspect("org.freedesktop.login1", "/org/freedesktop/login1")
        proxy = bus.get_proxy_object("org.freedesktop.login1", "/org/freedesktop/login1", introspection)
        manager = proxy.get_interface("org.freedesktop.login1.Manager")
        manager.on_prepare_for_sleep(self._prepare_for_sleep)

    def _prepare_for_sleep(self, sleeping: bool) -> None:
        self.callback(make_sleep_monitor_event(ABOUT_TO_SLEEP if sleeping else RESUMED))


def build_sleep_monitor(
    *,
    enabled: bool,
    backend: str,
    callback: Callable[[SleepMonitorEvent], None],
) -> SleepMonitor:
    if not enabled:
        return DisabledSleepMonitor()
    if backend != "logind":
        return UnavailableSleepMonitor(f"Unsupported sleep monitor backend: {backend}", backend=backend)
    return LogindSleepMonitor(callback)


class UnavailableSleepMonitor(SleepMonitor):
    def __init__(self, reason: str, *, backend: str = "logind") -> None:
        self.reason = reason
        self.backend = backend

    def start(self) -> SleepMonitorUnavailable | None:
        return SleepMonitorUnavailable(reason=self.reason, backend=self.backend)

    def stop(self) -> None:
        return None


def make_sleep_monitor_event(event_type: str, *, backend: str = "logind") -> SleepMonitorEvent:
    now = time.time()
    return SleepMonitorEvent(
        event_type=event_type,
        wall_time=now,
        wall_iso=wall_iso_from_timestamp(now),
        monotonic_time=time.monotonic(),
        backend=backend,
    )
