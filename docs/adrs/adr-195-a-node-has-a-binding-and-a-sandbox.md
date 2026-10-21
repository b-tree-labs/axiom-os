# ADR-195: A node has a binding, and a sandbox is not the data read

**Status:** Proposed (2026-10-18)
**Amends:** [ADR-164](adr-164-a-node-declares-its-functions.md) (a node declares its functions)
**Related:** [ADR-131](adr-131-there-is-always-a-medallion.md) (every install has a medallion), [prd-tenant-data-kit](../prds/prd-tenant-data-kit.md)

## Context

ADR-164 says what a node does. It does not say where a data read runs, or
what an engineer is holding while they build a contribution.

Three installs of one product, drawn as a bottling plant:

- **The plant box** declares `acquire` and `transmit`. It reads the line and
  sends what it read. It does not hold the medallion.
- **The company node** declares `ingest`, `data_platform`, `front_end` and
  `assistant`. It holds the medallion. People sign in here.
- **An engineer's laptop** declares no functions. It has a binding to the
  company node. While a contribution is being built it also has a sandbox.

A data read (`axi data` / the same verbs a product exposes) is not a local
command. It answers from whichever medallion this install is supposed to
read. A kit verb (`kit-up`, `kit-try`, `kit-down`, `kit-check`, `kit-init`)
is the explicit local case.

## Decision

### 1. A data read follows the binding, unless this install holds the medallion

This install holds the medallion when its functions include `data_platform`,
or when a database URL is set for the process (`DP1_RAG_DSN`, `DATABASE_URL`
or `AXIOM_DB_URL`). Then a data read opens that database.

Otherwise, if a binding is current, the read is sent to that node's face.
The binding file is only read. The key stays in the vault. The error names
the site and the face. It does not name a local database.

An install with no binding and no medallion keeps the local platform
default. That is an unbound install, not a silent fallback.

`AXIOM_KIT_DSN` is the sandbox. It must not become the data read.

### 2. The sandbox is what `kit-up` creates

`kit-up` creates a sandbox and `kit-down` drops it. `kit-try` and training
read the sandbox. `kit-up` is a checkout the person meant to create. It is
not what a data read falls back to when the binding cannot be reached.

The database itself may still be called the local medallion in the kit's
own notes. The command that creates it, and the thing a contribution is
tried against, is a sandbox.

### 3. Reference data is not a transform

A contribution may cite named facts that are not a bronze-to-silver or
silver-to-gold transform: a price list, a calibration, a floor plan, the
geometry of a line. That is reference data. It lives in the contribution
as `reference/*.toml`. A number in it has a unit. `kit-check` refuses a
number that does not.

Axiom does not learn the tenant's vocabulary. A fill-head height is one
tenant's fact. The platform only checks that the number has a unit.

### 4. A snapshot is kept; materialize loads it into a sandbox

A snapshot is a point-in-time subset pulled from a bound node (which site,
which channels, which window). It is kept after the experiment. It is not a
cache, a clone, or a sample.

Materialize loads a snapshot into a sandbox. `kit-down` drops the sandbox
and leaves the snapshot. This ADR names both. It does not build them.

### 5. Declaring functions on an install that is already running is a later release

An install that does not declare functions is unconfined and shows every
command. Writing a function list onto a machine that is already installed
hides commands on the release that machine runs today. Existing bindings
and vault keys keep working. A function list is written when that install
takes a release that knows this ADR.

## Consequences

- A laptop bound to the company node reads the company medallion. It does
  not open a database on the laptop unless the laptop holds the medallion.
- `kit-up` never becomes that read.
- A contribution can cite reference data, and a missing unit fails the
  same gate as a unitless channel.
- Snapshot and materialize have names and a place in the kit PRD. Building
  them is separate work.
