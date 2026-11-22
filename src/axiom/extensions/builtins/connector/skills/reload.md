# connector.reload

Apply changed storage connector configuration without restarting anything
(`axi connector reload [<name>]`, ADR-220 §3). Each changed connector is built
beside its current instance, probed, and swapped only on a passing probe; work in
flight finishes on the instance it started on. Reports every connector's health
(`ready | degraded | failed | unmounted`) and touches the reload stamp so
running services re-read their configuration on next use.

Every other connector family applies its own configuration too: a family's
`connector_status.reload(state_dir, name)` hook (or an `axiom.connector_reload`
entry point from a portfolio distribution) answers for the connectors it has, so
`reload <name>` works for any configured connector. A video source, for instance,
applies its trigger attachments to the capture holding it. A name no family has
is an error. ADR-056 thin wrapper over `skills/reload.py:run`.
