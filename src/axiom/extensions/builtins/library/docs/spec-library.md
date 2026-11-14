# Library: upload, filing and retrieval

**Status:** Living (2026-10-28) · **Owner:** library extension · **Decisions:** [ADR-213](../../../../../../docs/adrs/adr-213-upload-is-a-composable-extraction-pipeline.md) (the upload pipeline), [ADR-212](../../../../../../docs/adrs/adr-212-a-library-upload-is-filed-on-the-node-that-holds-the-library-and-a-delete-keeps-the-bytes.md), ADR-150 (Book is the Library)

The library is the Book on the rail: a person's and a site's documents, filed
under the product's vocabulary, browsable, previewable, and searchable by what
they say. Anything uploaded, to the Book or as a chat attachment, enters the
data platform's bronze ingest and ends as chunked, embedded text in the
retrieval corpus its visibility selects.

The surface is ported from SoilMetrix's Library (owner-approved, 2026-10-28):
its browser, upload flow, preview and limits, with the agronomy removed. The
machinery is Axiom's; the vocabulary is a taxonomy the product registers.

## 1. Inventory: what was ported, adapted and left

Source: `soilmetrix-platform@dd3a7602b`, `services/webapp/`.

| Component | Source | Target | Note |
|---|---|---|---|
| Library page | `frontend/src/components/pages/Library.jsx` | appkit `library/LibraryPage.tsx` | Ported: load, tree, search, upload surface, viewer. Accounts/fields became site/collections. |
| Browser (tree + list) | `components/library/LibraryBrowser.jsx` | appkit `FileLibrary.tsx` (already extracted) | Reused as is; the page feeds it. |
| Tree builder, filters, status badges | `utils/librarySvarData.js` | appkit `library/libraryTree.ts` | Ported. Field → collection; 19 agronomy categories → the registered taxonomy. |
| Drop zone | `components/shared/UploadDropZone.jsx` | appkit `library/UploadDropZone.tsx` | Ported; tokens instead of Tailwind. |
| Drop surfaces + global drag | `contexts/UploadContext.jsx`, `shared/UploadSurface.jsx`, `utils/uploadSurfaces.js` | appkit `library/UploadProvider.tsx` | Ported. Field/account targeting → visibility/collection. |
| Upload flow (drop → preview → process → result) | `shared/UnifiedUpload.jsx`, `shared/UnifiedUploadModal.jsx` | appkit `library/LibraryUpload.tsx`, `library/UploadModal.tsx` | Ported. Boundary maps, field creation, nutrient and collision steps left (domain). |
| Preview | `components/FileNavigator/FileViewer.jsx` | appkit `library/FileViewer.tsx` | Ported: PDF, image, paged text, download. Modus XML export left (domain). |
| Chat "choose from Library" | `components/chat/ChatLibraryPickerModal.jsx`, `utils/chatLibraryPickerUtils.js` | appkit `library/LibraryPicker.tsx` | Ported for the chat attachment work, with a reusable upload client. |
| Categorized "book" | `components/library/FieldLibrary.jsx` | `FileLibrary` categories mode | Left: routed nowhere in the source. |
| Upload limits | `api/utils/upload_limits.py` | `library/limits.py` | Ported and extended (below). |
| Documents API | `api/routes/documents.py` | `library/api.py`, `library/service.py` | Ported: upload, list, get, file, update, archive → delete, restore, search, index status. Left: share links and public URLs (wait for ADR-208/209 grants), thumbnails, zip download, GeoJSON. |
| Classify preview | `api/routes/unified_upload.py` `/classify` | `POST /api/v1/library/classify` | Ported, domain-free. |
| Storage, archive, purge | `api/services/document_service.py` | bronze (`FilesystemBronzeSink`) + `service.py` | Adapted: bronze instead of a bucket; archive is the tombstone; purge left to retention. |
| Indexing | `index_document_in_background`, `DocumentIndexingService` | `data_platform.rag_embed.embed_bronze_record` | Reused, not forked. Interim: this reads bronze straight into the RAG; the route is bronze → conform → silver → gold → RAG (ADR-079). |
| Placement taxonomy | `api/utils/library_placement.py` | `library/taxonomy.py` (existed) + a product registration | The product registers its vocabulary; Axiom ships a generic file-kind one. |
| Pluggable classifiers | `api/services/upload_providers` | `ClassifierChain` (existed) + `register_chain` | Adapted. |
| Lab, boundary, field and Modus handlers | `modus_upload_handler`, `field_extractor`, … | none | Left: domain. |

