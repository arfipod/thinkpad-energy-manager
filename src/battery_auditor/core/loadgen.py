from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
import os
import signal
import tempfile
import time
from contextlib import suppress
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthetic battery workload generator.")
    parser.add_argument("--cpu-workers", type=int, default=1)
    parser.add_argument("--cpu-duty", type=float, default=0.5)
    parser.add_argument("--memory-mib", type=int, default=0)
    parser.add_argument("--disk-mib-s", type=float, default=0.0)
    parser.add_argument("--disk-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stop_event = mp.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    memory = _allocate_memory(max(0, args.memory_mib))
    workers = [
        mp.Process(target=_cpu_worker, args=(stop_event, max(0.0, min(1.0, args.cpu_duty))), daemon=False)
        for _ in range(max(0, args.cpu_workers))
    ]
    if args.disk_mib_s > 0:
        workers.append(mp.Process(target=_disk_worker, args=(stop_event, args.disk_mib_s, args.disk_dir), daemon=False))

    for worker in workers:
        worker.start()

    try:
        while not stop_event.wait(1.0):
            _touch_memory(memory)
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=3.0)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2.0)
    return 0


def _allocate_memory(memory_mib: int) -> bytearray:
    if memory_mib <= 0:
        return bytearray()
    memory = bytearray(memory_mib * 1024 * 1024)
    _touch_memory(memory)
    return memory


def _touch_memory(memory: bytearray) -> None:
    if not memory:
        return
    page = 4096
    for offset in range(0, len(memory), page):
        memory[offset] = (memory[offset] + 1) % 256


def _cpu_worker(stop_event: mp.synchronize.Event, duty: float) -> None:
    period = 0.1
    busy_seconds = period * duty
    idle_seconds = period - busy_seconds
    payload = os.urandom(4096)
    while not stop_event.is_set():
        deadline = time.monotonic() + busy_seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            payload = hashlib.sha256(payload).digest()
        if idle_seconds > 0:
            stop_event.wait(idle_seconds)


def _disk_worker(stop_event: mp.synchronize.Event, mib_per_second: float, disk_dir: Path | None) -> None:
    block = os.urandom(1024 * 1024)
    target_dir = disk_dir.expanduser() if disk_dir is not None else Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"thinkpad-energy-manager-load-{os.getpid()}.bin"
    interval = 1.0 / max(0.1, mib_per_second)
    try:
        with target.open("wb") as fh:
            while not stop_event.is_set():
                start = time.monotonic()
                fh.write(block)
                if fh.tell() >= 512 * 1024 * 1024:
                    fh.seek(0)
                    fh.truncate(0)
                fh.flush()
                elapsed = time.monotonic() - start
                stop_event.wait(max(0.0, interval - elapsed))
    finally:
        with suppress(OSError):
            target.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
