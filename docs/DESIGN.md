# Design

## Goals

1. Measure batteries without introducing meaningful artificial load.
2. Separate the collector, storage, analysis, and UI.
3. Record enough information to diagnose multiple batteries.
4. Support sessions that end because of unexpected shutdown.
5. Integrate with TLP without calling TLP in a loop.

## Architecture

```text
thinkpad-energy-manager
|-- collector
|  |-- reads /sys/class/power_supply
|  |-- generates events
|  |-- writes SQLite
|  `-- updates heartbeat
|
|-- SQLite
|  |-- sessions
|  |-- samples
|  |-- sample_batteries
|  |-- power_supplies
|  `-- events
|
|-- CLI
|  |-- once
|  |-- collect
|  |-- sessions
|  |-- analyze
|  |-- analyze phases
|  |-- export
|  `-- tlp-*
|
`-- optional Qt UI
   |-- current status
   |-- manual recording
   |-- charts
   |-- events
   `-- TLP panel
```

## Collector hot path

The collector only performs cheap operations:

- `read()` from sysfs;
- string conversion to integers/floats;
- SQLite insert;
- small JSON heartbeat.

It does not use DBus, UPower, TLP, or periodic external commands.

## Why SQLite WAL

SQLite WAL allows writing samples without blocking UI reads. Normal mode uses `synchronous=NORMAL`. Black-box mode switches to `FULL` and forces a flush for every sample to prioritize durability over minimum power use.

## Per-battery measurement

ThinkPad Energy Manager does not rely only on the global percentage. Each battery has:

- kernel-reported percentage;
- computed percentage: `energy_now / energy_full * 100`;
- health: `energy_full / energy_full_design * 100`;
- instantaneous power;
- voltage;
- charging/discharging state;
- charge thresholds exposed by sysfs.

This makes it possible to distinguish normal firmware-controlled discharge from voltage sag, bad calibration, or physical degradation.

## Phase analysis

Raw samples are useful for charts, but dual-battery behavior is often easier to reason about as phases. A phase is a semantically stable run of samples where AC state, active discharging battery, active charging battery, and durable battery statuses stay the same. The analyzer debounces state changes so one-sample status noise does not create false phases, and short transitions can be kept as `MIXED_TRANSITION` instead of pretending they are a clean charge or discharge span.

The phase analyzer is intentionally post-processing only. It reads existing `samples` and `sample_batteries` rows through the normal read-only database path and does not run inside the collector loop.

On Lenovo dual-battery systems such as a ThinkPad T460s, firmware commonly charges or drains one pack while the other remains nearly flat. For example, BAT0 can discharge with BAT1 idle, then after AC connects BAT1 can charge while BAT0 stays flat. The analyzer preserves that behavior by computing per-battery signed Wh deltas, detecting inactive near-zero batteries, and classifying stable spans as `DISCHARGE_BAT0`, `DISCHARGE_BAT1`, `CHARGE_BAT0`, `CHARGE_BAT1`, `AC_IDLE`, `MIXED_TRANSITION`, or `UNKNOWN`.

## Gauge jump analysis

The gauge jump analyzer is also post-processing only. It compares consecutive stored samples for each battery using wall-clock delta, monotonic delta, `energy_now`, reported `capacity`, `power_now`, battery status, and AC state.

The physical check estimates the maximum plausible energy change as `power_now_w * dt_hours`, then applies an absolute Wh tolerance and a relative multiplier because embedded-controller readings are noisy. Large changes become findings such as `IMPOSSIBLE_ENERGY_DROP`, `IMPOSSIBLE_ENERGY_GAIN`, `PERCENTAGE_JUMP`, `LOW_END_GAUGE_JUMP`, or `RECOVERY_JUMP`. Short windows after AC/status/battery transitions are downgraded, and long suspend-like gaps are classified as after-resume reconciliation rather than normal discharge.

## Capacity relearning analysis

The relearn analyzer scans consecutive stored samples for per-battery changes in `energy_full`, `energy_full_design`, and derived health. It emits `ENERGY_FULL_RELEARN` only when `energy_full` crosses the configured absolute and relative thresholds. A design-capacity change is recorded separately as `ENERGY_FULL_DESIGN_CHANGE` so firmware metadata changes are not presented as ordinary relearning.

Reports explicitly warn that higher reported health does not mean physical capacity recovered. The analyzer is explaining that the denominator used for health, effective percent, and ETA modeling changed.

## Threshold watchdog

The threshold watchdog compares configured charge thresholds with stored sysfs readback for each battery. It uses `charge_control_start_threshold` and `charge_control_end_threshold`, falling back to `charge_start_threshold` and `charge_stop_threshold` when the newer names are unavailable. Status analysis never calls TLP, UPower, sudo, or other privileged commands.

The model records configured thresholds, current sysfs thresholds, sources, and status values: `OK`, `MISMATCH`, `UNKNOWN`, or `UNSUPPORTED`. A mismatch such as configured `75/80` with sysfs `0/100` emits `THRESHOLD_MISMATCH`; returning to the configured values emits `THRESHOLD_RESTORED`; missing readback emits `THRESHOLD_UNKNOWN`.

Threshold restore is a separate, opt-in action. Manual restore and the optional auto-restore paths emit `THRESHOLD_RESTORE_REQUESTED`, `THRESHOLD_RESTORE_DRY_RUN`, `THRESHOLD_RESTORE_SUCCESS`, `THRESHOLD_RESTORE_FAILED`, or `THRESHOLD_RESTORE_SKIPPED`. The collector only attempts restore when `restore_on_resume` or `restore_on_mismatch` is explicitly enabled.

## Sleep monitor

The optional sleep monitor listens for logind `PrepareForSleep(bool)` over D-Bus when `thinkpad-energy-manager[system]` is installed and `[sleep_monitor]` is enabled. It is event-driven; it does not poll. D-Bus callbacks enqueue lightweight notifications, and the collector thread performs the actual SQLite writes.

`PrepareForSleep(true)` becomes `ABOUT_TO_SLEEP`. `PrepareForSleep(false)` becomes `RESUMED`, wakes the collector loop, and requests one immediate battery sample with `RESUME_SAMPLE_TAKEN`. If the monitor cannot start, the collector records `SLEEP_MONITOR_UNAVAILABLE` and continues. Wall-time/monotonic gap classification remains the fallback and source of truth when hooks are unavailable or missed.

## Battery state model

The first battery model estimates effective pack percent and runtime from stored samples. It uses observed `energy_now`, current learned `energy_full`, per-battery routing, low-end gauge jump findings, threshold status, and robust recent discharge consumption. It never uses design capacity as current usable full capacity except as metadata.

The model exposes confidence and explanations because "real percent" is not directly measurable from sysfs. Low-end jumps, recent `energy_full` relearning, threshold mismatches, AC-connected data, and insufficient discharge history reduce confidence. Runtime ETA uses discharge-only windows and excludes AC, charging, transition, and suspend-gap samples.

## Events

Initial events:

- `AC_CONNECTED`
- `AC_DISCONNECTED`
- `BATTERY_SWITCH`
- `BATTERY_STATUS_CHANGE`
- `PERCENT_JUMP`
- `COMPUTED_PERCENT_JUMP`
- `IMPOSSIBLE_ENERGY_DROP`
- `IMPOSSIBLE_ENERGY_GAIN`
- `PERCENTAGE_JUMP`
- `LOW_END_GAUGE_JUMP`
- `RECOVERY_JUMP`
- `ENERGY_FULL_RELEARN`
- `ENERGY_FULL_DESIGN_CHANGE`
- `VOLTAGE_SAG`
- `LOW_BATTERY`
- `CRITICAL_BATTERY`
- `MISSED_SAMPLE_WINDOW`
- `THRESHOLD_MISMATCH`
- `THRESHOLD_RESTORED`
- `THRESHOLD_UNKNOWN`
- `ABOUT_TO_SLEEP`
- `RESUMED`
- `SLEEP_MONITOR_UNAVAILABLE`
- `RESUME_SAMPLE_TAKEN`
- `PROBABLE_POWER_LOSS`
