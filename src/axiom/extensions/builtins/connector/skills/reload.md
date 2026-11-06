# connector.reload

Apply changed storage connector configuration without restarting anything
(`axi connector reload [<name>]`, ADR-220 §3). Each changed connector is built
beside its current instance, probed, and swapped only on a passing probe; work in
flight finishes on the instance it started on. Reports every connector's health
(`ready | degraded | failed | unmounted`) and touches the reload stamp so
running services re-read their configuration on next use. ADR-056 thin wrapper
over `skills/reload.py:run`.
