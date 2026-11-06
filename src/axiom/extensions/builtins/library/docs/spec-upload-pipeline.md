# Library upload pipeline: API and contracts

**Status:** Living (2026-10-28) · **Owner:** library extension · **Decision:** [ADR-213](../../../../../../docs/adrs/adr-213-upload-is-a-composable-extraction-pipeline.md)

This is the reference for clients (the appkit Library and chat), for contributors
(extension and kit authors) and for operators. ADR-213 says why; this says what.

## 1. Stages

`received → stored → identified → extracted → conformed → indexed`

| Stage | What happens | On failure |
|---|---|---|
| received | Bytes arrive: a resumable session (chunks at offsets) or a multipart form. Name, type and size guards run on the session's declaration, before a byte is kept. | Session refused with the guard's reason; nothing is kept. |
| stored | The assembled file is checksum-verified (the client's SHA-256 when it sent one), content guards run (signature, container bombs, the scanner), and it lands in bronze, content-addressed, with its sidecar. The catalog row is written. | The file is not filed; the job says why and how to fix it. |
| identified | The extractor registry plans which extractors take the file (fan-out, priority, supersedes, exclusive). | Never fails: no extractor is a plan with only the universal facts. |
| extracted | Each planned extractor runs, isolated, with its timeout and size limit. Results are kept in bronze (`_derived/<item>/<extractor>@<version>.json`) and in the catalog. | One extractor failing does not stop the others; the stage is `partial`. |
| conformed | Each destination tag goes to its route: `dataset` tables to the conform hook; `input-deck` and `rom-artifact` registrations to their registries; `training-data` and `reference-data` marked. The Data Book files the item. | A route failing is recorded on that route; the file stays in bronze and the job can be retried. |
| indexed | Extracted text, after the export-control hook, is chunked and embedded into the corpus the item's visibility selects. | `pending` (retried) when retrieval is down; `held` when the EC hook holds it. |

A job's `state` is `queued`, `running`, `done`, `partial` (something did not
work, the file is safe), `failed` (it was not stored), `held` (EC hold) or
`cancelled`.

## 2. HTTP (`/api/v1/library`)

### Resumable upload (any size)

```
POST   /uploads                 {file_name, size, sha256?, visibility?, title?, tags?, collection?, folder?, extract_params?}
  201  {upload_id, job_id, offset: 0, chunk_size, max_chunk_bytes, expires_at}
GET    /uploads/{id}            {upload_id, job_id, offset, size, state}
PATCH  /uploads/{id}            body: raw bytes; headers Upload-Offset: <n>, Upload-Checksum: sha256 <hex> (optional, of this chunk)
  200  {offset, complete, job_id}
  409  {offset}                 the server's offset; resend from there
  460  checksum mismatch on the chunk; resend it
DELETE /uploads/{id}            cancel; the partial bytes are removed
```

A client resumes after a dropped connection with `GET /uploads/{id}` and continues
from `offset`. When the last chunk lands the job moves on by itself; the client
watches the job. `sha256` of the whole file, when given, is verified before the
file is stored (mismatch fails `stored` with a remedy).

The multipart `POST /items` remains for small files and the CLI; each filed item
now carries `job_id`.

### Jobs: "what happened to this file"

```
GET  /jobs?ids=a,b&active=1&limit=50   {jobs: [Job]}
GET  /jobs/{id}                         Job
GET  /jobs/stream?ids=a,b               text/event-stream: `event: job` with Job JSON on every change; a comment every 15 s
POST /jobs/{id}/retry                   Job   (re-runs from the failed stage; bytes are already in bronze)
```

```jsonc
// Job
{
  "id": "…", "item_id": "…|null", "upload_id": "…|null",
  "file_name": "run42.tdms", "size": 5368709120, "bytes_received": 2147483648,
  "state": "running", "stage": "received",
  "stages": [{"stage": "received", "status": "running", "started_at": "…", "finished_at": null, "detail": "2.0 of 5.0 GB"}, …],
  "progress": 0.31,               // overall, bytes and stages
  "error": {"stage": "extracted", "message": "…", "remedy": "…"} | null,
  "created_at": "…", "updated_at": "…"
}
```

