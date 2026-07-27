# Federation Lifecycle Test Harness

Multi-node federation test harness for Axiom. Spins up 3-20 Axiom nodes in
isolated Docker containers on a developer laptop, joins them into
federations of various shapes, drives lifecycle operations through the
real `axi` CLI, and asserts outcomes.

This is the scaffolding for the scenario matrix in
`docs/prds/prd-federation.md §17` (16 install/upgrade/attack scenarios).
The prototype only implements the happy-path 3-node scenario; additional
scenarios are added as separate files under this directory.

## Requirements

- Docker Desktop (or Docker Engine + compose v2 plugin) running locally
- Python 3.11+ with the repo's virtualenv activated (`source .venv/bin/activate`)
- Nothing else — no Kubernetes, no k3d, no Ansible

If Docker isn't available the suite skips cleanly; it never fails for
infrastructure-absence reasons.

## Run it

From the repo root:

```bash
source ../.venv/bin/activate   # per MEMORY.md
pytest tests/federation_lifecycle -v -m federation_lifecycle
```

Or the single scenario:

```bash
pytest tests/federation_lifecycle/test_happy_path_3_node.py -v
```

The suite is **opt-in**: the default `addopts` in `pyproject.toml` excludes
`-m federation_lifecycle`, so a plain `pytest` run will not invoke it.
You must pass `-m federation_lifecycle` explicitly (or set
`PYTEST_ADDOPTS="-m federation_lifecycle"`).

First run builds the node image, which takes ~60-90s. Subsequent runs reuse
the cached image and the scenario itself takes ~15s.

## Compose topology

```
 ┌──────────────────── fednet (bridge) ────────────────────┐
 │                                                          │
 │    ┌──────┐          ┌───────┐          ┌───────┐       │
 │    │ hub  │◄────────►│ leaf1 │          │ leaf2 │       │
 │    │ :22  │          │ :22   │          │ :22   │       │
 │    └──────┘          └───────┘          └───────┘       │
 │     22201              22202              22203          │ (host ports)
 └──────────────────────────────────────────────────────────┘
```

- Each node runs `sshd` (port 22) so `axi nodes add <name> axiom@<host>`
  can perform the identity-binding SSH fetch. Ports 8765/8766 are
  exposed for future A2A / A2A-health wiring.
- All three nodes share a test keypair mounted read-only at
  `/harness/keys/` — generated fresh by the harness on `start()`, never
  baked into the image. This is solely for SSH between test nodes;
  Axiom's own Ed25519 identity keypairs are generated *inside* each
  container by `axi federation init`, which the tests drive explicitly.
- Hostnames (`hub`, `leaf1`, `leaf2`) are resolvable via compose's built-in
  DNS. No host `/etc/hosts` tweaks needed.

## Harness API

`tests/federation_lifecycle/harness.py` exposes a single class,
`FederationHarness`, with:

| method                                  | purpose                                                  |
|----------------------------------------|----------------------------------------------------------|
| `start()`                              | Bring up the compose stack + seed SSH keys, wait for sshd |
| `stop()`                               | Stop containers but keep volumes                         |
| `teardown()`                           | `docker compose down -v --remove-orphans` (always safe)  |
| `exec(node, cmd)`                      | Run `cmd` inside `node` as the `axiom` user              |
| `exec_json(node, cmd)`                 | Same, but parse stdout as JSON                           |
| `add_peer(from_node, to_node)`         | `axi nodes add <to_node> axiom@<to_node>` on `from_node` |
| `assert_federated(node_a, node_b)`     | Assert identity-bound, verified, fingerprints match     |

Tests use the `harness` fixture (see `conftest.py`) which yields a
configured instance and guarantees teardown.

## Adding a scenario

1. Create `tests/federation_lifecycle/test_<scenario>.py`.
2. Import the fixture — don't instantiate `FederationHarness` directly:
   ```python
   def test_my_scenario(harness):
       harness.start()
       ...
   ```
3. For scenarios that need a different topology (hub-and-spoke with more
   leaves, hierarchical, cross-root), override `compose_file` and/or
   `nodes` when building the harness — either by parametrizing the
   fixture or by calling `FederationHarness(...)` directly in the test
   and using it as a context manager.
4. Every test file is auto-marked `federation_lifecycle` by `conftest.py`.

## What's intentionally out of scope (prototype)

- 10k–100k node simulation (different backend: process-per-node or
  testcontainers — same harness interface)
- Sybil/eclipse/replay attacks (future scenarios)
- Cross-root bridges
- Key-rotation drills (the CLI supports `--confirm-key-change`; the
  scenario just needs writing)

The harness *shape* — named nodes, lifecycle primitives, identity-bound
assertions — is chosen so those future scenarios slot in without needing
to rewrite the harness.

## Troubleshooting

- **"docker compose v2 plugin not installed"** — upgrade Docker Desktop or
  install `docker-compose-plugin`.
- **Build is slow** — the Dockerfile copies `src/` into the image. Once
  identity binding is stable we can switch to installing a pinned wheel
  instead; for now dev iteration is fine.
- **Port 22201/22202/22203 in use** — edit `docker-compose.yml` or remove
  the host port mapping (only used for ad-hoc debugging; tests exec
  through `docker compose exec`, not over the host).
- **Lingering containers after a crash** — `docker compose -p axifed_<test>
  down -v` or just `docker ps | grep axi-fed-` and remove.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
