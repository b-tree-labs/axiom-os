# ADR-174: Postgres is the runtime database, and SQLite is for tests

**Status:** Proposed (2026-10-06); implemented in two stacked changes the same day (see Implementation)
**Related:** [ADR-170](adr-170-a-release-is-proven-in-an-idle-slot-before-traffic-moves.md) (the `micro` profile provisions Postgres),
ADR-052 (schema per extension), ADR-049 (cross-extension reads ride the data platform)

## Context

Most of Axiom's runtime state is already in Postgres: schedule, fleet, receipts, attest, the notifications inbox,
the web app and the pgvector store use `session_for(...)`, and the pgvector store states "no SQLite fallback".
Beside that, SQLite has grown into runtime code. Read from the code, not the documents:

- **A fallback store for retrieval** (`SQLiteRAGStore`), advertised as the "zero-infra" first-run path, with 27
  test files and a parity suite to keep it equivalent to the Postgres store.
- **The only implementation** of the artifact ledger (`SQLiteBackend`, 11 production construction sites, 56 test
  files; a Postgres backend is a "future" note), of the memory concept graph, and of the memory recall corpus.
- **A fallback** for the chat conversation store, and local-only stores for background tasks and a classroom index.
- A silent downgrade: the classroom index imports the SQLite vector extension in a try block and skips vector
  search if it is absent, so retrieval quality changes with no message. In the retrieval measurements recorded
  for this project, keyword-only search recalled 30% of natural-language questions and vector search 100%.

The direction for the product is Postgres on every node, including a laptop: the rest of the system leans on it
heavily. A second store is not free. It means two implementations to keep equivalent, features that differ (full-text
and vector search work differently), concurrent writers (the sync service, the scheduler and chat run at once, and
SQLite serialises writers), and data created on a laptop that later has to migrate.

## Decision

1. **Postgres is the only runtime relational store of Axiom state.** SQLite is permitted in tests, and for
   reading other tools' SQLite files (the absorb readers and the Open WebUI projector), which is reading, not
   storing Axiom state.
2. **There is no setup-store exception.** First-run setup provisions Postgres: the `micro` profile of ADR-170 on a
   machine with a container runtime. For a machine without one, evaluate an embedded, pip-installable Postgres with
   pgvector (a candidate is `pgserver`; its license, maintenance and platform coverage must be verified against the
   dependency rule before adoption) before falling back to a Postgres the user supplies.
3. **Replace the SQLite-only components,** in this order: a Postgres `ArtifactRegistry` backend; a Postgres concept
   graph; the memory recall corpus on the Postgres retrieval store; then remove the conversation-store fallback and
   the retrieval SQLite store. Background tasks and the classroom index follow.
4. **Data created under SQLite is migrated, not stranded.** The session reconcile tool is the pattern; equivalent
   tools carry the retrieval and recall databases into Postgres.
5. **A guard prevents regrowth.** A test fails on `import sqlite3` or a `sqlite:` URL in non-test source outside a
   named allowlist (the readers in point 1).

## Implementation

Done in two changes, both verified against a real Postgres as well as the test seam:

1. **Memory layer.** The artifact ledger, the concept graph and the recall corpus now run on Postgres: a
   `memory` schema (one scope per location, so one database serves every location), a Postgres
   `ArtifactBackend` and `ConceptGraph`, and the recall corpus as a corpus in the existing retrieval store. Every
   production construction site uses it; throwaway evaluation and benchmark ledgers are in-memory.
2. **The rest.** The local conversation fallback is removed; the task store moved to a `tasks` schema (with a
   `node_id`, because a pid and a log path are facts about one machine); the retrieval store factory refuses
   `sqlite://` unless a test opts in; the RAG health report reads the Postgres store read-only; and the classroom
   search index moved to a `classroom` schema with Postgres full-text search. A guard test fails on new runtime
   SQLite (`import sqlite3`, `sqlite:` URLs, SQLite store construction) outside a named, justified allowlist; its
   pending list is empty.

Two facts found on the way are worth keeping. A first migration put its tables in `public`, and every behaviour
test still passed because the session's search path found them there, so each migration now has a catalog test
that asserts its tables are in its own schema. And the classroom index's vector table was written but never read,
and no caller ever passed an embedder, so that path was removed instead of ported.

## Options considered

- **Keep SQLite as the first-run fallback.** The honest case for it: a stranger can try the product with no
  install, there is no daemon, one file is simple, and tests run fast. Rejected because the friction it removes is
  better removed another way (a provisioned or embedded Postgres), while its costs persist: two implementations,
  silent feature gaps, writer contention, and a migration for everyone who starts on it.
- **Keep SQLite only for the first-run store, with a reconcile into Postgres later** (proposed, then withdrawn by the
  owner). It keeps a second store alive for exactly the users who then have to migrate.
- **Postgres everywhere including tests.** Cleaner, but tests need to stay fast and hermetic; they keep SQLite
  through the existing provider seams.

## Consequences

- One store to reason about, test and size; retrieval quality no longer depends on which store a user started on.
- The Postgres ledger and concept graph that were prerequisites now exist, so nothing is blocked or exempt.
- A first-run on a machine with no container runtime depends on the embedded-Postgres evaluation; until then it
  needs a Postgres the user provides.
- The wizard's "zero-infra" advice, the SQLite branches of the store factory and health check, and the planes
  status text are removed.
- **A student's classroom index no longer works with no database.** It was a portable file on the student's own
  machine, which suited an offline laptop; it is now a scope in Postgres, which a laptop gets from the `micro`
  profile of ADR-170. This is the one product behaviour the decision costs, accepted by the owner ("put everything
  on Postgres"). If an offline student path matters again, it needs its own decision, not a quiet SQLite exception.
- **Existing local SQLite data is not migrated.** Anyone with a `rag.db`, `recall.db`, `chat.db`, `tasks.db` or a
  classroom `index.db` keeps those files but they are no longer read. A one-shot importer per store is follow-up work;
  the conversation reconcile tool is the pattern.
- Open: the embedded-Postgres candidate for machines without a container runtime is still to be evaluated against the
  dependency rule.
