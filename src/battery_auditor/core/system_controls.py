from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from battery_auditor.core.sysfs import parse_bool01, parse_int, read_text_file

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class CommandResult:
    title: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def combined_output(self) -> str:
        output = "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)
        if output:
            return output
        return f"{self.title}: exit code {self.returncode}"


@dataclass(slots=True)
class BacklightDevice:
    name: str
    brightness: int
    max_brightness: int
    percent: float
    writable: bool


@dataclass(slots=True)
class LedDevice:
    name: str
    brightness: int
    max_brightness: int
    trigger: str | None
    writable: bool


@dataclass(slots=True)
class RfkillDevice:
    name: str
    kind: str
    label: str
    soft_blocked: bool | None
    hard_blocked: bool | None
    enabled: bool | None
    writable: bool


@dataclass(slots=True)
class PowerBackendStatus:
    xset_available: bool
    gsettings_available: bool
    systemctl_available: bool
    shutdown_available: bool
    pkexec_available: bool


class SystemControls:
    """Small Linux control surface for ThinkPad LEDs, radios, and power knobs."""

    def __init__(
        self,
        *,
        backlight_root: Path = Path("/sys/class/backlight"),
        leds_root: Path = Path("/sys/class/leds"),
        rfkill_root: Path = Path("/sys/class/rfkill"),
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.backlight_root = backlight_root
        self.leds_root = leds_root
        self.rfkill_root = rfkill_root
        self._runner = runner

    def backend_status(self) -> PowerBackendStatus:
        return PowerBackendStatus(
            xset_available=shutil.which("xset") is not None,
            gsettings_available=shutil.which("gsettings") is not None,
            systemctl_available=shutil.which("systemctl") is not None,
            shutdown_available=shutil.which("shutdown") is not None,
            pkexec_available=shutil.which("pkexec") is not None,
        )

    def list_backlights(self) -> list[BacklightDevice]:
        devices: list[BacklightDevice] = []
        for path in self._children(self.backlight_root):
            brightness = parse_int(read_text_file(path / "brightness"))
            maximum = parse_int(read_text_file(path / "max_brightness"))
            if brightness is None or maximum is None or maximum <= 0:
                continue
            percent = max(0.0, min(100.0, (brightness / maximum) * 100.0))
            devices.append(
                BacklightDevice(
                    name=path.name,
                    brightness=brightness,
                    max_brightness=maximum,
                    percent=percent,
                    writable=self._is_writable(path / "brightness"),
                )
            )
        return devices

    def set_backlight_percent(self, name: str, percent: int | float) -> BacklightDevice:
        path = self._named_child(self.backlight_root, name)
        maximum = parse_int(read_text_file(path / "max_brightness"))
        if maximum is None or maximum <= 0:
            raise ValueError(f"Backlight {name} does not expose max_brightness.")
        clamped = max(0.0, min(100.0, float(percent)))
        raw = int(round((clamped / 100.0) * maximum))
        self._write_value(path / "brightness", raw)
        updated = self._backlight_by_name(name)
        if updated is None:
            raise ValueError(f"Backlight {name} disappeared after writing brightness.")
        return updated

    def list_leds(self) -> list[LedDevice]:
        devices: list[LedDevice] = []
        for path in self._children(self.leds_root):
            brightness = parse_int(read_text_file(path / "brightness"))
            maximum = parse_int(read_text_file(path / "max_brightness"))
            if brightness is None or maximum is None:
                continue
            devices.append(
                LedDevice(
                    name=path.name,
                    brightness=brightness,
                    max_brightness=maximum,
                    trigger=read_text_file(path / "trigger"),
                    writable=self._is_writable(path / "brightness"),
                )
            )
        return devices

    def set_led_brightness(self, name: str, brightness: int) -> LedDevice:
        path = self._named_child(self.leds_root, name)
        maximum = parse_int(read_text_file(path / "max_brightness"))
        if maximum is None:
            raise ValueError(f"LED {name} does not expose max_brightness.")
        raw = max(0, min(int(brightness), maximum))
        self._write_value(path / "brightness", raw)
        updated = self._led_by_name(name)
        if updated is None:
            raise ValueError(f"LED {name} disappeared after writing brightness.")
        return updated

    def list_rfkill(self) -> list[RfkillDevice]:
        devices: list[RfkillDevice] = []
        for path in self._children(self.rfkill_root):
            kind = read_text_file(path / "type")
            if not kind:
                continue
            soft_blocked = parse_bool01(read_text_file(path / "soft"))
            hard_blocked = parse_bool01(read_text_file(path / "hard"))
            enabled = None
            if soft_blocked is not None and hard_blocked is not None:
                enabled = not soft_blocked and not hard_blocked
            devices.append(
                RfkillDevice(
                    name=path.name,
                    kind=kind,
                    label=read_text_file(path / "name") or path.name,
                    soft_blocked=soft_blocked,
                    hard_blocked=hard_blocked,
                    enabled=enabled,
                    writable=self._is_writable(path / "soft"),
                )
            )
        return devices

    def set_rfkill_enabled(self, name: str, enabled: bool) -> RfkillDevice:
        path = self._named_child(self.rfkill_root, name)
        self._write_value(path / "soft", 0 if enabled else 1)
        updated = self._rfkill_by_name(name)
        if updated is None:
            raise ValueError(f"Radio {name} disappeared after writing rfkill state.")
        return updated

    def set_screen_idle_timeout(self, seconds: int) -> list[CommandResult]:
        seconds = max(0, int(seconds))
        results: list[CommandResult] = []
        if shutil.which("xset") is not None:
            if seconds == 0:
                results.append(self._run("Disable X11 screensaver", ["xset", "s", "off"]))
                results.append(self._run("Disable X11 DPMS", ["xset", "-dpms"]))
            else:
                results.append(self._run("Set X11 screensaver timeout", ["xset", "s", str(seconds), str(seconds)]))
                results.append(self._run("Set X11 DPMS off timeout", ["xset", "dpms", "0", "0", str(seconds)]))
        if shutil.which("gsettings") is not None:
            results.append(
                self._run(
                    "Set GNOME idle timeout",
                    ["gsettings", "set", "org.gnome.desktop.session", "idle-delay", f"uint32 {seconds}"],
                )
            )
        if not results:
            raise RuntimeError("No supported screen timeout backend found (xset or gsettings).")
        return results

    def set_sleep_timeout(self, power_source: str, seconds: int, action: str) -> CommandResult:
        source = power_source.lower()
        if source not in {"ac", "battery"}:
            raise ValueError("power_source must be 'ac' or 'battery'.")
        if action not in {"nothing", "suspend", "hibernate"}:
            raise ValueError("action must be one of: nothing, suspend, hibernate.")
        timeout_key = f"sleep-inactive-{source}-timeout"
        type_key = f"sleep-inactive-{source}-type"
        seconds = max(0, int(seconds))
        return self._run(
            f"Set GNOME {source} inactive sleep",
            [
                "gsettings",
                "set",
                "org.gnome.settings-daemon.plugins.power",
                timeout_key,
                str(seconds),
                "&&",
                "gsettings",
                "set",
                "org.gnome.settings-daemon.plugins.power",
                type_key,
                f"'{action}'",
            ],
            shell=True,
        )

    def run_power_action(self, action: str) -> CommandResult:
        command_by_action = {
            "suspend": ["systemctl", "suspend"],
            "hibernate": ["systemctl", "hibernate"],
            "poweroff": ["systemctl", "poweroff"],
            "reboot": ["systemctl", "reboot"],
        }
        if action not in command_by_action:
            raise ValueError("Unsupported power action.")
        return self._run(f"Run {action}", command_by_action[action])

    def schedule_poweroff(self, minutes: int) -> CommandResult:
        minutes = max(1, int(minutes))
        return self._run("Schedule power off", ["shutdown", "-h", f"+{minutes}"])

    def cancel_scheduled_poweroff(self) -> CommandResult:
        return self._run("Cancel scheduled power off", ["shutdown", "-c"])

    def _run(self, title: str, command: Sequence[str], *, shell: bool = False) -> CommandResult:
        try:
            completed = self._runner(
                " ".join(command) if shell else list(command),
                capture_output=True,
                check=False,
                text=True,
                shell=shell,
            )
        except OSError as exc:
            return CommandResult(
                title=title,
                command=tuple(command),
                returncode=127,
                stderr=str(exc),
            )
        return CommandResult(
            title=title,
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def _backlight_by_name(self, name: str) -> BacklightDevice | None:
        return next((device for device in self.list_backlights() if device.name == name), None)

    def _led_by_name(self, name: str) -> LedDevice | None:
        return next((device for device in self.list_leds() if device.name == name), None)

    def _rfkill_by_name(self, name: str) -> RfkillDevice | None:
        return next((device for device in self.list_rfkill() if device.name == name), None)

    @staticmethod
    def _children(root: Path) -> list[Path]:
        try:
            return sorted([path for path in root.iterdir() if path.is_dir() or path.is_symlink()], key=lambda p: p.name)
        except (FileNotFoundError, PermissionError, OSError):
            return []

    @staticmethod
    def _named_child(root: Path, name: str) -> Path:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"Invalid device name: {name}")
        path = root / name
        if not path.exists():
            raise ValueError(f"Device not found: {name}")
        return path

    def _write_value(self, path: Path, value: int | str) -> None:
        text = f"{value}\n"
        try:
            path.write_text(text, encoding="utf-8")
        except PermissionError:
            self._write_value_with_pkexec(path, value)

    def _write_value_with_pkexec(self, path: Path, value: int | str) -> None:
        if shutil.which("pkexec") is None:
            raise PermissionError(f"Permission denied: {path}")
        command = [
            "pkexec",
            "/bin/sh",
            "-c",
            'printf "%s\\n" "$1" > "$2"',
            "thinkpad-energy-manager-write",
            str(value),
            str(path),
        ]
        completed = self._runner(command, capture_output=True, check=False, text=True)
        if completed.returncode != 0:
            output = "\n".join(part for part in ((completed.stdout or "").strip(), (completed.stderr or "").strip()) if part)
            raise PermissionError(output or f"pkexec failed with exit code {completed.returncode}")

    @staticmethod
    def _is_writable(path: Path) -> bool:
        try:
            return path.exists() and (os.access(path, os.W_OK) or shutil.which("pkexec") is not None)
        except OSError:
            return False
