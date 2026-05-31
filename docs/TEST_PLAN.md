# Test plan

## Unit tests

```bash
pytest
```

Cases:

- sysfs reads from fixture;
- Wh-based percentage calculation;
- SQLite insert;
- open-session recovery;
- export.

## Smoke test on real hardware

```bash
thinkpad-energy-manager once
thinkpad-energy-manager collect --mode diagnostic --duration 10 --name smoke-test
thinkpad-energy-manager sessions
thinkpad-energy-manager analyze
thinkpad-energy-manager export --format csv --out smoke.csv
```

## Controlled black-box test

1. Charge the machine to a safe level.
2. Run:

```bash
thinkpad-energy-manager collect --mode blackbox --name blackbox-smoke --duration 60
```

3. Confirm:

```bash
thinkpad-energy-manager analyze
```

## Recovery test

Simulate a collector interruption:

```bash
thinkpad-energy-manager collect --mode diagnostic --name recover-test
# In another terminal:
pkill -f 'battery_auditor.cli.*collect'
thinkpad-energy-manager recover
thinkpad-energy-manager analyze
```

`PROBABLE_POWER_LOSS` or an interrupted session should appear.
