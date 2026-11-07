# ADR-213: Upload is a composable extraction pipeline

**Status:** Draft (2026-10-28), for review
**Related:** [ADR-212](adr-212-a-library-upload-is-filed-on-the-node-that-holds-the-library-and-a-delete-keeps-the-bytes.md) (the library's bound-mode write path), ADR-208 draft (conforming utilities are governed catalog objects; this ADR adds the `extractor` and `previewer` kinds), [ADR-206](adr-206-silver-identity-is-the-reading-and-a-rebuild-replaces-in-place.md) (a rebuild replaces in place), [ADR-203](adr-203-a-data-kit-is-promoted-as-data-not-as-a-code-release.md) (a kit is promoted as data; D6 keeps contributor Python out of platform processes), ADR-194 (deposits are bronze), ADR-049 (bronze is the substrate of record), ADR-150 (the Book is the Library), ADR-182 (expand, then contract), ADR-174 (Postgres at runtime), [spec-library](../../src/axiom/extensions/builtins/library/docs/spec-library.md), [spec-upload-pipeline](../../src/axiom/extensions/builtins/library/docs/spec-upload-pipeline.md), [Build a previewer](../guides/build-a-previewer.md)

## Context

The Library's upload path (ADR-212) takes a document, lands it in bronze,
catalogues it, classifies it under a taxonomy and embeds its text. It was ported
from a product whose uploads are mostly of one domain's documents, and it shows:
an allowlist of office and image types, a 100 MB ceiling, one text extraction,
and nothing else learned from the file.

The owner's direction (2026-10-28) is broader. Anything a scientist works with,
large and small, dragged into the Library or into a chat, should "do the right
thing": the file is kept, what is in it is extracted, and each part goes where
it is useful: retrieval, a dataset that conforms to silver, a model registry,
a reduced-order-model registry, a future training set. What "what is in it"
means is a domain's knowledge: one product reads soil reports, another reads
simulation input decks and detector spectra, and a data kit knows its own
instrument's export. The strength (storage, guards, isolation, provenance,
status, routing, export control) has to be the platform's, and the knowledge
has to be contributable.

Three more requirements arrived the same day and are the same shape:

- every item is **filed automatically** in a classification scheme (the
  **Data Book**; products rename it), which a domain contributes, and which
  people extend with their own folders and correct by moving things;
- every item **previews** in the Library and in chat, with previewers a domain
  or kit contributes, and a designed fallback when nothing can show it;
- people see **every file's status**, through long uploads and many files at
  once, and whether each one worked.

## Decision

**An upload is a pipeline of six stages run by the platform. Domains, packs and
kits contribute extractors, previewers, classification schemes, routes and
controlled-code entries, as declared data in their manifests. Contributed code
never writes anywhere: it returns results, and the platform decides where they
go.**

### D1. Six stages, each recorded before the next starts

`received → stored → identified → extracted → conformed → indexed`.

- **received:** bytes arrive through a resumable session (D2) or a multipart
  form. The name, type and size guards run on the declaration, before a byte is
  kept.
- **stored:** the whole file's checksum is verified (against the client's when
  sent), the content guards run (signature, container bombs, the optional
  scanner), and the file moves into bronze, content-addressed, with its sidecar.
  The catalog row is written. **From here on the file is never lost.**
- **identified:** the registry plans the extractors (D4).
- **extracted:** each extractor runs, isolated and bounded (D6); results land as
  bronze-derived records and catalog rows (D8).
- **conformed:** destination tags go to their routes (D7); the Data Book files
  the item (D11).
- **indexed:** the export-control hook decides (D9); extracted text is chunked
  and embedded into the corpus the item's visibility selects (ADR-212's scoping,
  unchanged).

Each stage's status, timestamps and a one-line detail are written to the job as
they happen, so "what happened to this file" is answerable mid-run, after a
crash, and from any surface (D10).

