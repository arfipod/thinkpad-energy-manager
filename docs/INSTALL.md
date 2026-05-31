# Installation

## Debian 13 / Trixie

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tlp libxcb-cursor0
```

Clone the repository and create the environment:

```bash
git clone https://github.com/angelrubiodev/battery-auditor.git
cd battery-auditor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[ui]'
```

Check that it can read the batteries:

```bash
battery-auditor once
```

## CLI only, no UI

```bash
python -m pip install -e .
```

## Optional sleep hooks

The collector can optionally listen for logind `PrepareForSleep` D-Bus signals. This is not required for normal collection:

```bash
python -m pip install -e '.[system]'
```

Then enable it in `config.toml`:

```toml
[sleep_monitor]
enabled = true
backend = "logind"
```

If the optional D-Bus dependency or logind signal is unavailable, the collector records `SLEEP_MONITOR_UNAVAILABLE` and keeps running. Gap classification from wall-clock and monotonic timestamps remains the fallback.

## UI Qt

The UI uses PySide6 and pyqtgraph. You can install both with pip:

```bash
python -m pip install -e '.[ui]'
```

Or install PySide6 and pyqtgraph with distribution packages if you have them available, then install the CLI package with `python -m pip install -e .`.

## systemd user service

```bash
./scripts/install-user-service.sh
systemctl --user enable --now battery-auditor.service
```

The service uses `%h/.local/bin/battery-auditor`. If you install inside a project virtualenv instead of using `pip install --user`, adjust `ExecStart` in:

```text
~/.config/systemd/user/battery-auditor.service
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart battery-auditor.service
```

The sleep monitor is designed to work from the user service when the user bus can connect to system logind. Some distributions or hardening profiles may require a system-level service for the most reliable sleep hooks; Battery Auditor does not install or require one by default.