## 2. The upload path

> Since ADR-213 every upload runs the six-stage pipeline (received, stored,
> identified, extracted, conformed, indexed) with contributed extractors,
> routes, the Data Book and previews; see
> [spec-upload-pipeline](spec-upload-pipeline.md). The steps below are its
> `received` and `stored` stages and the `rag` route.

1. **Client → the node's upload endpoint.** `POST /api/v1/library/items`
   (multipart, `files` plus `visibility`, `title`, `tags`, `collection`). The
   mount is nested in `/api/v1` with `requires_authz`, so the authz hook
   resolves a session cookie or a gate key; the handler reads the principal and
   site that hook verified (`scope.caller_from_request`). A loopback request with
   no credential is the machine's own operator.
2. **Guards** (`limits.validate`), before anything is written: the name is
   reduced to a basename with no separators or control characters; the type must
   be on the allowlist (SVG and HTML are not); size against the per-file and
   per-request caps; the bytes must match the name's signature; zip-based
   documents are measured from their directory and then streamed with a hard
   stop, refusing bombs and unsafe member paths; an optional scanner
   (`AXIOM_LIBRARY_SCAN_CMD`) refuses on non-zero exit.
3. **Bronze.** `BronzeWriter` + `FilesystemBronzeSink` (the pair the push
   `IngestSink` composes) rooted at `<library root>/<site>/`. The blob is
   content-addressed; the sidecar records site, owner, visibility, checksum,
   type and the decision.
4. **Catalog.** `library.library_items` (schema per ADR-052) and a
   `library.library_events` row, committed before indexing, so the document is
   listed even when retrieval is down (`index_status = pending`).
5. **Intake.** Classification under the active taxonomy: what it is, how sure,
   who decided, where it files.
