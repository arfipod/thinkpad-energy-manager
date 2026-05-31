# Security and permissions

## Reading

The collector only needs to read `/sys/class/power_supply`. It normally does not require root.

## Writing

The SQLite database is stored in the user's state directory:

```text
~/.local/state/thinkpad-energy-manager/
```

## TLP

TLP actions may require `sudo`:

- `tlp-stat`
- `tlp setcharge`
- `tlp recalibrate`

The UI and CLI only run these commands when the user explicitly requests them.

## systemd

The included units are user services, not system services. They do not run as root.
