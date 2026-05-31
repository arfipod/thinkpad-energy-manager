# Measurement methodology

## Profiles

### passive

Use: long-term tracking.

- interval: 10 s by default;
- low impact;
- useful for seeing general trends.

```bash
thinkpad-energy-manager collect --mode passive --name tracking
```

### diagnostic

Use: normal controlled discharge.

- interval: 2 s by default;
- balance between precision and low impact.

```bash
thinkpad-energy-manager collect --mode diagnostic --name normal-discharge
```

### blackbox

Use: diagnostics until shutdown or failure.

- interval: 1 s by default;
- SQLite `synchronous=FULL`;
- flush on every sample;
- persistent heartbeat.

```bash
thinkpad-energy-manager collect --mode blackbox --name final-discharge
```

## Recommended test for a ThinkPad with two batteries

1. Charge to your usual level.
2. Start the collector in `diagnostic` or `blackbox` mode.
3. Disconnect AC.
4. Close the UI if you want minimal measurement noise.
5. Use the machine under a stable load or leave it in a controlled idle state.
6. When finished, export and analyze.

```bash
thinkpad-energy-manager sessions
thinkpad-energy-manager analyze
thinkpad-energy-manager analyze jumps
thinkpad-energy-manager analyze relearn
thinkpad-energy-manager thresholds status
thinkpad-energy-manager estimate
thinkpad-energy-manager export --format csv --out discharge.csv
```

## What to look for

### Probable bad calibration

- sudden jump in `capacity_percent`;
- gap between `capacity_percent` and `computed_percent`;
- `LOW_END_GAUGE_JUMP`, `IMPOSSIBLE_ENERGY_DROP`, or `RECOVERY_JUMP` from `thinkpad-energy-manager analyze jumps`;
- shutdown when `energy_now` still seems sufficient;
- improvement after recalibration.

The reported `capacity_percent` is a fuel-gauge estimate. It can be rounded, filtered, stale, or abruptly reconciled by firmware. The Wh-based `computed_percent` is derived from `energy_now / energy_full`, but even `energy_now` can jump when the embedded controller corrects its estimate. Impossible gauge jumps reduce confidence in runtime projections and in conclusions drawn from a single low-battery sample.

### Capacity relearning

`energy_full` is the battery or embedded controller's current estimate of usable full capacity. It can change after a deep discharge/charge cycle without the cells physically improving. For example, a move from 18.28 Wh to 19.91 Wh can make health appear to jump from about 78% to about 85%, but that is gauge relearning, not repair.

Run `thinkpad-energy-manager analyze relearn` after calibration tests. Relearn findings affect effective percentage and ETA modeling because the denominator for `energy_now / energy_full` changed; comparisons before and after the relearn should account for that boundary.

### Probable physical degradation

- low `health_percent`;
- quick voltage sag under load;
- shutdown while the percentage is still high;
- poor runtime even when the percentage is coherent.

### Normal dual-battery behavior

- one battery discharges first;
- the other remains stable;
- the active battery changes without abrupt energy jumps.

### Threshold mismatches

If you rely on TLP charge thresholds, check `thinkpad-energy-manager thresholds status` after resume, recalibration, or unusual charge behavior. The TLP configuration, UPower view, and sysfs readback can disagree. ThinkPad Energy Manager records sysfs values from the collector and reports mismatches offline, for example configured `75/80` but current sysfs `0/100`. This warns that the kernel-visible thresholds may not be enforcing the preservation window you intended.

Manual threshold restore is available with `thinkpad-energy-manager thresholds restore --dry-run` and `thinkpad-energy-manager thresholds restore --yes`. Keep it manual for pure observation. Enabling automatic restore after resume or mismatch can protect the intended charge window, but it also changes system behavior and should be recorded as part of the test conditions.

### Sleep and resume

With the optional logind sleep monitor enabled, the collector records `ABOUT_TO_SLEEP` and `RESUMED` events and takes an immediate post-resume sample. This helps distinguish a normal suspend from a low-battery shutdown during black-box tests.

The monitor is best-effort. A sudden power cut can prevent `ABOUT_TO_SLEEP` from being written, and some systems may miss D-Bus hooks. The wall-clock and monotonic timestamp gap remains the fallback and source of truth for classifying resume-like gaps.

### Effective percent and ETA

`thinkpad-energy-manager estimate` is a model, not a truth oracle. Raw percent is the kernel `capacity` field. Computed percent is `energy_now / energy_full`. Effective percent starts from observed Wh and learned full capacity, then applies configured reserve and uncertainty margins when the gauge is less trustworthy.

The ETA model uses recent discharge-only consumption windows: short, medium, and long. AC-connected periods, charging samples, AC transitions, and probable suspend gaps are excluded. Nominal ETA uses the medium window when enough data exists; pessimistic ETA uses higher recent consumption; optimistic ETA uses lower stable consumption. If there is not enough usable discharge history, ThinkPad Energy Manager reports unknown ETA and lowers confidence instead of inventing precision.

## Avoid contaminating the test

During a serious test:

- do not leave a browser open with live charts;
- do not run `tlp-stat` in a loop;
- do not export data continuously;
- use `diagnostic` or `blackbox` from the CLI/systemd;
- open the UI afterwards.
