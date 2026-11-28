# Spec: Comments

**Owner:** Platform (Axiom service, appkit component) • **Status:** Draft (proposal, 2026-10-31) • **Last updated:** 2026-10-31
**PRD:** [prd-comments.md](../prds/prd-comments.md)
**Related:** [prd-chart-surface.md](../prds/prd-chart-surface.md) (first host), [spec-scientific-displays.md](spec-scientific-displays.md) (section 9c.1, assistant context), [prd-axiom-notifications.md](../prds/prd-axiom-notifications.md), [ADR-128](../adrs/adr-128-medallion-tier-boundaries.md), [ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md), [prd-media-library.md](../prds/prd-media-library.md), [spec-library](../../src/axiom/extensions/builtins/library/docs/spec-library.md)

One comment system for every artifact the platform shows. This spec is the contract:
the data model, the selector registry, the appkit component contract, the service
and its API, the lifecycle, and the conformance tests. Requirement IDs (CM-nn) are
in the PRD.

## 1. Layout

| Layer | Owns |
|---|---|
| Axiom extension `comments` (a small generic extension; own tables in the app database, per ADR-052) | the model, permissions, lifecycle, audit, events, API, skills |
| Selector registry (Axiom side) | per-kind validation and wording of selectors |
| appkit `CommentsDrawer` and `useCommentSelection` | the panel and the selection-to-comment gesture, for every host |
| Selector adapters (appkit side) | per-kind selection capture, highlight and re-anchor, supplied by the host |
| Shared platform services | notifications, audit, object store, authorization gate, retention |

Hosts (the chart frame, a document view, a library file view, a video player, a
logbook view, a chat message, a notebook) do not implement comments. They supply
an artifact reference and an adapter.

## 2. Data model

Tables in the extension's own schema in the app database. All ids are opaque strings.

```
comment_object        -- the thing commented on (a thin reference; the artifact lives elsewhere)
  id, artifact_kind, artifact_id, site, owner_account, state ('live'|'trashed'),
  retention_class, trashed_at, purge_after, created_at

comment_thread
  id, object_id -> comment_object, selector (json), selector_version,
  anchor_state ('attached'|'detached'), anchor_text (text, the words shown),
  resolved (bool), resolved_by, resolved_at, created_at

comment
  id, thread_id -> comment_thread, author (principal), text, mentions (json),
  hidden (bool), hidden_by, hidden_at, copied_from (id|null),
  created_at, edited_at

comment_edit          -- history of edits
  id, comment_id, previous_text, edited_by, edited_at

comment_attachment
  id, comment_id, object_store_ref, name, size, content_type
```

`comment_object` references the artifact by id and kind; the comment tables never
hold the artifact's content. Deleting an object cascades from `comment_object` to
its threads, comments, edits and attachments by foreign key (CM-40); the trash
state is a flag on `comment_object`, so trashing and restoring move the whole
tree at once (CM-41, CM-43).

### 2.1 Selector

A selector is JSON with a `kind` and the fields that adapter needs, plus a
`revision` where the object has one (a data revision, a document version).

| Artifact kind | Selector fields |
|---|---|
| text | `quote`, `prefix`, `suffix`, `start`, `end` |
| heading | `heading_id` |
| table | `cell: {row_key, column}`, or `rows: {from_key, to_key}`, or `column`, or `whole` |
| chart | `whole`, or `range: {from, to}`, or `point: {t, series}`, or `series`, or `box: {x0, x1, y0, y1}` |
| image | `region: {x, y, w, h}` in image coordinates |
| video, audio | `time: {from, to}` in seconds (`to` optional for a moment) |
| model | `version`, or `run` |
| logbook entry | `entry_id` |
| page | `view_id` (a dashboard or saved view) |
| group | `group_id` |
| chat message | `message_id`, optionally `quote` |
| notebook | `cell_id`, optionally `quote` |
| library file | `whole` |

### 2.2 W3C Web Annotation mapping (CM-01, CM-62)

