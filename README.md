# Battery Auditor

Battery Auditor is a local Linux tool that records, analyzes, and charts real battery behavior, especially on laptops with multiple batteries such as Lenovo ThinkPads with Power Bridge.

Its main goal is not to save battery power, but to **diagnose batteries without adding much measurement noise**.

## What's included

- Lightweight collector based on `/sys/class/power_supply`.
- Persistent SQLite writes with WAL.
- Black-box mode for tests where the laptop may shut down because the battery runs out.
- Event detection: AC changes, active battery changes, percentage jumps, sudden voltage sag, low/critical battery, and interrupted sessions.
- Optional Python + Qt/PySide6 UI with interactive pyqtgraph charts.
- Manual TLP wrapper: `tlp-stat`, `setcharge`, `recalibrate`.
- User-level systemd services.
- CSV/JSON export for external analysis.
- Collector status, pause/resume/stop control, and session management.

## Non-invasive design

The collector does not run `tlp-stat`, `upower`, `acpi`, `journalctl`, or any other external command in a loop. In the hot path it only performs:

1. small file reads from `/sys/class/power_supply`;
2. derived metric calculations;
3. compact row inserts into SQLite.

The Qt UI is a viewer/controller, not the owner of the collector. Closing the UI does not mean "stop measuring". For a serious discharge test, it is still best to leave only the collector or systemd service running and reopen the UI when you want to inspect or control it.

## Installation on Debian 13 / modern Linux

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tlp libxcb-cursor0

# For the Qt UI via pip:
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[ui]'
```

If you prefer to install UI dependencies from Debian packages, install the `python3-pyside6*` and `python3-pyqtgraph` packages available for your version and then run:

```bash
python -m pip install -e .
```

## Quick start

Read a single snapshot:

```bash
battery-auditor once
```

Record a diagnostic session:

```bash
battery-auditor collect --mode diagnostic --name normal-discharge
```

Show collector status:

```bash
battery-auditor status
battery-auditor status --json
```

Pause, resume, or stop the collector:

```bash
battery-auditor pause
battery-auditor resume
battery-auditor stop
```

Use force stop only when normal stop cannot work:

```bash
battery-auditor stop --force
```

Record in black-box mode:

```bash
battery-auditor collect --mode blackbox --name discharge-to-shutdown
```

List sessions:

```bash
battery-auditor sessions
```

Rename or annotate a session:

```bash
battery-auditor rename-session <session_id> --name "post-recalibration run"
battery-auditor note-session <session_id> --notes "BAT1 drained faster after 40%."
```

Merge sessions into a synthetic session:

```bash
battery-auditor merge-sessions <id1> <id2> --name "merged-test"
```

Delete a session and its dependent rows:

```bash
battery-auditor delete-session <session_id>
```

Analyze the latest session:

```bash
battery-auditor analyze
```

Export to CSV:

```bash
battery-auditor export --format csv --out discharge.csv
```

Open the UI:

```bash
battery-auditor-qt
```

## Collector lifecycle

Battery Auditor keeps collector runtime state under the configured data directory:

- `collector.lock` records the active collector PID and is protected with an advisory lock;
- `heartbeats/*.json` records lightweight current-session state, including paused state, last seq, and heartbeat time;
- `collector.control.json` is the low-cost pause/resume control file checked by the collector between samples.

`battery-auditor status` combines the lock, PID liveness, heartbeat files, and open SQLite sessions. It reports `RUNNING`, `PAUSED`, `STOPPED`, `STALE`, or `UNKNOWN`.

The Qt Recording tab uses the same CLI/runtime layer as the terminal. It can detect and control collectors started by CLI, systemd, or a previous UI instance. When the UI starts a collector, it starts a detached CLI process and writes output to `collector-ui.log` in the data directory. The collector keeps running after the Qt window closes.

While paused, the collector does not sample `/sys/class/power_supply` and does not insert normal samples. It updates a lightweight heartbeat and records `SESSION_PAUSED` / `SESSION_RESUMED` events.

Delete, merge, recover, and repair operations refuse to run while an active or ambiguous collector may be writing. Merging creates a new session, keeps source sessions untouched, preserves original wall-clock sample times, renumbers merged sample seq values from zero, and records provenance events.

## Black-box mode

Black-box mode is designed to bracket the moment of battery-related shutdown:

```bash
battery-auditor collect --mode blackbox --name final-test
```

In this mode:

- default interval: 1 second;
- SQLite `synchronous=FULL`;
- flush on every sample;
- persistent per-session heartbeat;
- if the machine shuts down and the session remains open, the next `recover` marks it as `PROBABLE_POWER_LOSS`.

After rebooting:

```bash
battery-auditor recover
battery-auditor analyze
```

The exact shutdown instant cannot be measured after the machine loses power, but it can be bracketed by the last persisted heartbeat/sample and the configured interval.

Normal `battery-auditor stop` sends `SIGTERM`, allowing the collector to end the session with `signal_or_user_stop`. Force stop sends `SIGKILL` and can leave the session open because the collector cannot run its clean shutdown path. Use `battery-auditor recover` after reboot or after a force stop if a session remains open.

## User systemd services

Install units:

```bash
./scripts/install-user-service.sh
```

Enable the normal collector:

```bash
systemctl --user enable --now battery-auditor.service
```

Start a black-box session under systemd:

```bash
systemctl --user start battery-auditor-blackbox.service
```

The UI and `battery-auditor status` detect these systemd-started collectors through the same lock and heartbeat files.

View logs:

```bash
journalctl --user -u battery-auditor.service -f
```

Uninstall units:

```bash
./scripts/uninstall-user-service.sh
```

## TLP

Battery Auditor does not replace TLP. It complements it.

Useful commands:

```bash
battery-auditor tlp-stat battery
battery-auditor tlp-stat config
battery-auditor tlp-setcharge BAT0 75 80
battery-auditor tlp-setcharge BAT1 75 80
battery-auditor tlp-recalibrate BAT0
battery-auditor tlp-recalibrate BAT1
```

TLP actions are manual and are not part of the periodic collector, so they do not contaminate measurements.

## Recorded data

For each sample, Battery Auditor stores:

- wall-clock and ISO timestamp;
- monotonic timestamp;
- AC state;
- computed total energy;
- total power;
- computed total percentage;
- internal collector metrics;
- per battery: status, reported percentage, Wh-based computed percentage, health, energy, power, voltage, cycles, technology, manufacturer, model, serial number, and thresholds exposed by sysfs.

See more in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Configuration

Copy the example:

```bash
mkdir -p ~/.config/battery-auditor
cp examples/config.toml ~/.config/battery-auditor/config.toml
```

Pay special attention to:

```toml
[sampling]
interval_seconds = 2.0

[expected_thresholds.BAT0]
start = 75
stop = 80

[expected_thresholds.BAT1]
start = 75
stop = 80
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ui,dev]'
pytest
ruff check .
```

## Manual lifecycle validation

```bash
battery-auditor collect --mode diagnostic --name cli-test
battery-auditor status --json
battery-auditor pause
battery-auditor status
battery-auditor resume
battery-auditor stop
battery-auditor sessions
battery-auditor-qt
battery-auditor merge-sessions <id1> <id2> --name "merged-test"
battery-auditor export <merged_id> --format csv --out merged.csv
```

## Project status

Initial functional version. The collector, SQLite, CLI, systemd units, and UI are ready to use and evolve. Upcoming improvements are listed in [`docs/ROADMAP.md`](docs/ROADMAP.md).
