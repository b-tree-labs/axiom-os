# PRD: Document Mirror — one document, many surfaces, explicit directionality

**Status:** Draft, P0 implemented
**Owner:** platform
**Related:** axiom issue #848 (the incident-shaped precursor), `mirror.py`, `providers/sharepoint.py` (GraphEditorEndpoint)

## The feature, as requested

The user edits where they please — a web editor reached from a comment link,
a local editor, tomorrow something else — and the platform detects where
editing is live. The user wants the same document mirrored in their local
filesystem (possibly inside a git repository). The platform keeps careful
watch over the coordination between the published and local versions, and
when the mirror is in a git repository it ensures local git state is
properly annotated for git operations. A single-file Dropbox, except the
publication endpoint can be any remote editor — and there may be **n**
publication endpoints, all downstream of whichever surface holds the active
editing session.

## Why it exists

One real week produced the full failure catalog on one document: a remote
web editor and a local agent editing the same file, stale editor sessions
flushing old buffers on close and on machine wake, the sync client wedging
in one direction so local writes silently never arrived, and whoever wrote
last winning without record. Two hundred twenty-five versions later, the
lesson is not "be careful" — it is that directionality must be a property
of the system, not of vigilance.

## Doctrine

0. **Directionality follows the human.** The canonical surface is wherever
   the user is editing — web today, the local file an hour from now — and
   the agent's job is to detect that, adapt, and keep sync correct no
   matter what. Every rule below is an instance of this one.
1. **One canonical surface at a time.** P0 ships `web-canonical` (the
   human was editing remotely; the local mirror follows). The local file
   is itself a first-class editing surface: a local edit — the user's or
   the agent's — pushes with the version it expects, never blind.
2. **Writes carry the version they expect.** The endpoint write takes an
   expected version (Graph: `If-Match` with the item eTag) and fails on a
   foreign one. A failed push re-reads and re-decides; concurrent human
   work is never overwritten, in either direction.
3. **A stale flush is not new work — and repair happens once.** A remote
   save whose content hashes to an old base is a stale editor session:
   repaired (last good text pushed back, hash-verified) and attributed. If
   the same text returns after a repair, it is either a human insisting on
   a revert or a zombie repeating — indistinguishable on content — so the
   engine suspends repair, follows the remote, preserves the good text as
   a conflict copy, and says so. Never a war, never a silent follow.
3a. **Bytes are the truth.** The mirror is read and written byte-faithfully
   (no newline translation), so a normalizing remote (a web editor flipping
   line endings) converges instead of oscillating as a phantom local edit.
4. **Both changed: block, preserve, resolve (ADR-112 §D3).** The canonical
   side lands in the mirror (kept readable — never conflict markers, which
   the watcher would push back and corrupt the remote); the local edit is
   preserved beside it in a `*.conflict` sidecar; and **sync pauses until a
   human resolves it**. `mirror resolve <name> --theirs|--ours|--merged`
   picks a side, git-style; a remote that moved on re-blocks rather than
   being overwritten. Nothing is guessed, nothing is lost, nothing rolls
   silently past a conflict. See `docs/mirror-conflicts.md`.
5. **Git is annotated, never surprised.** With `git_annotate` on and the
   mirror inside a repository, each sync event commits the mirror file with
   provenance trailers (`Synced-From`, `Endpoint-Version`). Only the mirror
   file is staged; the user's other work is untouched.

## P0 (this change)

- `mirror.py`: `MirrorEngine` (reconcile, watch_once with stale-flush
  repair, git annotation), `RemoteEditorEndpoint` protocol, `RemoteDoc`,
  `VersionConflict`, `SyncReport`.
- `GraphEditorEndpoint`: any shared document as a versioned endpoint over
  Microsoft Graph, reusing the extension's MSAL device-code flow; injected
  transport, fully offline-testable.
- Skills per ADR-056: `press.mirror_add`, `press.mirror_sync`,
  `press.mirror_status`, `press.mirror_resolve`; CLI verbs are thin
  wrappers (`pub mirror add|sync|status|resolve`).
- Tests: engine suite (pull, push, race, block-on-conflict + resolve,
  stale-flush repair,
  genuine-edit non-repair, git trailers, no-repo no-commit) and endpoint
  suite (If-Match on write, 412 → VersionConflict), no network anywhere.

## P1 — follow the human

- Detect where the human is editing from the endpoint version feed (author
  application metadata distinguishes a web editor's saves from the agent's
  API writes) and from local file events (the agent knows its own writes
  and excludes them); canonical floats to the surface of the most recent
  human-authored edit, with an explicit user pin
  (`pub mirror follow <name> --active <surface>`) always winning.
- `pub mirror watch` as a daemon on the extension's ServiceManager pattern
  (the `watcher.py` precedent), running `watch_once` on an interval and
  raising stale-flush repairs through the platform alerting path with the
  writing application named.

## P2 — n downstream endpoints

- A mirror grows a list of downstream endpoints; whichever surface is
  active, every other endpoint plus the local mirror follows after a
  debounce. Per-endpoint transforms ride the existing converter providers
  (markdown to docx and back), so a rendered endpoint can follow a
  markdown-canonical session and vice versa.

## Out of scope

- Multi-document folders (this is deliberately single-file; a folder is a
  set of mirrors).
- Merge heuristics beyond disjoint-or-hold (a smarter three-way can arrive
  behind the same report shape).
- Real-time co-editing; the unit here is the save, not the keystroke.