A thread exports as an Annotation: `target.source` = the artifact URI; `target.selector`
= the matching W3C selector where one exists (`TextQuoteSelector` and
`TextPositionSelector` for text, `FragmentSelector` with a media-fragment for image
regions and time ranges, `RangeSelector` where needed) and a platform selector
(`axiom:selector`) carrying the full JSON where W3C has no equivalent (a chart
point, a table row range). Each comment is a `TextualBody` with `purpose`
"commenting" and replies are chained with `partOf` / `inReplyTo`. Import reverses the
mapping and attributes authors as imported.

### 2.3 Additions: state, audio, references

```
comment_thread   + state ('open'|'resolved'|'archived'|'detached'), archived_at, last_activity_at
comment          + state ('posted'|'edited'|'hidden'|'removed'), idempotency_key (unique per author),
                   hidden_reason, removed_at
comment_audio    -- a kept voice note (CM-103)
  id, comment_id, object_store_ref, duration_ms, content_type, language, transcript_source ('browser'|'server')
comment_reference -- structured links (CM-121)
  id, comment_id, ref_kind ('person'|'agent'|'group'|'artifact'), target_id, target_kind (for artifacts),
  address (the @name[:context] form, for people, agents, groups), label_at_post (text shown when removed)
comment_transition -- audit of every move (CM-112)
  id, machine ('composer'|'thread'|'comment'|'object'), subject_id, event, from_state, to_state, actor, at
```

`comment_object.state` is the object machine's state. Composer state is held by
the client and the draft endpoint; only `posting`, `posted` and `failed` are server
visible, through the idempotency key. `comment_audio` rows inherit the comment's
permissions and are removed with it; the audio bytes live in the object store.

## 3. Selector registry (CM-02, CM-03)

### 3.1 Axiom side

```python
class SelectorAdapter(Protocol):
    kind: str                                      # artifact kind this adapter serves
    def validate(self, selector: dict) -> None: ...         # raise on a malformed selector
    def describe(self, selector: dict) -> str: ...          # the words: "rows 41 to 58"
    def order_key(self, selector: dict) -> tuple: ...       # position in the object, for ordering
    def resolve(self, artifact_ref, selector: dict) -> Resolution: ...
        # Resolution: attached(place) | moved(place, selector') | detached(reason)
```

Registered through an entry point; the service rejects a thread whose kind has no
adapter. The service holds no kind-specific code.

### 3.2 appkit side

```ts
interface SelectionAdapter<S = unknown> {
  kind: string;
  /** turn the host's current selection into a selector, or null if there is none */
  capture(host: HostHandle): S | null;
  /** where to put the floating Comment button for the current selection */
  anchorRect(host: HostHandle, selection: S): DOMRect | null;
  /** tint, pin and focus a selector in the host */
  highlight(host: HostHandle, selector: S, state: "rest" | "hover" | "active"): void;
  clear(host: HostHandle): void;
  /** bring a selector into view (scroll, seek, pan) */
  reveal(host: HostHandle, selector: S): void;
  /** words for the thread header; mirrors the server's describe() */
  describe(selector: S): string;
}
```

The host declares the selection kinds it can produce (for a chart, from the engine's
capability set), and the component offers only those (CM-27).

## 4. Service and API

Permissions (CM-10). Every call passes the artifact reference to the authorization
gate with the caller's principal; the gate answers read, comment or edit on the
artifact, and the service maps these to the comment operations:

| Operation | Needs on the artifact |
|---|---|
| list, read, search, export | read |
| create thread, reply, edit own, resolve, reopen | comment |
| hide | moderator on the artifact's site or class |
| delete object (cascade), restore | the artifact's own delete and restore right; the comment service is called, never the other way round |
| purge | purge right, and the retention floor passed |

An artifact that is signed, attested or locked refuses delete and edit; comments
still attach (CM-14, CM-45).

### 4.1 REST

