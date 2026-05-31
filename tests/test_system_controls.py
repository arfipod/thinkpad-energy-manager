from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from battery_auditor.core.system_controls import SystemControls


def test_backlight_percent_writes_scaled_raw_value(tmp_path: Path) -> None:
    backlight = tmp_path / "backlight" / "intel_backlight"
    backlight.mkdir(parents=True)
    (backlight / "brightness").write_text("50\n", encoding="utf-8")
    (backlight / "max_brightness").write_text("200\n", encoding="utf-8")

    controls = SystemControls(backlight_root=tmp_path / "backlight")

    device = controls.set_backlight_percent("intel_backlight", 25)

    assert (backlight / "brightness").read_text(encoding="utf-8").strip() == "50"
    assert device.percent == 25.0


def test_led_brightness_is_clamped_to_device_max(tmp_path: Path) -> None:
    led = tmp_path / "leds" / "thinkpad::kbd_backlight"
    led.mkdir(parents=True)
    (led / "brightness").write_text("0\n", encoding="utf-8")
    (led / "max_brightness").write_text("2\n", encoding="utf-8")
    (led / "trigger").write_text("[none] timer\n", encoding="utf-8")

    controls = SystemControls(leds_root=tmp_path / "leds")

    device = controls.set_led_brightness("thinkpad::kbd_backlight", 9)

    assert (led / "brightness").read_text(encoding="utf-8").strip() == "2"
    assert device.brightness == 2


def test_rfkill_enable_disable_writes_soft_block(tmp_path: Path) -> None:
    radio = tmp_path / "rfkill" / "rfkill0"
    radio.mkdir(parents=True)
    (radio / "type").write_text("wlan\n", encoding="utf-8")
    (radio / "name").write_text("phy0\n", encoding="utf-8")
    (radio / "soft").write_text("1\n", encoding="utf-8")
    (radio / "hard").write_text("0\n", encoding="utf-8")

    controls = SystemControls(rfkill_root=tmp_path / "rfkill")

    enabled = controls.set_rfkill_enabled("rfkill0", True)
    disabled = controls.set_rfkill_enabled("rfkill0", False)

    assert enabled.enabled is True
    assert disabled.enabled is False
    assert (radio / "soft").read_text(encoding="utf-8").strip() == "1"


def test_screen_timeout_uses_available_xset_and_gsettings(monkeypatch: Any) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"xset", "gsettings"} else None

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("battery_auditor.core.system_controls.shutil.which", fake_which)
    controls = SystemControls(runner=fake_run)

    results = controls.set_screen_idle_timeout(300)

    assert [result.returncode for result in results] == [0, 0, 0]
    assert calls[0][0] == ["xset", "s", "300", "300"]
    assert calls[1][0] == ["xset", "dpms", "0", "0", "300"]
    assert calls[2][0] == [
        "gsettings",
        "set",
        "org.gnome.desktop.session",
        "idle-delay",
        "uint32 300",
    ]


def test_power_commands_are_routed_through_runner() -> None:
    calls: list[Any] = []

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    controls = SystemControls(runner=fake_run)

    controls.run_power_action("suspend")
    controls.schedule_poweroff(5)
    controls.cancel_scheduled_poweroff()

    assert calls == [
        ["systemctl", "suspend"],
        ["shutdown", "-h", "+5"],
        ["shutdown", "-c"],
    ]


def test_led_write_falls_back_to_pkexec_when_sysfs_denies_permission(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    led = tmp_path / "leds" / "tpacpi::lid_logo_dot"
    led.mkdir(parents=True)
    brightness = led / "brightness"
    brightness.write_text("0\n", encoding="utf-8")
    brightness.chmod(0o444)
    (led / "max_brightness").write_text("255\n", encoding="utf-8")
    calls: list[Any] = []

    def fake_which(name: str) -> str | None:
        return "/usr/bin/pkexec" if name == "pkexec" else None

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        brightness.chmod(0o644)
        brightness.write_text(f"{command[-2]}\n", encoding="utf-8")
        brightness.chmod(0o444)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("battery_auditor.core.system_controls.shutil.which", fake_which)
    controls = SystemControls(leds_root=tmp_path / "leds", runner=fake_run)

    device = controls.set_led_brightness("tpacpi::lid_logo_dot", 42)

    assert device.brightness == 42
    assert calls and calls[0][:3] == ["pkexec", "/bin/sh", "-c"]