### D2. Resumable upload is chunk plus offset, tus-shaped

`POST /uploads` declares name, size and an optional SHA-256; `PATCH /uploads/{id}`
carries one chunk at `Upload-Offset` with an optional per-chunk
`Upload-Checksum`; a wrong offset answers 409 with the server's offset, so a
resumed client continues exactly where the bytes stopped; `GET` answers the
offset. Staged bytes live under the site's own prefix on the same filesystem as
bronze, so storing is a rename, and a multi-gigabyte file is never held in
memory. Idle sessions expire and their bytes are removed. The ceiling for a
resumable file is separate from the multipart one (50 GB by default) and is
checked against free space when the session opens.

We chose this over adopting the tus protocol whole: the same three verbs, no
protocol negotiation or extension headers to support, and the same authz and
bound-mode relay (ADR-212) as every other library call.

### D3. The extractor contract

An extractor is a manifest plus a function `extract(ctx) -> ExtractionResult`:

- **name** `<scope>.extract.<name>` (ADR-208 D2's three-level name, with an
  `extract` area) and a semantic **version**;
- **match rules**: extensions, file-name globs, MIME, magic bytes at an offset,
  and regular expressions over the decoded head; an implementation may add
  `sniff(ctx) -> float` for a match the rules cannot express;
- **outputs** declared from `metadata`, `features`, `text`, `tables`,
  `artifacts`, `registrations`, `preview`;
- **destination tags** from `rag`, `dataset`, `training-data`, `input-deck`,
  `rom-artifact`, `reference-data` (a result may add tags);
- **limits**: the largest file it takes, a timeout, a memory limit;
- **isolation**: `thread` or `process`;
- **the export-control hook mode** `ec`: `screen`, `payload` or `none`, where
  text bound for retrieval cannot declare `none`;
- **narrowing**: `supersedes` (named extractors that do not run when this one
  matches), `exclusive`, `always`;
- **status** `active` or `stub`, `accepts` (types it adds to the allowlist),
  `requires` (optional modules), and optionally the ADR-208 `reader` its dataset
  output is shaped for.

The context is path-based (`ExtractionContext`), so an extractor streams a large
file rather than receiving its bytes. It also carries `siblings` (an archive's
other members, so a format that needs a companion file, a samples file and its
channel catalogue, is uploaded with it in one archive) and `params` (short
values the person gave at upload, `extract_params`: a run's start time, a map
name). A result may add tags and may drop manifest tags (`drop_tags`), as a deck
with no prose drops `rag`. `NotYet` and `ExtractorUnavailable` are
outcomes, not failures: a recognised format with no extraction yet, and a
missing optional dependency, each with a remedy.

**`extractor` is a new ADR-208 kind, not a `reader` specialization.** A reader
turns one bronze shape into records for silver; an extractor turns any file into
several kinds of output, only one of which (tables bound for `dataset`) is
reader-shaped. An extractor whose dataset output is a reader's names that reader
(`reader = "core.readers.read_tdms@1"`) and the `dataset` route hands its tables
to the conform path that reader feeds, so the reader is reused, not copied.
ADR-208's catalog lists extractors and previewers beside its six kinds with the
same name, version, owner and visibility rules; its duplicate check (D3) applies
by match signature.

### D4. Fan-out, priority and composition order

Every matching extractor runs: a spreadsheet gets tables and text, a deck gets a
domain summary and its universal facts. Order is **priority** (high first), then
**scope** (kit, then site, then pack, then core: the more specific contributor
wins a tie), then **match strength** (magic > sniff > name > extension > MIME),
then name. `supersedes` removes named extractors, `exclusive` removes everything
after it except `always` ones (the universal facts), a file over an extractor's
size limit skips it, and a missing dependency marks it unavailable. Every skip
is recorded with its reason.

