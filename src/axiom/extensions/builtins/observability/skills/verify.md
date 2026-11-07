---
name: observe.verify
description: Pre-flight and post-install probes for the observability substrate.
---

# observe.verify

Two phases via `phase=preflight|postinstall`.

- **preflight**: helm + kubectl on PATH, kubectl current-context valid.
- **postinstall**: `/api/public/health` returns 200, optional scratch
  trace round-trip via `LangfuseTraceProvider` when public/secret keys
  are supplied.
