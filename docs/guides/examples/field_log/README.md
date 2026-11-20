# The field log example

A second worked example for [Write a log extension](../../write-a-logbook-extension.md), beside
the [example log](../example_logbook/README.md). It keeps a season of work: an owner opens and
closes the season, a performer signs each action with the product, rate and area they observed,
an advisor co-signs each action, anyone on the season records observations, and one observation
is due every week while the season is open.

The words are neutral on purpose. A farm, a lab or a crew renames the season, the entry types
and the roles to its own; a version in one trade's words belongs in that trade's own repository.

It also declares an export form, `certifier_packet`, filled from `logbook/templates/`. One key is
declared although the loader does not enforce it yet: `partition` in `[logbook]`. Check the file
and see it listed:

```sh
axi log validate docs/guides/examples/field_log/logbook/field_log.toml
```

To run it, copy this directory to `<project>/.axi/extensions/field_log` and restart the app.
`axi log list` then shows the log `field-log`.