`[[extractors]]` previously declared "sense extractors" (`name`, `module`,
`file_patterns`), which nothing reads at runtime; an entry with `module` and no
`version` is that legacy form and the registry skips it. The AEOS manifest
schema lists the five ADR-213 arrays.

Contributions are read from every discovered extension's manifest (top-level
`[[extractors]]`, `[[previewers]]`, `[[book_schemes]]`, `[[upload_routes]]`,
`[[controlled_codes]]`), from the `axiom.library_contributions` entry point
group, and from a kit's `kit.toml`. The core set is registered in code, so every
install has it however discovery fares.

### D5. Core extractors

File facts (always), text, Markdown, reStructuredText, logs and source code,
JSON/JSON lines/YAML, XML (no entity expansion), PDF, Word/PowerPoint/
OpenDocument, Excel (every sheet a table), CSV/TSV (streamed; types, sample, row
count, and series detection), Parquet, HDF5 and NetCDF-4 (structure, attributes,
peeks; never a whole dataset read), classic NetCDF, images (EXIF, with location
recorded as present and never copied), and archives.

### D6. Failure semantics: isolated, bounded, never fatal

Each extractor runs with its timeout. `thread` isolation abandons a thread that
overruns; `process` isolation runs a spawned child with an address-space limit
and kills it at the timeout. **Kit extractors always run in a process** (ADR-203
D6). Any way an extractor ends is an outcome on its row: `done`, `failed`,
`timeout`, `not-yet`, `unavailable`, `skipped`, with the error and a remedy. One
extractor failing does not stop another, and a failed route does not undo an
extraction. A job ends `done`, `partial` (something did not work; the file is
safe), `failed` (it was never stored), `held` (D9) or `cancelled`. Retry and
re-extract start from bronze.

**Archives** recurse within limits: depth (3), members (2,000), total
uncompressed bytes (4 GB) and per-member compression ratio (200:1), all
environment-tunable. Unsafe paths, links and devices are listed and never
unpacked; over a limit the listing stays and unpacking stops, with the reason.
Members are identified and extracted with the archive as their item, and their
provenance names the member path (`a.zip!/inner.zip!/b.csv`).

### D7. Routing: tags request, routes decide

