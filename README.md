# ThinkPad Energy Manager

ThinkPad Energy Manager is a local Linux tool that records, analyzes, and charts real battery behavior, especially on laptops with multiple batteries such as Lenovo ThinkPads with Power Bridge.

Its main goal is not to save battery power, but to **diagnose batteries without adding much measurement noise**.

## What's included

- Lightweight collector based on `/sys/class/power_supply`.
- Persistent SQLite writes with WAL.
- Black-box mode for tests where the laptop may shut down because the battery runs out.
- Event detection: AC changes, active battery changes, percentage jumps, sudden voltage sag, low/critical battery, and interrupted sessions.
- Optional Python + Qt/PySide6 UI with interactive pyqtgraph charts.
- ThinkPad controls tab for display brightness, keyboard and chassis LEDs, rfkill radios, screen idle timeout, suspend/hibernate/power-off actions, and delayed shutdown.
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
thinkpad-energy-manager once
```

Record a diagnostic session:

```bash
thinkpad-energy-manager collect --mode diagnostic --name normal-discharge
```

Show collector status:

```bash
thinkpad-energy-manager status
thinkpad-energy-manager status --json
```

Pause, resume, or stop the collector:

```bash
thinkpad-energy-manager pause
thinkpad-energy-manager resume
thinkpad-energy-manager stop
```

Use force stop only when normal stop cannot work:

```bash
thinkpad-energy-manager stop --force
```

Record in black-box mode:

```bash
thinkpad-energy-manager collect --mode blackbox --name discharge-to-shutdown
```

List sessions:

```bash
thinkpad-energy-manager sessions
```

Rename or annotate a session:

```bash
thinkpad-energy-manager rename-session <session_id> --name "post-recalibration run"
thinkpad-energy-manager note-session <session_id> --notes "BAT1 drained faster after 40%."
```

Merge sessions into a synthetic session:

```bash
thinkpad-energy-manager merge-sessions <id1> <id2> --name "merged-test"
```

Delete a session and its dependent rows:

```bash
thinkpad-energy-manager delete-session <session_id>
```

Analyze the latest session:

```bash
thinkpad-energy-manager analyze
```

Show semantic charge/discharge phases:

```bash
thinkpad-energy-manager analyze phases
thinkpad-energy-manager analyze --phases <session_id>
```

Show physically impossible or suspicious fuel-gauge jumps:

```bash
thinkpad-energy-manager analyze jumps
thinkpad-energy-manager analyze --jumps <session_id>
```

Show reported full-capacity relearning:

```bash
thinkpad-energy-manager analyze relearn
thinkpad-energy-manager analyze --relearn <session_id>
```

Show configured vs sysfs charge threshold readback:

```bash
thinkpad-energy-manager thresholds status
thinkpad-energy-manager thresholds status <session_id>
thinkpad-energy-manager thresholds restore --dry-run
thinkpad-energy-manager thresholds restore BAT0 --yes
```

Estimate effective charge state and runtime:

```bash
thinkpad-energy-manager estimate
thinkpad-energy-manager estimate --session <session_id>
thinkpad-energy-manager estimate --json
```

Export phases:

```bash
thinkpad-energy-manager analyze --phases <session_id> --format csv --out phases.csv
thinkpad-energy-manager analyze --phases <session_id> --format json --out phases.json
```

Export to CSV:

```bash
thinkpad-energy-manager export --format csv --out discharge.csv
```

Open the UI:

```bash
thinkpad-energy-manager-qt
```

## Qt UI screenshots

Status overview:

![Qt status overview](docs/screenshots/qt-status.png)

Recording controls:

![Qt recording controls](docs/screenshots/qt-recording.png)

Session management:

![Qt session management](docs/screenshots/qt-sessions.png)

Battery chart:

![Qt battery chart](docs/screenshots/qt-chart-battery.png)

Event chart:

![Qt event chart](docs/screenshots/qt-chart-events.png)

System metric chart:

![Qt system metric chart](docs/screenshots/qt-chart-system.png)

Events and TLP tools:

![Qt events table](docs/screenshots/qt-events.png)

![Qt TLP tools](docs/screenshots/qt-tlp.png)

## Collector lifecycle

ThinkPad Energy Manager keeps collector runtime state under the configured data directory:

- `collector.lock` records the active collector PID and is protected with an advisory lock;
- `heartbeats/*.json` records lightweight current-session state, including paused state, last seq, and heartbeat time;
- `collector.control.json` is the low-cost pause/resume control file checked by the collector between samples.

`thinkpad-energy-manager status` combines the lock, PID liveness, heartbeat files, and open SQLite sessions. It reports `RUNNING`, `PAUSED`, `STOPPED`, `STALE`, or `UNKNOWN`.

The Qt Recording tab uses the same CLI/runtime layer as the terminal. It can detect and control collectors started by CLI, systemd, or a previous UI instance. When the UI starts a collector, it starts a detached CLI process and writes output to `collector-ui.log` in the data directory. The collector keeps running after the Qt window closes.

While paused, the collector does not sample `/sys/class/power_supply` and does not insert normal samples. It updates a lightweight heartbeat and records `SESSION_PAUSED` / `SESSION_RESUMED` events.

Delete, merge, recover, and repair operations refuse to run while an active or ambiguous collector may be writing. Merging creates a new session, keeps source sessions untouched, preserves original wall-clock sample times, renumbers merged sample seq values from zero, and records provenance events.

## Black-box mode

Black-box mode is designed to bracket the moment of battery-related shutdown:

```bash
thinkpad-energy-manager collect --mode blackbox --name final-test
```

In this mode:

- default interval: 1 second;
- SQLite `synchronous=FULL`;
- flush on every sample;
- persistent per-session heartbeat;
- if the machine shuts down and the session remains open, the next `recover` marks it as `PROBABLE_POWER_LOSS`.

After rebooting:

```bash
thinkpad-energy-manager recover
thinkpad-energy-manager analyze
```

The exact shutdown instant cannot be measured after the machine loses power, but it can be bracketed by the last persisted heartbeat/sample and the configured interval.

Normal `thinkpad-energy-manager stop` sends `SIGTERM`, allowing the collector to end the session with `signal_or_user_stop`. Force stop sends `SIGKILL` and can leave the session open because the collector cannot run its clean shutdown path. Use `thinkpad-energy-manager recover` after reboot or after a force stop if a session remains open.

## Phase analysis

A phase is a stable span of samples with the same power context: AC state, active charging battery, active discharging battery, and durable battery statuses. The phase analyzer runs after collection from stored SQLite samples, so it does not add work to the sampling loop and old databases remain readable.

Dual-battery ThinkPads such as the T460s do not necessarily charge or discharge both packs uniformly. Firmware often chooses one pack at a time, so BAT0 may discharge while BAT1 stays almost flat, then BAT1 may become active later. Phase analysis turns that raw per-sample behavior into summaries with signed Wh deltas, average signed power, active battery inference, and classifications such as `DISCHARGE_BAT0`, `CHARGE_BAT1`, and `AC_IDLE`.

Example output:

```text
#  Start                End                  Dur    AC   Classification  Disch  Chg   BAT0 dWh  BAT1 dWh  Total dWh
-  -------------------  -------------------  -----  ---  --------------  -----  ----  --------  --------  ---------
0  2023-11-14 22:13:20  2023-11-14 22:16:20  3m00s  off  DISCHARGE_BAT0  BAT0   -     -3.000    0.000     -3.000
1  2023-11-14 22:17:20  2023-11-14 22:20:20  3m00s  on   CHARGE_BAT1     -      BAT1  0.000     3.000     3.000
2  2023-11-14 22:21:20  2023-11-14 22:24:20  3m00s  off  DISCHARGE_BAT1  BAT1   -     0.000     -3.000    -3.000
```

## Gauge jump analysis

Fuel-gauge percentages are firmware estimates, not direct measurements of real remaining energy. The reported `capacity` value can be smoothed, rounded, stale, or recalibrated by the embedded controller, so it is not always equal to the Wh-based percentage from `energy_now / energy_full`.

`thinkpad-energy-manager analyze jumps` compares consecutive samples per battery. It checks whether the observed `energy_now` change is plausible for the elapsed time and reported `power_now`, with noise tolerance, and also flags large reported percentage jumps. Low-end jumps below 25% are classified as `LOW_END_GAUGE_JUMP` because this is where bad calibration can hide shutdown risk. These findings lower confidence in later runtime and health conclusions: a battery that drops from about 18% to 6% in one sample may still have usable cells, but its gauge can no longer be trusted as a smooth remaining-energy signal.

## Capacity relearning

`thinkpad-energy-manager analyze relearn` scans stored samples for changes in each battery's reported `energy_full` and `energy_full_design`. A change such as 18.28 Wh to 19.91 Wh is reported as `ENERGY_FULL_RELEARN` when it is larger than the configured absolute and relative thresholds.

This does not mean the battery physically recovered; it means the reported full capacity changed. Embedded controllers can relearn or reconcile the full-capacity estimate after deep discharge, full charge, resume, or other firmware events. Because `computed_percent` is `energy_now / energy_full`, a relearn can change effective percent and ETA models even when the actual stored energy did not suddenly change.

## Effective battery estimate

`thinkpad-energy-manager estimate` reports an estimated effective pack percentage and runtime ETA with confidence. It does not claim absolute truth. It combines observed Wh, current learned `energy_full`, routing between packs, low-end gauge jump history, threshold state, and recent discharge-only consumption.

The terms are:

- raw percent: the kernel/firmware `capacity` percentage;
- computed percent: `energy_now / energy_full * 100`;
- effective percent: usable Wh after small uncertainty/reserve margins divided by learned pack full Wh.

ETA uses recent discharge samples only. AC-connected periods, charging samples, AC transitions, and probable suspend gaps are excluded. The nominal ETA prefers the medium window, while pessimistic and optimistic values use higher and lower stable recent consumption. If there is not enough discharge data, the ETA is unknown and confidence is low.

## User systemd services

Install units:

```bash
./scripts/install-user-service.sh
```

Enable the normal collector:

```bash
systemctl --user enable --now thinkpad-energy-manager.service
```

Start a black-box session under systemd:

```bash
systemctl --user start thinkpad-energy-manager-blackbox.service
```

The UI and `thinkpad-energy-manager status` detect these systemd-started collectors through the same lock and heartbeat files.

Optional sleep/resume hooks:

```bash
python -m pip install 'thinkpad-energy-manager[system]'
```

Then enable the logind monitor in config:

```toml
[sleep_monitor]
enabled = true
backend = "logind"
```

When available, the collector records `ABOUT_TO_SLEEP` before suspend and `RESUMED` after resume, then takes one immediate battery sample and records `RESUME_SAMPLE_TAKEN`. If the optional D-Bus dependency or logind hook is unavailable, it records `SLEEP_MONITOR_UNAVAILABLE` and continues. Wall-time/monotonic gap classification remains the fallback and source of truth when hooks are missed. A sudden power cut can prevent `ABOUT_TO_SLEEP` from being written.

View logs:

```bash
journalctl --user -u thinkpad-energy-manager.service -f
```

Uninstall units:

```bash
./scripts/uninstall-user-service.sh
```

## TLP

ThinkPad Energy Manager does not replace TLP. It complements it.

Useful commands:

```bash
thinkpad-energy-manager tlp-stat battery
thinkpad-energy-manager tlp-stat config
thinkpad-energy-manager tlp-setcharge BAT0 75 80
thinkpad-energy-manager tlp-setcharge BAT1 75 80
thinkpad-energy-manager tlp-recalibrate BAT0
thinkpad-energy-manager tlp-recalibrate BAT1
```

TLP actions are manual and are not part of the periodic collector, so they do not contaminate measurements.

## Threshold watchdog

Charge thresholds can be configured in TLP while the live sysfs readback reports something else. On some systems the expected 75/80 thresholds can temporarily appear as 0/100 after firmware, resume, or power-management events. That matters because the battery may charge outside the intended preservation window even though the user-space configuration still looks correct.

`thinkpad-energy-manager thresholds status` compares configured thresholds with the latest stored sysfs readback from `charge_control_start_threshold` / `charge_control_end_threshold`, falling back to `charge_start_threshold` / `charge_stop_threshold` when needed. It does not run privileged commands. TLP config, UPower, and sysfs can disagree because they observe or manage different layers; the watchdog treats sysfs as the current kernel readback and reports `OK`, `MISMATCH`, or `UNKNOWN`.

Restoring thresholds is optional and explicit because it changes charging behavior. Use `thinkpad-energy-manager thresholds restore --dry-run` to review the exact `sudo tlp setcharge <start> <stop> BAT*` commands. Run `thinkpad-energy-manager thresholds restore --yes` or target one battery such as `thinkpad-energy-manager thresholds restore BAT1 --yes` only after confirming the configured values. Automatic restore after resume or mismatch is disabled by default and must be enabled in config.

## Recorded data

For each sample, ThinkPad Energy Manager stores:

- timestamps: wall-clock, ISO, monotonic time, and sequence number;
- power state: AC online state, active battery names, computed total energy, total full/design energy, total power, computed total percentage, and total health percentage;
- collector overhead: sample read time, SQLite write time, collector RSS, collector user/system CPU seconds, and loop delay;
- system load context: CPU usage percentage, 1-minute load average, total/available memory, memory used percentage, and disk read/write bytes per second;
- environment context: display brightness percentage/raw/max value, WiFi enabled state, and Bluetooth enabled state;
- per battery: presence, status, reported percentage, Wh-based computed percentage, health, capacity level, energy, power, voltage, cycle count, technology, manufacturer, model, serial number, charge thresholds, charge behaviour, and raw sysfs values;
- non-battery power supplies: name, type, online state, and raw sysfs values;
- events: type, severity, battery, message, timestamps, and structured details.

See more in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Configuration

Copy the example:

```bash
mkdir -p ~/.config/thinkpad-energy-manager
cp examples/config.toml ~/.config/thinkpad-energy-manager/config.toml
```

Pay special attention to:

```toml
[sampling]
interval_seconds = 2.0

[thresholds]
restore_on_resume = false
restore_on_mismatch = false
restore_command = "tlp"
require_confirmation = true

[thresholds.BAT0]
start = 75
stop = 80

[thresholds.BAT1]
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
thinkpad-energy-manager collect --mode diagnostic --name cli-test
thinkpad-energy-manager status --json
thinkpad-energy-manager pause
thinkpad-energy-manager status
thinkpad-energy-manager resume
thinkpad-energy-manager stop
thinkpad-energy-manager sessions
thinkpad-energy-manager-qt
thinkpad-energy-manager merge-sessions <id1> <id2> --name "merged-test"
thinkpad-energy-manager export <merged_id> --format csv --out merged.csv
```

## Project status

Initial functional version. The collector, SQLite, CLI, systemd units, and UI are ready to use and evolve. Upcoming improvements are listed in [`docs/ROADMAP.md`](docs/ROADMAP.md).
