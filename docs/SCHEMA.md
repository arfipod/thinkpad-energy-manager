# SQLite schema

The default database is located at:

```text
~/.local/state/thinkpad-energy-manager/thinkpad-energy-manager.sqlite3
```

## `sessions`

One row per recording session.

Relevant fields:

- `id`
- `name`
- `started_at_wall`
- `started_at_iso`
- `ended_at_wall`
- `ended_at_iso`
- `ended_reason`
- `probable_power_loss`
- `last_heartbeat_wall`
- `last_heartbeat_iso`
- `sample_count`
- `config_json`
- `system_json`

## `samples`

One row per time sample.

Relevant fields:

- `session_id`
- `seq`
- `wall_time`
- `wall_iso`
- `monotonic_time`
- `ac_online`
- `total_energy_now_uwh`
- `total_energy_full_uwh`
- `total_energy_full_design_uwh`
- `total_power_now_uw`
- `total_computed_percent`
- `total_health_percent`
- `active_batteries`
- `sample_duration_ms`
- `db_write_duration_ms`
- `collector_rss_kib`
- `collector_user_cpu_seconds`
- `collector_system_cpu_seconds`
- `loop_delay_ms`
- `system_cpu_percent`
- `system_load_1m`
- `system_memory_total_kib`
- `system_memory_available_kib`
- `system_memory_used_percent`
- `system_disk_read_bytes_per_second`
- `system_disk_write_bytes_per_second`
- `display_brightness_percent`
- `display_brightness_raw`
- `display_brightness_max`
- `wifi_enabled`
- `bluetooth_enabled`
- `created_at_wall`

## `sample_batteries`

One row per battery and sample.

Relevant fields:

- `sample_id`
- `session_id`
- `name`
- `present`
- `status`
- `capacity_percent`
- `computed_percent`
- `health_percent`
- `capacity_level`
- `energy_now_uwh`
- `energy_full_uwh`
- `energy_full_design_uwh`
- `power_now_uw`
- `voltage_now_uv`
- `voltage_min_design_uv`
- `cycle_count`
- `technology`
- `manufacturer`
- `model_name`
- `serial_number`
- `charge_control_start_threshold`
- `charge_control_end_threshold`
- `charge_start_threshold`
- `charge_stop_threshold`
- `charge_behaviour`
- `raw_json`

## `power_supplies`

One row per non-battery power supply and sample.

- `name`
- `type`
- `online`
- `raw_json`

## `events`

Derived events. Collector events are written during sampling. Analyzer findings such as gauge jumps, capacity relearning, and threshold watchdog status changes can also be persisted into this table when requested; their structured fields are stored in `details_json` without requiring a schema migration.

- `event_type`
- `severity`
- `battery_name`
- `message`
- `details_json`

## Units

Energy/power/voltage sysfs paths usually come in micro-units:

- `energy_*`: micro-watt-hour (`uWh`)
- `power_now`: micro-watt (`uW`)
- `voltage_now`: microvolt (`uV`)

ThinkPad Energy Manager preserves these raw units and calculates Wh/W/V views in the CLI/UI when needed.

Additional units and encodings:

- `sample_duration_ms`, `db_write_duration_ms`, and `loop_delay_ms`: milliseconds
- `collector_rss_kib`, `system_memory_total_kib`, `system_memory_available_kib`: KiB
- `collector_user_cpu_seconds`, `collector_system_cpu_seconds`: process CPU seconds
- `system_cpu_percent`, `system_memory_used_percent`, `display_brightness_percent`: percent
- `system_load_1m`: Linux 1-minute load average
- `system_disk_read_bytes_per_second`, `system_disk_write_bytes_per_second`: bytes per second
- `display_brightness_raw`, `display_brightness_max`: raw `/sys/class/backlight` brightness values
- `ac_online`, `present`, `online`, `wifi_enabled`, `bluetooth_enabled`: nullable booleans stored as `1`, `0`, or `NULL`
- `created_at_wall`: wall-clock insertion time for the SQLite row