```
POST   /api/v1/comments/threads                {artifact:{kind,id}, selector, text, mentions}
GET    /api/v1/comments/threads?artifact_kind=&artifact_id=&filter=all|selection|open|resolved
POST   /api/v1/comments/threads/{id}/replies   {text, mentions}
PATCH  /api/v1/comments/{id}                   {text}                 # author only
POST   /api/v1/comments/{id}/hide              {reason}              # moderator
POST   /api/v1/comments/threads/{id}/resolve   |  /reopen
GET    /api/v1/comments/export?artifact_kind=&artifact_id=            # Web Annotation
POST   /api/v1/comments/import
POST   /api/v1/comments/objects/{kind}/{id}/trash | /restore | /purge
```

Delete and restore of an artifact call the comment service through the artifact
owner's lifecycle hook (4.3); a client does not trash comments directly except
through these object routes, which the artifact owner also uses.

### 4.2 Skills, MCP and CLI (CM-81, CM-82)

One skill per operation (`comments.create`, `comments.reply`, `comments.list`,
`comments.resolve`, `comments.hide`, `comments.export`, `comments.trash`,
`comments.restore`, `comments.purge`), declared once and exposed as MCP tools and
as `axi comments <verb>`. The REST routes call the same skills.

### 4.3 Lifecycle hook

An artifact owner (library, charts, chat, logbook, models, documents) registers a
`CommentLifecycle` with the service:

```python
class CommentLifecycle(Protocol):
    def on_trash(self, ref): ...      # mark comment_object trashed; hide from search and context
    def on_restore(self, ref): ...
    def on_purge(self, ref): ...      # cascade delete; audit first
    def on_copy(self, src, dst, with_comments: bool): ...
    def deletable(self, ref) -> Refusal | None: ...   # signed, locked, attested, legal hold
```

The library already has the tombstone and purge operations; its hook is the first
implementation, and charts, chat and the rest follow it.

## 5. The appkit component contract (CM-20 to CM-31)

```ts
interface CommentsDrawerProps {
  artifact: { kind: string; id: string };     // what is being commented on
  adapter: SelectionAdapter;                  // from the registry for this kind
  host: HostHandle;                           // the host's handle for highlight and reveal
  open: boolean; onClose(): void;
  focusThread?: string;                       // open on a thread (from a pin or a search hit)
  selection?: unknown;                        // the host's current selection, for the "This selection" filter
  mode?: "object" | "collection";             // collection: whole-gallery or folder comments
  canModerate?: boolean;                      // from the authorization gate
}

function useCommentSelection(opts: {
  artifact: { kind: string; id: string };
  adapter: SelectionAdapter;
  host: HostHandle;
}): {
  selection: unknown | null;                  // the single selection model for this host (CM-29)
  button: { visible: boolean; rect: DOMRect | null; onComment(): void };
  threads: ThreadSummary[];                   // for pins, badges and highlight
  count: number;                              // per-object badge
};
```

Rules: the host renders `CommentsDrawer` and calls the hook; it adds no comment
markup. On a page with many objects the page mounts one drawer and re-points it at
the selected object (CM-24). The drawer is full height of its region, one column of
threads ordered by `order_key` then time, with the four filters, each thread headed by
`describe(selector)`. The hook is the only holder of selection state; a host's other
consumers of selection (an assistant context, a linked second view) read it from
the hook (CM-29). Everything is keyboard operable and labelled (CM-31).

