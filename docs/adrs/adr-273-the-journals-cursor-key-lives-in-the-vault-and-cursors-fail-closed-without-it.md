# ADR-273: The journal's cursor key lives in the vault, and cursors fail closed without it

**Status:** Accepted (2026-11-21)
**Supersedes in part:** ADR-271, Consequences, the bullet on where the node key is kept
**Related:** ADR-271 (a reader of part of a site never sees the journal's node-wide number), ADR-218
(the one vault component), issue #2224

## Context

ADR-271 makes the journal's cursors and stream ids opaque for a reader of part of a site. The tokens
are made with a node key. As first built, that key was a random value in a file in the node's state
directory, or an environment variable. Secrets live in the vault (Core Invariants). A key in a
plaintext file can be read by anything that can read the file, survives in backups, and cannot be
rotated or audited.

## Decision

1. **The key lives in the vault.** It is `journal.cursor-key` in KEEP's store, the store
   `attest.presence` keeps its location secrets in. It is created there on first use and read from
   there. It is never written to the state directory, a log or a response. Tests replace the store
   through a seam; there is no environment variable.
2. **Without the vault, cursors fail closed.** A partial reader still gets the partial form, but
   rows carry no key and no positions are offered. A token is refused with 503
   `cursor_unavailable`, and a partial reader's raw number is refused with 400 `bad_cursor`. A
   whole-site reader's raw numbers need no key, so their responses are unchanged.
3. **Two processes may create the key at once.** A token that does not decode is checked once more
   against the key read fresh from the vault before it is refused.

## Consequences

- A node whose vault is sealed serves partial readers a Timeline they can read but not page or
  resume until the vault answers.
- Rotating the key (`axi vault rotate`) makes old cursors stop decoding. The client starts again
  from the newest rows.
- Nodes that ran the first build have a `journal/cursor.key` file in their state directory. Nothing
  reads it any more. It can be deleted; it holds only the key for cursors that no longer decode.
