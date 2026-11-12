# connector.status

`axi connector status [<name>]`. ADR-056 thin wrapper over `skills/status.py:run`.

Two halves:

- **configured**: every connector declared on this node, from every family that
  reports them, with ADR-220 §3 health (`ready | degraded | failed | unmounted`)
  and a one-line detail. A family is a status provider, `rows(state_dir)`: a
  builtin extension's `connector_status` module (found by convention), or an
  `axiom.connector_status` entry point from a portfolio distribution (ADR-023-A1).
  `connector/configured.py` names no kind (ADR-220 §1); a provider that fails is
  listed under `configured_problems` and hides nothing else.
- **connectors**: the last delivery outcome per connector from the in-process
  status store, with the `reconnect_pending` subset surfaced prominently. A fresh
  process finds it empty.

With `<name>`, the configured row for that connector, and its last outcome if any.