Host conformance (run in the host's test suite):

| ID | Test |
|---|---|
| HC-01 | Selecting shows the Comment button beside the selection; pressing it opens the drawer with a compose box anchored to that selector. |
| HC-02 | A saved thread tints its anchor and shows a numbered pin; hover highlights, click focuses the thread. |
| HC-03 | `reveal` brings an off-screen anchor into view. |
| HC-04 | A host with many objects mounts exactly one drawer. |
| HC-05 | A host that does not declare a selection kind never offers it. |
| HC-06 | The host contains no comment markup or API client of its own (guard CG-1). |

## 6. Lifecycle

### 6.1 States

```
live -> trashed -> (restore) -> live
            \-> purged (comments removed with the object)
```

A signed, attested or locked object cannot leave `live` by delete (CM-45); a legal
hold blocks purge. A trashed object's threads are excluded from search and the
assistant context, and from every list except the trash view (CM-49).

### 6.2 Retention and the sweep (CM-42, CM-50 to CM-53)

`comment_object` carries `retention_class` (from the platform's retention classes:
system, operational, qualification, lifetime, legal hold), `trashed_at` and
`purge_after`. On trash: `purge_after = trashed_at + period`, where `period` is the
schedule for the object's site and class, defaulting to 90 days, valid 1 to 3650
days, never below the class's floor, a site only lengthening a floor.

The sweep is a job on the schedule substrate:

```
sweep(dry_run, limit):
  for each comment_object where state='trashed' and purge_after <= now, oldest first, up to limit:
      if the artifact owner's deletable(ref) refuses (hold): skip and report
      audit "purge" (written before destruction)
      if not dry_run: owner.on_purge(ref)      # cascades the comments
  return {candidates, purged, skipped}
```

Schedule settings (per site or node, per object class): `comments.trash.days.<class>`,
read through the platform settings store, audited on change.

### 6.3 Detached anchors (CM-47, CM-07)

On object change (a new data revision, a new document version) or on read, the
service asks the adapter to `resolve`. `attached` and `moved` keep the thread
attached (a moved selector is updated and the move recorded); `detached` sets
`anchor_state='detached'`, keeps `anchor_text`, and the thread stays on the object
at its position in the list under a "detached anchor" heading. A detached thread
is never deleted for that reason.

### 6.4 Copy (CM-48)

`on_copy(src, dst, with_comments)`: by default nothing is carried. With
`with_comments`, threads and comments are copied with `copied_from` set, the
original authors and times kept, and a visible "copied" mark.

### 6.5 The state machine (CM-110 to CM-119)

The transition table is data in the service (`comments/state_machine.py`, one list of
rows). Code asks `machine.apply(machine_name, subject, event, actor)`; it looks up the
row, checks the actor rule and the guard, writes the audit row, applies the effect and
returns the new state. No row, no move: the call raises and nothing changes.

**Composer** (client state; the server sees posting, posted, failed)

| From | Event | To | Who | Guard and effect |
|---|---|---|---|---|
| idle | type | review | author (any with comment access) | text present |
| idle | press mic | recording | author | deliberate press; browser microphone permission; refused permission leaves idle with a message |
| recording | stop, or about 2 s silence | improving | author, or the hook (silence) | clips under 0.4 s are dropped and the composer returns to idle |
| recording | cancel | idle | author | discards the clip |
| improving | server text ready | review | system | swapped in only if the text is unedited; with no server, or on error, the browser draft stands and the move is to review |
| improving | edit text | review | author | the server text is discarded, the author's edit stands |
| review | edit text | review | author | transcript always editable |
| review | keep or discard audio | review | author | sets whether a voice note is attached |
| review | post | posting | author only | never automatic; assistant cannot cause this; permission check; reference warnings shown first (CM-124); idempotency key fixed |
| review | discard | idle | author | draft removed |
| posting | success | posted | system | comment created in state posted; mentions and references notified; audited |
| posting | error | failed | system | draft kept |
| failed | retry | posting | author | same idempotency key; a duplicate returns the first result |
| failed | discard | idle | author | |
| any composer state before review | assistant fills draft | review | assistant | the only composer move the assistant may cause |

**Thread**

| From | Event | To | Who | Guard and effect |
|---|---|---|---|---|
| open | resolve | resolved | anyone with comment access | records who and when; notifies |
| resolved | reopen | open | anyone with comment access | |
| resolved | new reply | open | author of the reply | a reply reopens (CM-114) |
| resolved | no activity for N days | archived | system (sweeper) | N per site and object class; hidden by default, searchable |
| archived | reopen | open | anyone with comment access | restores visibility |
| archived | new reply | open | author of the reply | |
| open, resolved | anchor lost | detached | system (reconciler) | keeps the last known anchor; never deleted |
| detached | re-anchor | open (or prior resolved state) | author, or system when the target returns | adapter `resolve` returns attached or moved |
| detached | resolve | resolved | anyone with comment access | |
| any | object trashed | (unchanged; hidden with the object) | system | object machine drives visibility |

**Comment**

| From | Event | To | Who | Guard and effect |
|---|---|---|---|---|
| posted | edit | edited | author only | history kept; "edited" mark; refused on a locked, signed or attested object |
| edited | edit | edited | author only | same |
| posted, edited | hide | hidden | moderator | reason required; never rewritten |
| hidden | unhide | posted or edited (prior) | moderator | |
| posted, edited | remove | removed | author or moderator | tombstone so replies keep their place; refused on a locked, signed or attested object |
| hidden | remove | removed | moderator | |

**Object lifecycle** (driven by the artifact owner through the `CommentLifecycle` hook)

| From | Event | To | Who | Guard and effect |
|---|---|---|---|---|
| active | move to trash | trashed | whoever may delete the object | all comments follow in one step; signed, locked or attested objects cannot be trashed (CM-45) |
| trashed | restore | active | whoever may restore the object | comments return with their states |
| trashed | retention elapsed | purged | system (sweeper) | at or above the class floor; legal hold blocks; audit first |
| trashed | purge | purged | admin with the right | floor passed; audit first |
| purged | (none) | | | terminal; comments and kept audio deleted; an audit entry remains |

**Rules.** An unlisted (machine, from, event) fails closed. Every applied
transition writes a `comment_transition` row (actor, event, from, to, time) before
its effect is committed. The assistant is an actor with the asker's authority and
may cause only the composer row "assistant fills draft"; any other attempt is
refused and audited. An agent actor may post a comment (agent reply, CM-127) but
has no row for resolve, hide, remove or edit.

**Completeness test (CG-9).** The test loads the table and asserts: every state is
reachable or initial; every non-terminal state has an exit; every row names an actor
rule and an audit event; the product of (machine, state, event) not listed is
refused; and the table in the spec matches the table in code (generated, compared).

## 7. Registry conformance suite (CG-7)

| ID | Test |
|---|---|
| AD-01 | `validate` rejects malformed selectors and accepts every selector the appkit adapter can capture. |
| AD-02 | `describe` on the server and the client agree for the same selector. |
| AD-03 | `resolve` returns `attached` for an unchanged object. |
| AD-04 | After a change that removes the anchor, `resolve` returns `detached` with a reason; after a change that moves it, `moved` with the new selector. |
| AD-05 | `order_key` orders selectors by position in the object. |
| AD-06 | The Web Annotation export of a selector imports back to an equal selector. |

## 8. Reuse and port checklist (CM-50, CM-90)

### 8.1 Reuse evaluation (CM-90)

| Candidate | What it is (first read, not a code audit) | Reuse |
|---|---|---|
| `note` extension | personal quick-capture daily log, indexed into retrieval | the retrieval indexing pattern; not the model |
| `review` extension | local diff review agent producing ranked findings | none; findings are not anchored threads with permissions |
| attestation record | signed record of accountable human acts | comments sit beside it (CM-14); not the model |
| library object store | stores files with tombstone delete, purge, action ledger | attachments, trash and purge, retention classes (reused) |
| notifications service | multi-channel notification primitive | mentions, replies, resolves (reused) |
| authorization gate, audit log, schedule substrate | platform services | reused |

The evaluation is repeated against the code in phase C0 and the result recorded here;
the design changes only if it finds a clean fit for the model.

### 8.2 The partner product's trash and retention system

The design is taken from a partner product that already runs it. Facts read from
its platform repository on main at `432900aff` (2026-10-30):

| Piece (path in that repository) | What it does | Status in Axiom and appkit |
|---|---|---|
| `docs/architecture/archive-delete-propagation.md` | Archive is soft and reversible, hidden from UI, retrieval and memory; permanent delete cascades through database, retrieval, memory and lakehouse with an audit record. | not ported; fits CM-40 to CM-49 as is |
| `services/webapp/api/services/document_archive_retention.py`; migration `20260527_200000_documents_archive_retention.py` | `archived_at` and `purge_after` columns, partial index on `purge_after`, compute, apply, clear (restore), list expired, purge expired with dry run and limit; default 90 days (`ARCHIVE_RETENTION_DAYS`, clamped to 1 to 3650). | not ported |
| `services/webapp/api/routes/internal_cron.py`, `services/webapp/scripts/purge_expired_archived_documents.py`, `docs/operations/purge-expired-archived-documents.md` | sweeper with shared-secret header or admin token, `dry_run`, `limit` (500, max 2000), daily 04:00 UTC job. | not ported; becomes a schedule-substrate job |
| `services/webapp/api/services/chat_archive_service.py`; admin routes `/system-settings/chat-archive` | auto-archive of inactive chats and tasks; `auto_archive.enabled`, `auto_archive.stale_days` (default 90, valid 1 to 365), `auto_archive.run_on_startup`; run-now and dry run. | not ported (listed as a gap in the appkit parity study) |
| `services/webapp/frontend/src/components/pages/settings/ArchiveSettingsTab.jsx`, `chat/DeleteChatConfirmModal.jsx` | archived-items settings tab with restore, batch permanent delete; confirmation text. | partly: appkit has an archived-conversations table with restore |

Already in Axiom and appkit: library tombstone delete (`library/service.py`), manual
purge with action-ledger record (`library/operations.py`, `POST /items/{id}/purge`,
`axi library purge`), appkit's Deleted files view and purge call
(`frontend/src/library/browser/LibrarySidebar.tsx`, `libraryApi.ts`). Open and not
on `main`: Axiom pull request 1548 (branch `feat/w11-w13-labels-retention-appdb`)
adding retention classes with floors (`src/axiom/governance/retention.py`),
the class column (migration `0003_labels_and_retention.py`) and a purge refused
before the floor; matching appkit work on the `appkit-wt-w11-labels` branch.

### 8.3 Port checklist

1. Retention classes and floors from PR 1548 on `main`.
2. `purge_after` and the trash flag, with the partial index, in the shared trash module.
3. The sweep on the schedule substrate, with dry run and limit.
4. The per-site, per-class schedule setting and its audit.
5. The library's lifecycle hook as the first `CommentLifecycle`.
6. appkit trash view and restore generalised beyond Library.

## 9. Assistant context (CM-70 to CM-72)

The context builder returns, for an object, a bounded list:

```
comments: [{thread_id, anchor: "range 11:05 to 14:30", anchor_state, resolved,
            author_label (role or display name), created_at,
            text (delimited as untrusted data), replies: [...]}]
```

Order: threads anchored inside the current selection first, then newest first;
cut at a token budget (the host's budget, default 1,500 tokens for comments) with
a count of omitted threads. Only threads the asker may read are considered; each
call writes an audit record. A drafted comment comes back as a proposal with a
diff-style preview and is posted only through the confirmation card.

## 10. Search (CM-73)

A `comment` provider indexes comment text with the object's title and the
selector's words; results are filtered by the asker's permission on the object
and exclude trashed objects. A hit's link opens the object with `focusThread`.

## 11. Events

`comment.created`, `comment.edited`, `comment.hidden`, `thread.resolved`,
`thread.reopened`, `object.trashed`, `object.restored`, `object.purged`. Consumers:
notifications (mentions, replies, resolves), search (index and remove), audit.

## 12. The composer and voice (CM-100 to CM-108)

The composer is part of `CommentsDrawer`. It owns the text box, the attachments, the
reference chips and the microphone control, and drives the composer machine (6.5).

```ts
const dictation = useDictation({
  transcribe,                        // injected: (blob, filename) => Promise<string>; omit for browser-only
  onInterim: setDraftInterim,        // live draft while speaking
  onResult: setDraftFromBrowser,     // browser draft on stop
  onUpgrade: swapIfUnedited,         // server text, swapped in only if the draft is unedited
  onError: keepBrowserDraft,
});
// minRecordMs (400), silenceAutoStopMs (2000) are the hook's defaults and are not overridden.
```

Rules: the composer imports the chat's hook from appkit's `chat/useDictation` (or its
shared re-export) and nothing else for speech (CG-8). The recognition language is set
from the person's profile. On stop the composer moves to improving, then review; the
transcript is a normal editable text. A kept clip is uploaded to the object store at
post time and stored as `comment_audio`; a discarded clip never leaves the browser. The
microphone permission prompt is the browser's own and is requested only on a press.
The server transcription call is supplied by the host application (appkit carries no
endpoint); with none, the browser draft is the transcript. The transcript is indexed
(CM-108); the audio is not read by the assistant.

## 13. References (CM-120 to CM-130)

### 13.1 Address form

`@name` with an optional `:context`, parsed by the platform's single addressing parser
(the principal naming form; regex `@([\w\-\.]+)(?::([\w\-\.]+))?`), shared with the
assistant chat. A reference stores both the address text and a resolved target:

| `ref_kind` | Resolves to | Notification |
|---|---|---|
| person | a principal | notified, added to the thread |
| group | a role or named group | members who can read the object |
| agent | an agent on the roster | request routed (13.3) |
| artifact | (kind, id) | none by itself; adds a backlink; the target's owner may opt in |

An address that does not resolve stays plain text (CM-130).

### 13.2 Artifact references

`#` opens the picker (section 14); a pasted link of a known shape is converted to a chip
on paste. The stored form is `comment_reference(ref_kind='artifact', target_kind,
target_id, label_at_post)`. Rendering resolves the chip at read time through the
authorization gate: readable shows the live title and kind; unreadable shows
"restricted artifact" and no more; deleted shows a "removed" chip with the label at post
time only if the viewer could read it when posted (otherwise "removed artifact").
Renames and moves change nothing stored because the link is by id.

Before posting, the composer asks the service for a **reference check**: for each
mentioned person and referenced artifact, whether the people on the thread's object can
open it. The result lists only counts and names the author may already see ("one mentioned person cannot open this chart"); the author posts anyway or edits (CM-124).

Backlinks: `GET /api/v1/comments/backlinks?artifact_kind=&artifact_id=` returns the
comments referencing the artifact, filtered by the viewer's read permission on each
comment's object; the count shown is the filtered count (CM-125).

### 13.3 Agent mention

A mention of an agent creates a request routed through the same mechanism as the chat's
agent addressing (the `agents.address` capability): the message is the comment text and
the context is the comment's object and selector. The agent's reply is posted as a
comment authored by the agent principal, labelled "agent, asked by <person>", with the
requesting person on record in the audit row. The agent acts under its own approval
rules; a person's posting of the mention does not approve anything the agent then does.
The state machine has no resolve, hide, remove or edit row for an agent actor (6.5).

## 14. The picker (CM-140 to CM-146)

One component, shared with the assistant chat, serves `@` and `#`:

```ts
interface PickerProps {
  trigger: "@" | "#";
  query: string;
  context: { site: string; artifact?: { kind: string; id: string } };
  onPick(item: PickerItem): void;
}
type PickerItem = {
  ref: { kind: "person" | "agent" | "group" | "artifact"; id: string; address?: string };
  label: string; kind_label: string; detail?: string;
  match: "exact" | "prefix" | "fuzzy";
  reason?: string;                    // shown for fuzzy: "matches title and series", "close to the name"
};
```

Backend: the platform's agent-backed search (the search extension) returns candidates
ranked in three tiers: exact, prefix, fuzzy. Fuzzy covers typos (edit distance), partial
names, and descriptions of contents (the chart PRD's content search, CF-180 to CF-185),
each with a reason. Results are filtered to what the asker may open (the authorization
gate). People and agents are searched within the asker's site first, then beyond it
where the asker may address them. A fuzzy item renders with "did you mean" and its
reason and is never applied automatically; selection is by the person (CM-141). The
chat and the composer pass different `context` and the same component renders results.
Until the fuzzy search exists the backend returns only exact and prefix items
(CM-146). Whether the chat already has this as a standalone component is unknown
(PRD CQ-5); if not, it is extracted from the chat first.

## 15. Open items

CQ-1 to CQ-7 in the PRD. Unknown today: the exact entry-point name for adapter
registration, the token budget default, and the thread-list performance ceiling
(set in C6).
