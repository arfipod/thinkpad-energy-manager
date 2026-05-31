# Repository structure

```text
thinkpad-energy-manager/
|-- src/battery_auditor/
|  |-- cli.py                    # Main CLI
|  |-- config.py                 # TOML configuration and defaults
|  |-- core/
|  |  |-- analyzer.py            # Summary and export
|  |  |-- collector.py           # Recording loop
|  |  |-- database.py            # SQLite WAL and schema
|  |  |-- events.py              # Event detection
|  |  |-- models.py              # Snapshot/event dataclasses
|  |  |-- sysfs.py               # /sys/class/power_supply reader
|  |  `-- tlp.py                 # Manual TLP wrapper
|  `-- ui/
|     `-- main.py                # Qt/PySide6 app
|
|-- packaging/
|  |-- desktop/                  # .desktop file for the UI
|  `-- systemd/user/             # User systemd services
|
|-- scripts/
|  |-- install-user-service.sh
|  |-- uninstall-user-service.sh
|  |-- record-blackbox.sh
|  `-- run-dev.sh
|
|-- examples/config.toml
|-- docs/
|-- tests/
|-- pyproject.toml
|-- README.md
`-- LICENSE
```