`rag` is the pipeline's own (D9 then the embed). `dataset` writes every table
as bronze-derived JSON lines beside the item, then offers it to each registered
`dataset` route (a domain's conform lane, the bronze→silver connector path).
`training-data` and `reference-data` are marked in the catalog for consumers.
`input-deck` and `rom-artifact` have no core route: a domain registers its model
registry and ROM registry. A tag with no route on this install is recorded as
`no-route`, kept, and routed when one is installed and the item is re-run.
A route may answer `None` ("not mine": another pack's tables) and records
nothing. Routes are idempotent on (item, extractor), return a status, a detail, a remedy
and a link the client shows (a retrieval document, a registry entry, a dataset).

### D8. Provenance and re-extraction

Every result is a bronze-derived record named by extractor and version
(`_derived/<item>/<extractor>@<version>.json`, with the full text and the input
checksum) and a catalog row recording extractor, version, scope, contributor,
member, status, duration and the output's checksum. **A new extractor version
makes its items stale** (`GET /stale`); re-extraction (`--rebuild` in ADR-206's
sense) **replaces in place**: the new row becomes current, older rows remain as
history, and every route is offered the new result. A partial re-extraction keeps
the other extractors' current results. A person's manual placement (D11) is
never undone by a re-extraction.

### D9. Export control attaches to code, at the hook before retrieval

Export control attaches to a small, declared set of controlled simulation codes
and closely held methods, not to data. The hook asks three things of every item
before any of its text reaches a corpus:

1. an extractor declared `ec = "payload"` and named the code its file feeds: is
   that code in the **controlled-code registry** (`[[controlled_codes]]`,
   declared data)? Then the item is **held**;
2. a source-code file whose text matches a controlled code's `source_sniff`:
   **held**;
3. every extracted text goes through the marker screen retrieval ingest already
   runs: `controlled` holds; `sensitive` indexes to the item's own site or
   personal corpus and is flagged for review.

A held item is stored, listed, registered and downloadable by those who may read
it; its text is not indexed until a reviewer releases it. **Hold is fail-closed
for the whole item**, so a generic text extractor cannot carry a deck past a
domain extractor's verdict.

### D10. Status is a job, streamed

Every upload, retry and re-extraction is a job with per-stage status, bytes
received, overall progress, and an error with a remedy. `GET /jobs` and
`GET /jobs/{id}` answer; `GET /jobs/stream` is server-sent events (one event
per change, a keep-alive comment, an end event when every requested job is
terminal) and a client falls back to polling. `GET /items/{id}/pipeline` is the
whole story of one file: job, extractions, routes, EC verdict, placement, events.
The bound-mode relay passes the upload headers through and relays the stream as
it arrives.

### D11. The Data Book: domain-contributed classification, extended by people

The Library is the feature; the **Data Book** is its classification scheme, and
products give it their own name (one constant in the client, overridable by the
scheme's `display_name`). A scheme is data: hierarchical **categories** and
**rules** over the signals the pipeline has (extractors that ran, destination
tags, name, extension, MIME, metadata, features, text), each with a confidence
and a `because` sentence. A rule's category may be a template
(`measured/{metadata.site}`), so a scheme files by instrument or by code without
listing every value. Axiom ships a generic Data Book; the first product scheme
installed replaces it, and `AXIOM_LIBRARY_BOOK` chooses among several.

**Every item is filed.** Precedence, highest first:

1. **corrections**: a person moved items with the same signature (extractors,
   extension, short features) to a folder; the next similar item follows;
2. **user folders**: a folder a person made extends the scheme; its name and
   the signatures of what is already in it are signals, and it outranks a domain
   category when it fits at least as well (a 0.05 margin), because it is the
   person's own statement of how they work;
3. **scheme rules**;
4. the legacy taxonomy's document type, when the scheme has that category.

Below 0.35 confidence an item goes to **Unsorted, needs a look**; below 0.6 it is
filed and marked for a look. Every placement keeps its explanation ("Filed
under Logs / Line B7 because it is a widget log from line B7"). A folder's path
is its identity: renaming changes its label, moving changes where it is shown,
and no item is re-filed because the tree was tidied. A move pins the item
(source `manual`) and records a correction. A scheme is listed in ADR-208's
catalog as reference data (`<scope>.vocab.<name>_book`), not as a utility kind,
because it transforms nothing.

### D12. Previews are representations, not plugins

A previewer is a manifest plus `preview(ctx) -> representation | None`, matched by
MIME, extension, name and **which extractors produced results**, ordered scope
first, then priority, then strength. It returns plain typed data (`media`,
`text`, `table`, `tree`, `listing`, `chart`, `summary`, `composite`, `html`,
`none`) that the client draws with **its own components**: the table, the tree,
highlighted text, and the platform chart (the same component Charts uses, with
"Open in Charts"; a Plotly figure goes through the platform's Plotly translation
and its themed fallback). **Contributed code therefore never runs in the
viewer's page.** `html` exists for rich output nothing else fits: sanitized on
the node and rendered only in a sandboxed iframe without same-origin. Media load
only the item's own content URL; nothing in a preview fetches from elsewhere.

Every contributed previewer must pass the **conformance suite**
(`preview.conformance`): no network, bounded time and memory, a valid
representation (sanitized `html`, named columns, series and axes, alt text,
titles), theme tokens rather than raw colours, and an injection fixture that
must come back valid or declined, never raised. A previewer that fails at
runtime is skipped for the next. When nothing can show a file the answer is
`none`: the client shows the file's identity (name, size, type, checksum, what
the extractors found), Download, Open with, and "Contribute a previewer for this
type", linking to the guide.

### D13. Bound and local

Local installs run everything on the machine. A bound install relays every call,
resumable chunks and the status stream included, to the node that holds the
library (ADR-212); extraction, routing, the Book and previews run there, with
the node's registry. A laptop's own extensions do not extract for the node: what
a site's Library does is what the site has installed, which `GET /extractors`
and `GET /previewers` state.

### D14. Security

The existing guards stand (basename-only names; an allowlist; signature checks;
zip-bomb measurement; the optional scanner, now streamed from disk). Contributed
types can extend the allowlist but never redefine a built-in type, and SVG,
HTML and script types stay refused whoever declares them. XML is parsed without
entity expansion. Pillow's decompression-bomb limit applies to images. Archive
limits are D6. Process isolation bounds memory for heavy or untrusted
extractors. Previews are D12.

## Packaging an existing product's extractors (the Library's source product)

The Library was ported from SoilMetrix, whose upload handlers (lab reports,
field boundaries, the Modus export) were left behind as domain (spec-library §1).
Under this contract they become one Axiom extension, `soilmetrix-library`,
without changing SoilMetrix:

- each handler becomes an extractor (`smx.extract.lab_report`,
  `smx.extract.field_boundary`, `smx.extract.modus_xml`) whose match rules
  restate what the handler sniffs, whose outputs are its parsed fields
  (`metadata`, `tables` of nutrients, a boundary as an `artifact`) and whose tags
  send nutrients to `dataset` and report text to `rag`;
- the field and nutrient writes become `dataset` routes into its conform lane,
  so the core never learns a nutrient;
- its 19 placement categories become the **"Ag Book"** scheme (`display_name =
  "Ag Book"`), its classifier rules become scheme rules over those extractors'
  outputs, and its existing classifier chain stays registered for the legacy
  taxonomy;
- its Modus and boundary previews become previewers returning `table` and
  `summary` representations (a boundary map would be an `html` representation
  until a map component exists in the client).

The extension declares all of this in one manifest and is tested with the same
conformance suite and `ExtractionContext.for_path` fixtures as any other.

## Consequences

- Any file type is welcome: the core reads the common ones, a pack adds its own,
  and an unknown one is still stored, listed, previewed by its identity, and
  extracted later when a pack arrives (D8).
- Domains add knowledge without trust: their code returns data; the platform
  stores, routes, gates and shows it.
- New catalog tables (`library_jobs`, `library_uploads`, `library_extractions`,
  `library_routes`, `library_folders`, `library_corrections`) and nullable
  columns on `library_items`; expand-only (ADR-182).
- The retrieval document of an item is now the extractors' text (bronze-derived
  `rag.md`), not the file re-read: what retrieval knows is what extraction found,
  with provenance.
- A thread that overruns is abandoned, not killed; heavy or untrusted
  extractors should declare `process`.

## Open questions

1. **Releasing a hold.** Held items wait for a reviewer; the release verb (who
   may release, recorded how) belongs with the export-control review workflow.
2. **Recovery on boot.** `uploads.stuck_jobs_on_boot()` re-queues jobs a restart
   interrupted, and `uploads.sweep()` expires idle sessions; neither is
   scheduled yet (the pulse substrate, ADR-198, is the place).
3. **Member items.** Archive members file with their archive. Whether a member
   should become its own item (its own placement, visibility and preview) is a
   product question.
4. **Process isolation and progress.** A process-isolated extractor cannot report
   progress yet; the job shows the stage running.
5. **The controlled-code list is data.** The domain pack declares it; which
   simulation codes belong on it is the export-control office's call, not the
   platform's. The mechanism holds any code a pack lists.
6. **Large dataset outputs.** A result's tables and records are held in memory;
   an extractor producing millions of readings should write a derived file and
   return a reference. A streamed table output is the follow-up.
7. **Scheme precedence margins** (0.05, 0.35, 0.6) are first values; tune them
   from the corrections people make.