6. **Retrieval.** `embed_bronze_record` (the interim bronze-to-RAG shortcut;
   ADR-079's route goes through conform and gold): extract, chunk, the ingestion guard,
   embed (`nomic-embed-text`, 768-dim, or whatever the node's embedder is),
   upsert under `source_path = library/<site>/<id>/<name>` into the corpus below.

Identical bytes the caller can already see, at the same visibility, are not
filed twice; the existing document is returned with `duplicate: true`.

## 3. Scoping

**Site** comes from the credential (`caller_site`), else the handle's
`@name:site`, else the node's home site, else `default`. A request cannot name
another. **Storage** is `<library root>/<site>/`: one tree per site.

| Visibility | Home site, or a local install | A tenant site |
|---|---|---|
| `site` (default) | `rag-org` | `rag-tenant:<site>` |
| `private` | `rag-personal:<principal>` | `rag-personal:<principal>` |

The site corpora are the ones a served conversation already reads for that
caller (`chat.scope.shared_corpora_for`), so a site upload is in the site's
chat without more wiring. A private corpus is read only by its owner's library
search; a chat surface that should ground on it names it
(`scope.readable_corpora`).

**Access** is ADR-263's: every verb asks the one decision point
(`axiom.governance.access`) through `access.py`, which hands it what only the
Library knows: the Owner (`owner`, a folder's `created_by`), the person's
folders that enclose the item (each passes its role down, capped at Editor),
general access before sharing existed (`visibility`: `site` is Everyone at the
site · Viewer, `private` is Restricted; written in step as the projection), an
export-control hold (shares suspended) and who deleted it. Each verb names its
least role (prd-sharing-and-access SHR-R1.4): a Viewer opens, previews,
downloads and copies; an Editor renames, re-extracts, adds or restores a
version, and moves (into a person's folder only with Editor there too); delete
is the Owner's or an Editor's of the folder that holds it; restore the Owner's
or the deleter's; delete forever and the label the Owner's. No role is 404; a
role too low is 403 naming the role needed; each answer is a `decide()`
receipt. Listings and search filter with `access.readable_ids` (one read of
the record per page) after a SQL pre-filter (`access.sql_scope`).

**Folders** (`folders.py`): creating one needs only being at the site, and it
is the maker's and Restricted, so another person's folder is not in your tree.
Renaming or moving a person's folder needs Editor on it; a Data Book category
is renamed, moved or hidden by a Book manager (site-admins hold it). Deleting a
person's folder needs Owner or Editor and takes what it holds, subfolders
included, to Deleted files as one act (`deleted_with`, migration 0007) with one
undo (`POST /book/folders/restore`); an item held for review, under a legal
hold, or not the person's to delete stays, and so do the folders that hold it.
The classifier files a person's upload only into folders that person may file
into, so a placement never decides who can read a file. Every upload, change,
delete and restore is an event.

**Retrieval follows access** (prd-sharing-and-access SHR-R8.3). General access
reaches people through the corpus, as above; a Restricted file shared by name sits
in its Owner's own corpus, which nobody else's retrieval names. The Library's
source guard (`retrieval.py`, registered in `axiom.rag.source_guards`) closes both
ends for every caller of `axiom.rag.retriever.retrieve` (chat, `axiom_rag__retrieve`,
the served retrieval and the federation search): it names the Owners' corpora that
hold what was shared with the person asking, and keeps a `library/<site>/<id>/`
chunk only when the item is live and the decision point lets that person read it.
With no principal only what Everyone at the site may read passes, and a guard that
cannot answer withholds its chunks. Library search adds the same corpora
(`access.shared_corpora`) and filters through `access.readable_ids`.

**Local mode** does all of this on the local node. **Bound mode** relays every
library call to the bound node with the binding's key (ADR-212); the node does
all of it there.

## 4. Delete and retention

Delete removes the chunks from the corpus and the document from every listing
at once; the row is a tombstone, bronze gains `library/_deleted/<day>/<id>.json`,
and the blob is kept. `restore` re-indexes from the blob. Every delete is
recorded in the tamper-evident action chain (`retention.delete.library.item`)
before anything changes.

**Delete forever** (`POST /items/{id}/purge`, `axi library purge`, CLI only) is
the deletion the retention floor governs (MVP-22-A2). It is the one purge, in
`operations.py` (§5.2): each item carries a retention class from
`axiom.governance.retention` (`system` ~1 y, `operational` ≥ 5 y, the default;
`qualification`; `lifetime`; `legal_hold`), and with the labels on a purge is
refused with 423 before the class's floor (the refusal names the class and the
date it may be purged from), under a legal hold, for a lifetime item, and under
an export-control hold; `_purge_refusal` is where every hold and floor is
checked. Otherwise the row, its versions and everything derived from the bytes
go, the blob leaves bronze unless another item or version still points at it,
and the record is the action ledger entry, the `purge` event and the
`<id>.purged.json` file beside the bytes. A class may lengthen and never
shorten; a legal hold ends only with `release_hold`.

## 4a. Export-control labels (MVP-22, W11)

One label model, `axiom.governance.classification.ExportControlLabel`, on every
item (`ec_label`): a value (`public`, `internal`, `regulated`, `controlled`) and
a state. The EC hook's verdict *proposes* one (clear → internal, sensitive →
regulated, held → controlled); a person *confirms* it, at upload (`ec_label`
field, `--label`) or after (`POST /items/{id}/label`, `axi library label`,
`library.label`). Export control attaches to the controlled codes, not to data,
so most items are `internal`.

With the `library_labels` flag on (staging on, production off by default):

- an upload without a confirmed label is stored and listed, not ingested
  (`index_status = awaiting-label`); confirming the label indexes it from the
  retrieval document already written (MVP-16-A2);
- an unlabelled item is not returned by search and cannot be downloaded
  (MVP-22-A1); `axi library label --sweep` (service `sweep_unlabelled`) takes
  items indexed before the flag out of retrieval.

Whatever the flag, a confirmed label is stamped on the item's chunks
(`chunks.classification`), and the retriever returns a chunk only when its label
is at or below the person's clearance (`governance.classification.clearance_for`:
`internal` for a verified person until the directory supplies an attribute,
`public` with no principal). Library search and content reads apply the same
comparison (MVP-06-A1). A **sample** item (`sample`) shows as a sample, is never
indexed and is never exported (MVP-25-A5).

## 5. Surfaces

| Verb | HTTP (`/api/v1/library`) | CLI (`axi library`) | Skill / MCP |
|---|---|---|---|
| upload | `POST /items` | `upload <paths…>` | `library.upload` |
| list | `GET /items` | `ls` | `library.list` |
| show | `GET /items/{id}` | `show` | `library.show` |
| preview | `GET /items/{id}/content` | — | — |
| change | `PATCH /items/{id}` | `update` | `library.update` |
| delete | `DELETE /items/{id}` | `rm` | `library.delete` |
| restore | `POST /items/{id}/restore` | `restore` | `library.restore` |
| label | `POST /items/{id}/label` | `label <id> [label] [--retention]` | `library.label` |
| delete forever | `POST /items/{id}/purge` (423 under a hold or before the floor) | `purge` | `library.purge` (CLI only) |
| search | `GET /search?q=` | `search` | `library.search` |
| classify | `POST /classify` | — | — |
| limits, facets, taxonomies | `GET /limits`, `/facets`, `/taxonomies` | — | — |
| star, unstar | `PUT`/`DELETE /items/{id}/star`, `/stars/folders/{path}` | — | — |
| starred | `GET /starred` | — | — |
| opened, recent | `POST /items/{id}/opened`, `GET /recent` | — | — |
| shared with me | `GET /shared` | — | — |
| as uploaded | `GET /originals?page=&page_size=&job=` | — | — |
| list, paged | `GET /items?limit=&cursor=&sort_by=&sort_dir=&root=` (answers `next_cursor`) | — | — |
| copy | `POST /items/{id}/copy`, `POST /book/folders/{path}/copy` (`{"folder"}`) | — | — |
| versions | `GET/POST /items/{id}/versions`, `POST …/versions/{n}/restore`, `GET …/versions/{n}/content` | — | — |
| delete forever | `POST /items/{id}/purge` (423 under an export-control hold, a legal hold, or before the retention floor) | `purge` | `library.purge` (CLI only) |
| zip | `POST /zip` (`items`, `folders`; `?check=true` answers size or refusal) | — | — |

Preview and classify are browser concerns and have no CLI or skill form. Nor
do the browser's common views below: they are a person's own browsing state.

### 5.2 Copy, versions, delete forever, zip (`operations.py`)

- **Copy** makes a new item owned by the copier over the original's
  content-addressed bronze blob and sidecar (nothing written twice;
  `attributes.copied_from` names the original), then runs the pipeline from
  `identified`, so the copy has its own extractions, placement and chunks. A
  folder copy recreates the folder and its subfolders under the destination.
- **Versions** (`library_versions`, migration 0004): a new version lands in
  bronze like an upload and the item moves to it under a new job; extraction
  rows are keyed by job, so each version keeps its results. Restore makes a
  new version pointing at the older bytes. Editors add and restore; Viewers
  list and download (Editors only, when the Owner turned download off).
- **Delete forever** only from Deleted files, only by the Owner (404 to
  anyone else). Refused with 423 while
  the item's export-control verdict is `held`, and, with the labels on
  (MVP-22), before its retention class's floor, for a lifetime item or under a
  legal hold; `_purge_refusal` is the one place every hold and floor is
  checked, and names the rule the refusal is ledgered under. Every
  attempt is in the action ledger (`library.purge`) and a `purge` event; bytes
  leave bronze only when no other item or version points at them, and a
  `<id>.purged.json` record stays.
- **Zip** streams the selection (folders as directories, repeated names
  suffixed) holding one 1 MB chunk at a time; over `AXIOM_LIBRARY_MAX_ZIP_BYTES`
  (default 2 GB) or 1000 files it is a 413 with the reason, before any byte.

### 5.3 Tidy up (`tidy.py`)

Propose, review, apply, as the earlier product's Library did (parity F26).
`POST /tidy/proposals {item_ids?}` reads the caller's own live files (up to 500;
`capped` says when there were more) and returns, sorted by kind then item id so
the same Library proposes the same list:

