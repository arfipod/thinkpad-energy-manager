# TLP integration

ThinkPad Energy Manager does not replace TLP. It records data and provides manual shortcuts.

## TLP diagnostics

```bash
thinkpad-energy-manager tlp-stat battery
thinkpad-energy-manager tlp-stat config
thinkpad-energy-manager tlp-stat system
```

These commands may ask for `sudo`.

## Temporary thresholds

```bash
thinkpad-energy-manager tlp-setcharge BAT0 75 80
thinkpad-energy-manager tlp-setcharge BAT1 75 80
```

This uses `tlp setcharge`. Changes are temporary unless they are reflected in the TLP configuration.

## Recalibration

```bash
thinkpad-energy-manager tlp-recalibrate BAT0
thinkpad-energy-manager tlp-recalibrate BAT1
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

If the value read from sysfs does not match, `THRESHOLD_MISMATCH` is recorded. When readback returns to the configured values, `THRESHOLD_RESTORED` can be recorded; if readback is missing, the watchdog reports `THRESHOLD_UNKNOWN`.

Use the offline watchdog to summarize the latest readback:

```bash
thinkpad-energy-manager thresholds status
```

TLP configuration, UPower, and sysfs can disagree. ThinkPad Energy Manager does not treat TLP config as proof that the kernel-visible thresholds are active; it compares the configured target with the sysfs values captured by the collector.

## Optional threshold restore

If sysfs reports a mismatch such as `0/100`, you can ask ThinkPad Energy Manager to restore the configured values through TLP. This is manual by default:

```bash
thinkpad-energy-manager thresholds restore --dry-run
thinkpad-energy-manager thresholds restore BAT0 --dry-run
thinkpad-energy-manager thresholds restore BAT0 --yes
```

The command uses `sudo tlp setcharge <start> <stop> BAT*`. Review the dry-run output first. Restoring thresholds can change when the machine charges or stops charging, so automatic restore is disabled unless you set `restore_on_resume = true` or `restore_on_mismatch = true`. If sudo or TLP fails, ThinkPad Energy Manager records `THRESHOLD_RESTORE_FAILED`; dry runs record `THRESHOLD_RESTORE_DRY_RUN` and never report success.

## Design note

The collector does not call `tlp-stat` periodically. `tlp-stat` is useful for manual diagnostics, but not for low-impact power-consumption measurement.
