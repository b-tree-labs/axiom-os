<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenFGA authorization model for GUARD

GUARD's fine-grained authorization decisions are backed by **OpenFGA**
(relationship-based access control) via `OpenFgaSubstrate` (`../openfga.py`),
registered as the `SubstrateSource` in the policy combiner (ADR-083).

## The substrate ↔ model contract

`OpenFgaSubstrate.check(envelope)` maps an `ActionEnvelope` to an OpenFGA check
and translates the result into GUARD's three-valued opinion:

| OpenFGA result | Substrate opinion | Combiner effect |
|---|---|---|
| `blocked` relation holds | `DENY` | deny-overrides — wins over any grant |
| permit relation holds | `ALLOW` | authoritative — short-circuits the propose default |
| neither | `ABSTAIN` | defer to rules / graduation / floor |

Absence of a grant is **ABSTAIN, not DENY**: an action OpenFGA has not modelled
falls through to the rule engine and the deterministic capability floor rather
than being silently denied. Explicit denial is the separate `blocked` relation.

An OpenFGA outage returns `on_error` (default `ABSTAIN`, so authz keeps working
off the rule engine + floor; set it to `DENY` to fail closed).

## Mapping envelopes to tuples

`default_mapper` is the transparent dev convention:

- **user** — `subject.fga_user` when the envelope carries `SubjectContext`, else
  `user:<actor handle>`.
- **object** — `<scheme>:<identifier>` from the resource (`slack://team-rsc/#alerts`
  → `slack:team-rsc/#alerts`).
- **permit relation** — the dotted intent with `.`→`_` (`notification.send` →
  `notification_send`).
- **deny relation** — `blocked`.
- **contextual tuples** — `subject.contextual_tuples`, evaluated at check time.

OpenFGA models are *closed* (finite types + relations) while Axiom intents and
resource schemes are *open*. A production deployment therefore registers its own
`TupleMapper` that collapses its intents onto the model's relations (e.g. reads →
`viewer`, writes → `editor`, admin → `owner`) and its schemes onto the model's
types. `default_mapper` suits a model that literally defines the underscored
intent relations.

## `starter.fga`

A template model exercising the three patterns GUARD relies on: an explicit
`blocked` deny relation, `group#member` for team grants + contextual tuples, and
a hierarchical `owner → editor → viewer` chain. Load it with `fga model write`,
or adapt it and keep your `TupleMapper` in step.

## Deferred (infra-gated)

Not in this cut — both need an OpenFGA-on-Postgres server the build sandbox lacks:

- the production `FgaCheckClient` adapter over `openfga-sdk` (a thin wrapper — the
  substrate logic is already complete and unit-tested against the client seam);
- the recall + p99-latency **benchmark gate** that qualifies OpenFGA against the
  in-process rule engine before it is switched on for a tenant.


## Wiring the live server (`openfga_http.py`)

`OpenFgaHttpClient` is the adapter the module docstring deferred: one object that
satisfies GUARD's `FgaCheckClient` (**check**) *and* the directory reconciler's
`TupleStore` (**read** + **write**), over OpenFGA's plain HTTP API.

```sh
# a throwaway server
docker run --rm -p 8080:8080 openfga/openfga run

# bootstrap once (from Python or the OpenFGA CLI)
python - <<'EOF'
from axiom.extensions.builtins.authz.openfga_http import *
t = HttpxTransport("http://127.0.0.1:8080")
sid = create_store(t, "axiom")
c = OpenFgaHttpClient(transport=t, store_id=sid)
print(sid, c.write_authorization_model(starter_type_definitions()))
EOF

# then point the node at it
export AXIOM_OPENFGA_URL=http://127.0.0.1:8080
export AXIOM_OPENFGA_STORE_ID=<sid>
export AXIOM_OPENFGA_ON_ERROR=abstain      # or deny for a strict node
```

`setup_extension()` calls `maybe_register_substrate(DecideContext)` — unset means
`NullSubstrate` (unchanged behaviour); a URL without a store id raises rather than
silently running without the substrate. The same keys work as `authz.openfga.*`
in `axi settings`. Writes go in chunks of ≤100 with deletes first, so an
interrupted batch can only under-grant. Run the live suite with
`AXIOM_OPENFGA_LIVE_URL=http://127.0.0.1:8080 pytest …/tests/test_openfga_live.py`.
