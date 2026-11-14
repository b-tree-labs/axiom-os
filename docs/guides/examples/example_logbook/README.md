# Example log

A small, complete log extension to copy when you build your own. It names no trade: people
open a period, sign checks and notes while it is open, and close it. Every key it uses is one
the loader enforces today, so what you read here is what is in force.

## What is in it

- `axiom-extension.toml` declares the extension and, in one `[[extension.provides]]` block with
  `kind = "logbook"`, names the log's id (`example_log`) and its file.
- `logbook/example_log.toml` is the declaration:
  - `[logbook]` gives the id, version, the name people see ("Example Log") and the floor:
    `attested`, which a person can sign at the command line.
  - `[interval.period]` is a span opened by `OPEN_PERIOD` and closed by `CLOSE_PERIOD`.
  - Four entry types. `OPEN_PERIOD` (a member or lead; no period may be open). `CHECK` (a member
    or lead, inside a period): an observed `reading` with its unit, which only the person signing
    may enter, a `confirmed` checkbox and an optional `note`. `NOTE` (a member or lead, any time):
    one line of text. `CLOSE_PERIOD` (a lead only).
  - `[[obligation]] periodic_check`: while a period is open a `CHECK` is due every 60 minutes, or
    the site's `example_log.checks.interval_minutes`, with a warning 5 minutes before. A miss is
    an alarm to members.

Software may propose an entry; only a person signs it. Roles are `member` and `lead`; a person
gets one at a site with `axi log role grant`.

## Install it

```bash
cp -r docs/guides/examples/example_logbook <project>/.axi/extensions/
axi log validate <project>/.axi/extensions/example_logbook/logbook/example_log.toml
```

Restart the app so the node loads it. Validation should list nothing as "declared, not yet
enforced".

## See it on every surface

- **CLI:** `axi log list` shows the log `example-log` (id `example_log`) with its four types.
- **Signing at the CLI:** grant yourself a role, for example
  `axi log role grant @you:example-site member --site example-site`, then sign an entry with
  `axi log example-log new OPEN_PERIOD --title "Start"`
  and answer `sign`. The command line signs at `attested`: set
  `AXIOM_IDENTITY_POSTURE=attested`.
- **Chat and MCP:** `attest.logbook_list` and `attest.show` (`axiom_attest__logbook_list`,
  `axiom_attest__show`) describe it. Neither signs.
- **API:** `GET /api/v1/attest/logbooks/example_log`.
- **Web:** the Log page lists it.

To remove it, delete the directory and restart. Records already signed stay verifiable.