### An item's pipeline

```
GET  /items/{id}/pipeline      {item, job, extractions[], routes[], ec, placement, events[]}
POST /items/{id}/reextract     {extractors?: [name], stale_only?: bool}  → Job
GET  /items/{id}/preview       a representation (§4) plus {previewer}
GET  /extractors               {extractors: [manifest + available]}
GET  /previewers               {previewers: [manifest]}
```

`extractions[]`: `{extractor, version, scope, status (done|failed|not-yet|unavailable|skipped|timeout), summary, result, tags, duration_ms, error, remedy, current}`.
`routes[]`: `{tag, status (done|held|failed|no-route|pending), target, detail, link: {kind, label, ref, href?}}`.
`ec`: `{verdict (clear|sensitive|held|released), reasons[], code, kind (input|output), extractor, file_kind, release?}`.

### Export-control review (ADR-213 D9)

```
GET  /items/{id}/ec              {verdict, code, kind, file_kind, reasons[], uploader, can_release, can_keep_held,
                                  can_rehold, why_not, reviewers: {role, reviewers[], default_owner}, history[]}
POST /items/{id}/ec/release      {reason}  → the state; the file version is indexed
POST /items/{id}/ec/keep-held    {reason}  → the state; recorded, nothing else changes
POST /items/{id}/ec/re-hold      {reason}  → the state; the file leaves retrieval and is held again
```

Skills: `library.ec_status`, `library.ec_release`, `library.ec_keep_held`,
`library.ec_rehold`; CLI: `axi library ec status|release|keep-held|re-hold <id> --reason "..."`.