- `delete_duplicate`: same SHA-256; the earliest copy is kept (`duplicate_of`),
  each later one proposed for Deleted files.
- `move`: the Book, filing it now with the site's corrections and folders, puts it
  elsewhere and is confident (`to_folder`, `to_labels`, `because`, `confidence`).
  A file a person placed (`placement.source = manual`) is never proposed. A move the
  Book only inferred (a correction or a folder's resemblance) is capped at 0.7, so a
  client's review starts it unticked; same bytes are 1.0.

Proposing changes nothing. `POST /tidy/apply {items: [{item_id, action, to_folder?}]}`
re-checks each accepted item and runs the person's own move (recorded as a
correction, so the Book learns) or delete (tombstone, restorable); an item that is
no longer the caller's, a move with no folder, or a duplicate whose other copies are
gone is `skipped` with its `reason`. Web only for now; the CLI and skills follow #1957.

### 5.1 The browser's common views (`views.py`)

The Library browser opens on a column of views every person has, above the
product's Book: All files, Recent, Starred, Shared with me, Deleted files and
As uploaded (ported from the source product's browser). All files and Deleted
files are `GET /items`. The other four:

- **Starred** and **Recent** are per person and per site:
  `library_marks` (migration 0003) holds one row per (site, principal, item
  or Book folder) with `starred_at`, `opened_at` and `open_count`. Recent is
  what the caller opened (`POST /items/{id}/opened`, which the browser sends
  when it opens a preview) or uploaded, whichever is later, with
  `recent_reason`. A colleague's opens never count. Every listing re-checks
  the item through the decision point, so a star on a document that went private to someone else, or
  was deleted, is kept but not served.
- **Shared with me** lists first what was shared with the caller or a group
  of theirs by name (`shared_via: "shared"`, with who shared it, when, the role
  and the group it came through), newest share first, then what colleagues
  filed for the caller's site (`shared_via: "site"`). The answer carries
  `grants: true`; the site rows leave this view when the web's sharing lands
  (prd-sharing-and-access SHR-R9.2).
- **As uploaded** is the provenance listing: each live file by the name it
  was given, with its SHA-256, size, who sent it and the surface it came
  through (from the `upload` event), newest first, paged, narrowable to one
  upload by `job`. Read-only: the one action is `download_href`, the
  checksum-verified original bytes from bronze.

## 6. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AXIOM_LIBRARY_BRONZE_ROOT` | `<state dir>/library/bronze` | Where the per-site trees live. |
| `AXIOM_LIBRARY_MAX_FILE_BYTES` | 100 MB | Per-file cap. |
| `AXIOM_LIBRARY_MAX_FILES` | 20 | Files per request. |
| `AXIOM_LIBRARY_MAX_BATCH_BYTES` | 200 MB | Bytes per request. |
| `AXIOM_LIBRARY_SCAN_CMD` | unset | Scanner command (stdin, non-zero refuses). |
| `AXIOM_LIBRARY_TAXONOMY` | the one product taxonomy, else `generic` | Active vocabulary. |

## 7. Follow-ups

Share links and public URLs on ADR-208/209 grants (and "Shared with me" from
those grants instead of the site); thumbnails; a scheduled purge of tombstoned
items past their floor; a re-index pass for `pending` documents; chat grounding
on the private corpus. ADR-213's open questions (releasing an export-control
hold, scheduling the upload sweep and boot recovery, member items) are tracked
there.
