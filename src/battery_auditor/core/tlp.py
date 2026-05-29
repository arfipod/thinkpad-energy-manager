from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def combined_output(self) -> str:
        if self.stderr:
            return f"{self.stdout}\n\n[stderr]\n{self.stderr}".strip()
        return self.stdout


class TlpClient:
    """Thin TLP wrapper.

    TLP commands are intentionally outside the collector hot path. They are
    useful for diagnostics and manual actions, but periodic calls would
    contaminate low-power measurement runs.
    """

    def __init__(self, use_sudo: bool = True, timeout_seconds: int = 30) -> None:
        self.use_sudo = use_sudo
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return shutil.which("tlp") is not None or shutil.which("tlp-stat") is not None

    def stat_battery(self) -> CommandResult:
        return self._run(["tlp-stat", "-b"], sudo=True)

    def stat_config(self) -> CommandResult:
        return self._run(["tlp-stat", "-c"], sudo=True)

    def stat_system(self) -> CommandResult:
        return self._run(["tlp-stat", "-s"], sudo=True)

    def setcharge(self, start: int, stop: int, battery: str) -> CommandResult:
        self._validate_thresholds(start, stop)
        self._validate_battery_name(battery)
        return self._run(["tlp", "setcharge", str(start), str(stop), battery], sudo=True)

    def fullcharge(self, battery: str) -> CommandResult:
        self._validate_battery_name(battery)
        return self._run(["tlp", "fullcharge", battery], sudo=True)

    def recalibrate(self, battery: str) -> CommandResult:
        self._validate_battery_name(battery)
        return self._run(["tlp", "recalibrate", battery], sudo=True, timeout_seconds=None)

    def _run(self, command: list[str], sudo: bool, timeout_seconds: int | None = 30) -> CommandResult:
        executable = command[0]
        if shutil.which(executable) is None:
            return CommandResult(command, 127, "", f"Command not found: {executable}")
        full_command = command
        if sudo and self.use_sudo and shutil.which("sudo") is not None:
            full_command = ["sudo", *command]
        try:
            completed = subprocess.run(
                full_command,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds if timeout_seconds == 30 else timeout_seconds,
                check=False,
            )
            return CommandResult(full_command, completed.returncode, completed.stdout, completed.stderr)
        except subprocess.TimeoutExpired as exc:
            stdout = self._timeout_output_to_text(exc.stdout)
            stderr = self._timeout_output_to_text(exc.stderr) or "Command timed out"
            return CommandResult(full_command, 124, stdout, stderr)

    @staticmethod
    def _validate_thresholds(start: int, stop: int) -> None:
        if not (0 <= start <= 99):
            raise ValueError("start threshold must be between 0 and 99")
        if not (1 <= stop <= 100):
            raise ValueError("stop threshold must be between 1 and 100")
        if start >= stop:
            raise ValueError("start threshold must be lower than stop threshold")

    @staticmethod
    def _validate_battery_name(battery: str) -> None:
        if not battery.startswith("BAT") or not battery[3:].isdigit():
            raise ValueError("battery name must look like BAT0, BAT1, ...")

    @staticmethod
    def _timeout_output_to_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
