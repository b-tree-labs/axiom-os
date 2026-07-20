# ADR-009: Promote Media to Top-Level Noun, Internalize Database Management

**Status:** Accepted
**Date:** 2026-02-26
**Decision Makers:** Ben

## Context

The `axiom signal` module accumulated two responsibilities that don't belong to it:

1. **`axiom signal db`** — Exposes database migration, clustering, and schema
   management commands directly to end users. The target personas (system
   operators, operations engineering researchers, compliance officers) have no use
   for `axiom signal db migrate` or `axiom signal db stats`. This is a leaky
   abstraction: implementation plumbing surfaced as a user-facing command.

2. **`axiom signal media`** — A media library (recordings, images, documents with
   metadata, vector search, access control) that is useful far beyond signal
   extraction. The Experiment Manager needs photos of samples. System Ops Log
   needs inspection recordings. Compliance needs evidence artifacts. Training
   needs instructional media. Trapping media under sense forces every other
   module to import from sense's internals, creating tight coupling.

**Root cause:** Successive agentic coding sessions indexed deeply on the sense
pipeline and conflated it with the broader platform vision described across the
full set of PRDs. Features that belong at the platform level were implemented
in whatever module happened to be in context.

## Decision

### 1. Promote `media` to a first-class CLI noun

```
axiom media ingest <file>       # Add media to the library
axiom media search <query>      # Semantic + metadata search
axiom media list                # Browse the library
axiom media tag <id> <tags>     # Add metadata tags
axiom media link <id> <entity>  # Associate with experiment, log entry, etc.
axiom media export <id>         # Export for compliance or sharing
```

Media becomes a platform service consumed by Neut Signal, Experiment Manager, Ops Log,
Compliance, and any future module. Neut Signal becomes a *consumer* of
media (indexing recordings for signal extraction), not the *owner* of the media
library.

### 2. Internalize `db` under setup/infra

Database lifecycle commands move out of the user-facing CLI:

- **`axiom config`** handles initial database provisioning as part of first-run
  setup (already has a wizard flow)
- **`axiom infra`** handles migration, schema verification, and health checks
  for administrators
- Direct `axiom signal db` commands are removed from the default `--help` output

## Alternatives Considered

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| **Promote media, internalize db** | Clean separation of concerns, media reusable across modules | Requires refactoring sense imports | Selected |
| **Keep media under sense, add re-exports** | No refactoring needed | Perpetuates coupling, confusing for contributors | Rejected |
| **Create a `axiom data` noun for both** | Groups storage concerns | Conflates media (user-facing) with db (admin plumbing) | Rejected |
| **Do nothing** | Zero effort | New modules will depend on sense internals | Rejected |

## Consequences

### Positive

- Media library is discoverable and usable by all modules without importing sense
- Database management is hidden from non-admin users, reducing CLI noise
- Clear ownership boundaries: sense owns intelligence, media owns storage
- New contributors aren't confused by DBA commands in a signal extraction tool
- Aligns with the system-agnostic plugin architecture (media is generic, sense
  signal types are facility-specific)

### Negative

- Existing code in `tools/pipelines/sense/pgvector_store.py` and
  `tools/pipelines/sense/media_library.py` needs to move to `tools/media/`
- Tests referencing `sense.media` and `sense.db` need updated imports
- Two-phase migration: old paths work during transition, removed later

### Mitigations

- Use Python re-exports during transition (`from tools.media import X` works,
  old `from tools.pipelines.sense.media_library import X` emits deprecation warning)
- Create a migration checklist in the media PRD
- Single PR for the move, with mechanical import updates

## Implementation

```
tools/
  media/                         # NEW — promoted from sense
    __init__.py
    cli.py                       # axiom media subcommands
    library.py                   # Media library (from sense/media_library.py)
    store.py                     # Vector store (from sense/pgvector_store.py)
    models.py                    # MediaItem, MediaCollection dataclasses
  agents/
    sense/
      media_library.py           # DEPRECATED — re-exports from tools.media
      pgvector_store.py          # DEPRECATED — re-exports from tools.media
      cli.py                     # Remove 'db' and 'media' subcommands
  db/
    cli.py                       # Existing — absorbs sense db commands
```

## References

- [Media Module PRD](../prd/media-library-prd.md)
- [Executive PRD — Product Modules](../prd/axiom-executive-prd.md)
- [CLI Design PRD](../prd/axi-cli-prd.md)
- [Agent Architecture Spec](../tech-specs/spec-agent-architecture.md)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
