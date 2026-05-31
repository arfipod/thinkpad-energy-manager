# TLP integration

Battery Auditor does not replace TLP. It records data and provides manual shortcuts.

## TLP diagnostics

```bash
battery-auditor tlp-stat battery
battery-auditor tlp-stat config
battery-auditor tlp-stat system
```

These commands may ask for `sudo`.

## Temporary thresholds

```bash
battery-auditor tlp-setcharge BAT0 75 80
battery-auditor tlp-setcharge BAT1 75 80
```

This uses `tlp setcharge`. Changes are temporary unless they are reflected in the TLP configuration.

## Recalibration

```bash
battery-auditor tlp-recalibrate BAT0
battery-auditor tlp-recalibrate BAT1
```

Do this for one battery at a time. During recalibration, it is useful to keep the collector recording so you can compare before/after.

## Threshold verification

The collector records these paths if they exist:

```text
/sys/class/power_supply/BAT*/charge_control_start_threshold
/sys/class/power_supply/BAT*/charge_control_end_threshold
/sys/class/power_supply/BAT*/charge_start_threshold
/sys/class/power_supply/BAT*/charge_stop_threshold
/sys/class/power_supply/BAT*/charge_behaviour
```

You can define expected thresholds in `config.toml`:

```toml
[thresholds.BAT0]
start = 75
stop = 80

[thresholds.BAT1]
start = 75
stop = 80
```

If the value read from sysfs does not match, `THRESHOLD_MISMATCH` is recorded. When readback returns to the configured values, `THRESHOLD_RESTORED` can be recorded; if readback is missing, the watchdog reports `THRESHOLD_UNKNOWN`.

Use the offline watchdog to summarize the latest readback:

```bash
battery-auditor thresholds status
```

TLP configuration, UPower, and sysfs can disagree. Battery Auditor does not treat TLP config as proof that the kernel-visible thresholds are active; it compares the configured target with the sysfs values captured by the collector.

## Design note

The collector does not call `tlp-stat` periodically. `tlp-stat` is useful for manual diagnostics, but not for low-impact power-consumption measurement.