- **Who decides.** The per-site `ec-reviewer` role: the site's role assignments
  (`axi attest role grant <handle> <site> ec_reviewer`), the FGA relation
  `ec_reviewer` on `site:<id>` (the site's `owner` satisfies it), or
  `AXIOM_LIBRARY_EC_REVIEWERS`. With nobody named, the node's owner is the
  default reviewer. **The uploader can never release their own file** (403),
  whatever roles they hold. Re-holding is the safe direction: a reviewer or the
  uploader may.
- **A reason is required** for every decision (422 without one), one line.
- **The trail.** Every hold, release, keep-held and re-hold is a row in
  `library_ec_reviews`: a per-site hash chain, each record sealed with the node
  signer, verifiable with `axiom.attest.chain.verify_chain`; and a library event
  (`ec-hold`, `ec-release`, `ec-keep-held`, `ec-re-hold`) on the item. A release
  that cannot be sealed is refused (an unattested release proves nothing).
- **What a release covers.** One file version (`sha256`) for one basis (the
  code, reasons and markers). New bytes (a new version of the item, or a new
  upload) are checked from scratch;
  when an earlier version was released the hold says so. A re-extraction that
  finds a different basis re-holds the file automatically.
- **Stubs count.** An `ec = "payload"` extractor may name `code = "..."` in its
  manifest; when it recognises a file (even as a stub that raises `NotYet`),
  the file is held for that code and filed by it. If unsure, hold.

### The Data Book

```
GET    /book                          {scheme: {name, display_name, version}, folders: [Folder], unsorted: {path, count}}
POST   /book/folders                  {label, parent?}           → Folder (user)
PATCH  /book/folders/{path}           {label?, parent?}          → Folder (rename or move; a scheme folder's rename is an override)
DELETE /book/folders/{path}           an empty user folder only
POST   /items/{id}/move               {folder}                   → item; recorded as a correction
GET    /items?folder=<path>           items filed there (and below with `below=1`)
```

`Folder`: `{path, label, kind (scheme|user|dynamic), parent, count, needs_look}`.
Every item carries `folder`, `folder_labels`, and `placement`
`{confidence, source (correction|user-folder|rule|taxonomy|unsorted|manual), needs_look, explanation}`.

## 3. Contributing an extractor

Declare it in your extension's `axiom-extension.toml` (a kit: in `kit.toml`):

```toml
[[extractors]]
name     = "mypack.extract.widget_log"     # <scope>.extract.<name>
version  = "1.0.0"
summary  = "Widget logs: run id, operator, a table of readings"
entry    = "mypack.extractors:widget_log"  # extract(ctx) -> ExtractionResult
outputs  = ["metadata", "tables", "text"]
tags     = ["dataset", "rag"]
priority = 50
ec       = "screen"                        # screen | payload | none
supersedes = ["core.extract.text"]
[extractors.match]
extensions = [".wlog"]
sniff      = ["^WIDGET-LOG v\\d"]
[extractors.limits]
max_bytes = 2_000_000_000
timeout_s = 120
[[extractors.accepts]]
extension = ".wlog"
sniff     = "text"
```

The implementation (`axiom.extensions.builtins.library.extract.contract`):

```python
from axiom.extensions.builtins.library.extract.contract import ExtractionContext, ExtractionResult, Table, TextSection

def widget_log(ctx: ExtractionContext) -> ExtractionResult:
    lines = list(ctx.iter_lines())
    ...
    return ExtractionResult(summary="Widget log: 1,204 readings", metadata={"run": "42"},
                            tables=[Table("readings", ["ts", "value"], rows[:50], row_count=len(rows))],
                            text=[TextSection(header_notes, title="Notes")])
```

`ctx.sibling("channels*.csv")` finds a companion file uploaded in the same archive;
`ctx.params` holds the uploader's `extract_params`. `drop_tags=["rag"]` on a result
removes a manifest tag. A route handler returns `None` for output that is not its own.

Raise `NotYet("…", tracking="…")` for a recognised format you do not handle yet,
`ExtractorUnavailable("pip install …")` for a missing optional dependency. Test it
with `ExtractionContext.for_path(fixture)`.

## 4. Contributing a previewer

See [Build a previewer](guide-build-a-previewer.md).

## 5. Contributing a Data Book scheme

```toml
[[book_schemes]]
file = "book/my-book.toml"
```

```toml
[scheme]
name = "widgets"
display_name = "Widget Book"
version = "1.0.0"

[[categories]]
path = "logs"
label = "Logs"
[[categories]]
path = "logs/by-line"
label = "By line"

[[rules]]
category = "logs/by-line/{metadata.line}"
confidence = 0.85
because = "it is a widget log from line {line}"
when.extractor = ["mypack.extract.widget_log"]
```

Precedence, confidence and the "needs a look" state are ADR-213 D11.

**Extend the generic Book; do not replace it.** Put `extends = "data"` in the
`[scheme]` table. Your categories come first and the generic Book's follow, so a
spreadsheet, an image or an archive your scheme says nothing about still files
under *Spreadsheets*, *Images* or *Archives*, and *Unsorted* is left for items
that are genuinely ambiguous. Your rules may file under a generic category
without redeclaring it, and a category with a generic path replaces its label.
When one of your rules and an inherited rule both match, yours wins unless the
inherited one is surer by more than `specific_margin` (default `0.15`): that is
what "specific beats generic on ties" means. A scheme that extends a base not
installed, or forms a cycle, is refused with a warning and the generic Book
serves. When several product schemes are installed, the most specific (one no
other extends) is active unless `AXIOM_LIBRARY_BOOK` names another.

## 6. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AXIOM_LIBRARY_MAX_UPLOAD_BYTES` | 50 GB | Largest file a resumable session takes. |
| `AXIOM_LIBRARY_CHUNK_BYTES` | 8 MB | Chunk size offered to clients (the most one PATCH carries is 4x). |
| `AXIOM_LIBRARY_UPLOAD_TTL_H` | 48 | An idle session's bytes are removed after this. |
| `AXIOM_LIBRARY_PIPELINE_WORKERS` | 2 | Concurrent pipelines on a node. |
| `AXIOM_LIBRARY_ARCHIVE_DEPTH` / `_MEMBERS` / `_BYTES` | 3 / 2000 / 4 GB | Archive recursion limits. |
| `AXIOM_LIBRARY_BOOK` | the most specific product scheme, else `data` | The active Data Book scheme. |
| `AXIOM_LIBRARY_EC_REVIEWERS` | (none: the node's owner) | Comma-separated handles who hold `ec-reviewer` at this site, beside the role store and FGA. |
