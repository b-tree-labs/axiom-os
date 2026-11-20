# Library PRD

**Owner:** library extension · **Status:** Draft for the owner's review (2026-11-13) · **Last updated:** 2026-11-16

**Design:** spec [spec-library.md](../../src/axiom/extensions/builtins/library/docs/spec-library.md) (browse, file, scope, delete, labels) and [spec-upload-pipeline.md](../../src/axiom/extensions/builtins/library/docs/spec-upload-pipeline.md) (stages, API, contracts). **Decisions:** [ADR-213](../adrs/adr-213-upload-is-a-composable-extraction-pipeline.md) (upload is a composable extraction pipeline), [ADR-212](../adrs/adr-212-a-library-upload-is-filed-on-the-node-that-holds-the-library-and-a-delete-keeps-the-bytes.md) (filed on the node that holds the library; a delete keeps the bytes), [ADR-209](../adrs/adr-209-every-installation-is-a-site.md) (every installation is a site), [ADR-239](../adrs/adr-239-a-persons-book-follows-domain-and-roles-and-only-grows.md) (a Book shaped by roles, earmarked). **Access:** every rule about who may see or change a file is in [prd-sharing-and-access.md](prd-sharing-and-access.md) (SHR-R1 to R10), decided by [ADR-263](../adrs/adr-263-documents-and-agents-are-shared-by-drive-roles-an-agent-adds-invoker-and-a-run-acts-with-the-lesser-authority.md) (Accepted); this PRD points to it and does not restate it. **Parity:** the appkit inventory [library-parity.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/library-parity.md) (owner's rule, 2026-11-13). **As-built check:** [library-check-2026-11-13](../working/library-check-2026-11-13.md).

Sections marked "as built" cite the merged pull request. The three decisions left open earlier were decided on 2026-11-16 (section 8).

This PRD states what the generic Library must do and why. It was a stub; this draft
writes down the requirements the code already implements, marks the few it does not
yet meet, and separates what a product configures. It describes the platform's
Library, never one product's.

## 1. What it is

The **Library** is the rail item where a site's files live: the one home of a file.
People drop in documents, spreadsheets, images, data and archives; each is kept exactly
as it came in, filed in a classification tree, made searchable by what it says, and
kept or deleted under rules the site can show an auditor.

**Terms.** A **Book** is the Library's classification tree: the **Data Book**
generically, a product's own named Book when a product extends it. A **folder** is a
place in the Book (a scheme folder from the Book, or a person's own). An **item** is
one filed file, with its versions. **Bronze** is where the bytes are kept, unchanged.

## 2. Why

Files that matter (procedures, results, reports, data) otherwise live in shared drives,
detached from the work that produced them, unsearchable by content, and deleted by
whoever has the drive. The Library keeps them on the site's own node, lets people and
agents find them by what they say, and makes keeping and deleting a recorded,
rule-bound act.

## 3. Who uses it

- **A person at a site:** uploads, finds, previews, files, shares with the site or keeps private, deletes and restores their own files.
- **A reviewer** (the site's export-control reviewer role): releases or keeps held a file the screening held.
- **An agent or a script:** does the same through the skills (CLI, MCP, agent tool), with the person's scope.
- **An operator:** sets the caps, the scanner, the active Book, and reads the record.

## 4. Requirements

Each requirement has an id; "Built" says whether `main` meets it today (issues in the
check note).

### LIB-R2 Upload

| ID | Requirement | Built |
|---|---|---|
| LIB-R2.1 | A file is refused, with a reason a person can act on, before any byte is kept, when its name is unsafe, its type is not accepted (script-bearing types never are), it is empty, or it exceeds the per-file, per-request or resumable cap. | Yes (the refusal wording lists every accepted type: #1958) |
| LIB-R2.2 | The stored bytes must match the file's declared type (signature), an archive is measured before it is opened (no bombs, no unsafe paths), and an optional scanner may refuse it. | Yes |
| LIB-R2.3 | A file of any size up to the resumable cap uploads in chunks, each checksummed, and resumes from the server's offset after a dropped connection; an abandoned upload's bytes are removed after an idle period. | Yes (server); the page loses an upload on reload: appkit #210 |
| LIB-R2.4 | Identical bytes the person can already see, at the same visibility, are not filed twice; the person is told it is already there. | Server yes; the page does not say so: appkit #213 |
| LIB-R2.5 | Several files upload at once, and a second upload may start while one runs. | Yes |
| LIB-R2.6 | Every surface that uploads (page, CLI, skill, chat attachment) runs the same pipeline. | Yes |
| LIB-R2.7 | A file with the same name as one already in the folder but different bytes asks **Replace** (a new version), **Keep both** or **Cancel**; nothing is filed until the person answers. | Yes (as built: #1997, parity U7) |

### LIB-R3 What happened to a file

| ID | Requirement | Built |
|---|---|---|
| LIB-R3.1 | Every upload runs received, stored, identified, extracted, conformed, indexed, and the person can see each stage, its state and, on a failure, the reason in words and what to do. | Partly: stages say done only when they did their work (as built: #1995, #1954); raw exceptions still shown (appkit #213) |
| LIB-R3.2 | A file is stored and listed even when an extractor, a route or retrieval fails; one extractor failing never stops the others. | Yes |
| LIB-R3.3 | Re-extract runs the extractors again from the kept bytes, and its result replaces the earlier one, including whether the file is searchable. | Yes (fixed in #1951) |
| LIB-R3.4 | A file waiting for retrieval is indexed when retrieval returns, without anyone acting. | No: #1955 |
| LIB-R3.5 | Every change to an item (upload, change, move, label, copy, version, delete, restore, delete forever, review decision) is recorded with who, when and through which surface, and the item shows that history in words. | Yes (fixed in appkit #208) |

### LIB-R4 Filing in the Book

| ID | Requirement | Built |
|---|---|---|
| LIB-R4.1 | Each file is filed in the active Book with a sentence saying why, a confidence, and a "needs a look" state when no rule is sure. | Yes |
| LIB-R4.2 | A person may move a file to another folder; the move is remembered, teaches later filing, and survives re-extraction. | Yes |
| LIB-R4.3 | People may create their own folders and rename or move them. Deleting a folder that holds things follows [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R4.5 (the folder and what it holds go to Deleted files as one undoable act). | Folders yes; the delete rule is SHR-R4.5 (replaces "a folder that still holds files cannot be deleted", which would lose what the original and the reference file browser do) |
| LIB-R4.4 | Who may move another person's file, and who may rename a scheme folder for everyone, is decided by the permission system, not by being at the site: see [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R4.3 and SHR-R4.4 (decision 10, [ADR-263](../adrs/adr-263-documents-and-agents-are-shared-by-drive-roles-an-agent-adds-invoker-and-a-run-acts-with-the-lesser-authority.md)). | Yes (as built: #2021, closes #1952; sharing stage 1 of 4) |
| LIB-R4.6 | Tidy up proposes a reorganisation of the person's files and applies only what the person accepts (parity F26). | Yes (as built: #1999) |
| LIB-R4.5 | A product extends the generic Book; files its scheme says nothing about still file under the generic categories. | Yes |
| LIB-R1 | A person's Book follows their domain and roles and only grows (below). | Earmarked |

### LIB-R5 Finding and seeing

| ID | Requirement | Built |
|---|---|---|
| LIB-R5.1 | A person finds files by name and by what they say, among the files they may read, and each content hit shows the passage that matched. | Partly: unrelated files and first-chunk snippets: #1956 |
| LIB-R5.2 | Search and retrieval never return a file above the person's clearance, an unlabelled file when labels are required, a sample, or a file the person has no access to ([prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R8.3). | Yes (access filter as built: #2026) |
| LIB-R5.3 | Every accepted type has a preview drawn by the page's own components (text, table, image, PDF, archive listing, chart), and a file with none still shows its facts and a download. | Yes |
| LIB-R5.4 | The common views: Home (frequently used, new, starred), All files, Recent and Starred (per person), Shared with me (what it lists is SHR-R9.2), As uploaded (every file exactly as it came in, with its checksum), Deleted files. | Yes |
| LIB-R5.5 | An item opens in one component in two sizes (quick view beside the list, expanding to full screen), as [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R9.6 defines it; the pipeline detail (what happened to it) is one step inside. This replaces "one drawer", which would lose the full-screen preview and stepping. | No: three surfaces today, appkit #214 |
| LIB-R5.6 | An empty Library says what fills it and offers Upload and two or three starters. | No in practice: appkit #212 |
| LIB-R5.7 | The page works at phone width. | No: appkit #211 |

### LIB-R6 Scope and change

| ID | Requirement | Built |
|---|---|---|
| LIB-R6.1 | An item belongs to one site, taken from the person's credential; a request cannot name another site. Storage is per site. | Yes; a dev node's Library is the lane's even when the machine is bound (as built: #1998, #1959); the page and the CLI can still disagree on the site on a node with no session context: #1953 |
| LIB-R6.2 | Who may read an item is its general access and its shares, as [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R2 and SHR-R1 define (Restricted or Everyone at the site, plus people and groups); changing it moves the item's retrieval as SHR-R2.4 says. | Site or private as built; roles and shares as built in sharing stage 1 (#2018, #2024, #2026) |
| LIB-R6.3 | Who may change, label, delete, restore, re-extract, share or transfer a file is the verb table of [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R1 (least role per verb); no verb is decided by being at the site. | Yes (as built: #2021, #2024) |
| LIB-R6.4 | A copy is a new item of the copier's over the same kept bytes. | Yes |
| LIB-R6.5 | A new version keeps the old ones; restoring an old version makes a new version; every version downloads. | Yes |
| LIB-R6.6 | On a bound install every Library call is served by the site node it is bound to. | Yes |

### LIB-R7 Keeping and deleting

| ID | Requirement | Built |
|---|---|---|
| LIB-R7.1 | Delete takes a file out of retrieval and every listing at once and keeps the bytes; it is recorded before anything changes, and can be undone and restored. | Yes |
| LIB-R7.2 | Delete forever is the owner's, only from Deleted files, refused under an export-control hold, a legal hold, for a lifetime item, and before the retention class's floor; every attempt is recorded, and a record of the deleted file stays. | Partly: the floor and lifetime apply only while `library_labels` is on; decided 2026-11-16 that they always apply (section 8), not yet built |
| LIB-R7.3 | A retention class may lengthen and never shorten; a legal hold ends only when it is released. | Yes |
| LIB-R7.4 | Bytes leave storage only when no other item or version points at them. | Yes |

### LIB-R8 Labels and export-control review

| ID | Requirement | Built |
|---|---|---|
| LIB-R8.1 | Every item shows its export-control label (proposed or confirmed) and its retention class. | Yes |
| LIB-R8.2 | The screening proposes a label; the owner confirms or changes it, at upload or after, on every surface including the page. | Not in the page: appkit #209 |
| LIB-R8.3 | When labels are required, an unlabelled file is kept and listed but not indexed, retrieved or exported, and confirming its label indexes it without re-extracting. | Yes |
| LIB-R8.4 | A file the screening holds is released, kept held or held again only by a reviewer, never by its uploader, with a one-line reason; every decision is sealed in a per-site chain; a release covers one version of the bytes. | Yes |
| LIB-R8.5 | A prompt to act (confirm a label, review a hold) is shown only to the person who can act, with the action. | No: appkit #209 |

### LIB-R9 Every surface

| ID | Requirement | Built |
|---|---|---|
| LIB-R9.1 | Every act a person can take in the page is also a CLI verb, an MCP tool and an agent ability, through one skill, except acts deliberately kept to a person (delete forever: CLI and page only) and a person's own browsing state (stars, recent). The share, access and transfer verbs are specified by [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R3.5. | Partly: #1957; share, access, transfer, request access on every surface (as built: #2024) |
| LIB-R9.2 | The CLI says when a search finds nothing, and a destructive verb asks first. | No: #1957 |

### LIB-R10 The Library keeps what the reference file browser has

| ID | Requirement |
|---|---|
| LIB-R10.1 | The Library keeps every action and interaction pattern listed in the parity inventory ([library-parity.md](https://github.com/b-tree-labs/axiom-appkit/blob/main/docs/design/library-parity.md), the owner's rule of 2026-11-13: the reference file browser's actions and patterns, as the original Library kept them). Sharing differs only as [prd-sharing-and-access.md](prd-sharing-and-access.md) SHR-R9.5 states. Small improvements are allowed; removing or merging an action or pattern is not, unless the owner agrees. |
| LIB-R10.2 | Before a design or a build renames, merges or drops anything in the Library, it is checked against the inventory; a checklist test over the inventory ids is the acceptance (LIB-A21, SHR-A24). |

### LIB-R1 A Book shaped by domain and roles

Earmarked, not scheduled. Decision: [ADR-239](../adrs/adr-239-a-persons-book-follows-domain-and-roles-and-only-grows.md).
**Decided 2026-11-16:** roles may add categories, never access (section 8).

| ID | Requirement |
|---|---|
| LIB-R1.1 | A person's Book is derived from their domain and roles. |
| LIB-R1.2 | A domain's scheme declares which categories belong to which roles. A category with no declared role belongs to everyone in the domain. |
| LIB-R1.3 | Adding a role adds that role's categories to the person's Book. |
| LIB-R1.4 | Removing a role leaves the person's Book unchanged. They can still use those categories and upload to them. |
| LIB-R1.5 | Auto-categorization considers the person's current domain and roles. |
| LIB-R1.6 | Categories only organize files; they are not storage locations. A Book does not grant access. The permission system decides who may read a file. |

## 5. Acceptance

One row per check; each is a test with the id as its marker. "Have" names a test that
already carries it.

| ID | Acceptance | Have |
|---|---|---|
| LIB-A1 | An `.exe`, an `.svg` and an empty file are refused before storage, each with its reason; a file over the per-file cap is refused with a size that is not the cap itself. | test_library_upload |
| LIB-A2 | A 40 MB upload interrupted at half resumes from the server's offset and its checksum verifies. | test_upload_pipeline |
| LIB-A3 | The same bytes uploaded twice make one item, and the second answer says it is already there. | test_library_upload |
| LIB-A4 | A text file, a one-line PDF and a workbook are each searchable by a word inside them after one upload. | test_upload_pipeline, test_extractors |
| LIB-A5 | A file uploaded while retrieval is down becomes searchable when retrieval returns, with no person acting. | new |
| LIB-A6 | A failed extractor leaves the file stored and listed, the job `partial`, and the stage shows the failure, not "done". | partly (test_upload_pipeline) |
| LIB-A7 | Re-extract after a fix makes a file that was not searchable searchable. | test_upload_pipeline |
| LIB-A8 | A move by the owner files the item and survives re-extraction; a move by someone without the permission is refused (SHR-A2). | partly (test_data_book_and_preview); refusal as built: #2021 |
| LIB-A9 | Deleting a folder that holds files moves it and what it holds to Deleted files as one undoable act (SHR-A3); a folder created by one person cannot be deleted by another without the permission (SHR-A2). | partly |
| LIB-A10 | Search returns only files whose content answers the query, within scope and clearance, with the matching passage. | partly |
| LIB-A11 | Another site's file is a 404; a colleague's private file is a 404; a colleague's site file reads but does not change (SHR-A1). | test_library_upload |
| LIB-A12 | Making a file private moves its chunks out of the site corpus. | test_library_upload |
| LIB-A13 | Delete takes a file out of search at once and restore puts it back; delete forever before the floor, under a legal hold or under an export-control hold is refused with the rule named; after, bytes go and the record stays. | test_library_upload, test_library_operations, test_labels_and_retention |
| LIB-A14 | A retention class cannot be shortened; a legal hold ends only with a release. | test_labels_and_retention |
| LIB-A15 | With labels required, an unlabelled upload is not indexed; confirming the label in the page indexes it. | server: test_labels_and_retention; page: new |
| LIB-A16 | The uploader cannot release their own held file; a reviewer's release is sealed and verifiable. | test_ec_review |
| LIB-A17 | Every page act has a CLI verb and an MCP tool on one skill, except delete forever (no MCP) and personal browsing state. | new |
| LIB-A18 | An item opens in one component, quick view and full screen (SHR-R9.6), showing preview, filing, access, label, retention, searchability and activity in words. | new |
| LIB-A19 | An empty Library shows a statement, Upload, and two or three starters. | LabelChips.test (on a Book with no folders only) |
| LIB-A20 | At 390 px every view, the Book tree and a row's menu are reachable without horizontal scroll. | new |
| LIB-A21 | Every parity inventory row the original or the build has is present in the page (a checklist test over the inventory ids). | new (same as SHR-A24) |
| LIB-A22 | A same-name upload with new bytes asks Replace, Keep both or Cancel. | as built: #1997 |

## 6. What a product configures

The platform ships the generic Library; a product sets, without forking:

- **The Book:** its name, its scheme (categories, rules, confidence), extending the generic Data Book.
- **Its vocabulary:** a taxonomy of document types.
- **Extractors, routes and previewers** for its own formats (contributed by manifest).
- **The accepted types** its extractors add, within the platform's safety rules.
- **Caps, the scanner command, the retention classes in use, the reviewer role assignments.**
- **Whether labels are required** (`library_labels`) per environment.
- **Upload starters and import sources** shown on the empty Library.
- **Whether the rail item is on, soon or off** (`library` flag).

A product does not change who may read a file, what is recorded, or how deletion is ruled.

## 7. Non-functional

- Nothing a person uploads runs as script in the browser (content served sandboxed, script-bearing types refused).
- A deploy, update or migration loses no upload in flight (resumable sessions survive a restart).
- Library search answers within the retrieval budget the node already has.
- Every Library write is attributable (who, when, surface) and every deletion is attested.

## 8. Decisions

**Answered by [prd-sharing-and-access.md](prd-sharing-and-access.md) (accepted with [ADR-263](../adrs/adr-263-documents-and-agents-are-shared-by-drive-roles-an-agent-adds-invoker-and-a-run-acts-with-the-lesser-authority.md), 2026-11-13):**

- **Filing and folders (LIB-R4.4, was decision 2).** Moving another person's file needs Editor on it; renaming a scheme category needs the Book manager role, which gives no access to any file (SHR-R4.3, SHR-R4.4; its decision 10, recommended option (a)). Built: #2021.
- **Shared with me (was decision 4).** It lists only items shared with the person or their groups, newest share first, with who shared and when, plus an Owner filter (SHR-R9.2).

**Decided 2026-11-16** by the coordinating session under the owner's standing instruction to decide routine calls with a clear safe default; the owner may reverse any of them:

1. **Retention without labels (LIB-R7.2).** The retention floor and the lifetime rule always apply, whatever `library_labels` says. Delete forever stays the Owner's (SHR-R1). Not yet built: the code still gates the floor on the flag.
2. **CLI parity (LIB-R9.1).** Every page act (copy, versions, folders and zip included) reaches the CLI, MCP and agents through one skill, except delete forever (CLI and page only, no MCP) and a person's own browsing state (stars, recent). The sharing verbs are SHR-R3.5.
3. **LIB-R1 (ADR-239).** Roles may add categories to a person's Book, never access (LIB-R1.3, R1.4, R1.6 as written).

## Note (2026-11-10): an attachment to a signed record is not a Library item

An attachment to a logbook entry belongs to the record that names it: one immutable blob by content hash, served only through the attest door with the entry's labels, retained with the record ([ADR-251](../adrs/adr-251-indexes-and-context-are-labelled-projections-the-record-is-sealed-never-compacted-one-check-at-every-door.md) D6). The Library's extraction pipeline (ADR-213) is reused to parse, OCR or caption it, and the output is a labelled derived index pointing at the record. The Book does not list the attachment and grants no access to it (LIB-R1.6).
