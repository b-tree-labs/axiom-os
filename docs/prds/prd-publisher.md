# Publisher — Product Requirements Document

> **Implementation Status: 🟡 Partial** — Core document generation (pandoc-docx), storage (local, OneDrive), feedback (docx-comments), notifications (terminal, SMTP), link rewriting, versioning/state, and draft watermarking are shipped. Format inference, endpoint catalog, audience targeting, document types, citations, compilation, reverse ingestion, drift detection, and publisher agent are planned. See the Capability Summary table below for per-feature status.

**Version**: 0.7.0 · **Date**: 2026-03-13 · **Status**: Active
**Spec:** [Publisher Architecture Specification](../tech-specs/spec-publisher.md)

---

## How to Read This Document

Each capability is tagged with an implementation status marker:

| Marker | Meaning |
|--------|---------|
| ✅ | Implemented in the current release |
| 🔲 | Planned — not yet implemented |

---

## What is Publisher?

Publisher is the document lifecycle subsystem of Axiom. It takes markdown source files and carries them through the complete lifecycle of a technical document — from scaffold and draft, through collaborative review, to publication at one or more destinations — and tracks provenance back to the source data the document describes.

Publisher is designed to be universally useful across any organization that produces technical documents. The following are illustrative user types — not an exhaustive list:

- A **researcher** scaffolds a journal paper from experiment notes, manages citations, compiles chapters into a thesis, and submits a PDF.
- An **operations manager** publishes a shift report to a shared team endpoint with a single command.
- A **compliance officer** generates a regulatory submission to a local air-gapped endpoint with no format fallback permitted.
- A **principal investigator** compiles a multi-investigator grant proposal from contributor sections, watermarks the draft for sponsor review, and publishes the final to a partner portal.
- A **technical writer** maintains a living specification across multiple markdown files and publishes versioned HTML to a project wiki.

These roles may exist in operations facilities, national laboratories, pharmaceutical companies, aerospace programs, clinical research organizations, or any other domain where technical documents carry regulatory, scientific, or operational weight. Publisher does not assume the domain — it assumes the workflow.

Key properties: **format-agnostic** (docx, PDF, HTML, LaTeX from the same source), **endpoint-agnostic** (local filesystem, OneDrive, Google Drive, S3, GitHub Wiki, or any pluggable destination), **type-aware** (document types bundle format, template, citation, and audience defaults), **provenance-tracked** (declared data sources are hashed and monitored for changes), and **offline-first** (all state is local; remote publication is a side-effect).

---

## Design Principles

1. **Format-agnostic**: The source of truth is always markdown. The rendered format is a delivery detail, not a document identity.
2. **Endpoint-agnostic**: Publisher does not care where a document lands. Storage backends are pluggable providers; adding a new destination requires only a new provider implementation.
3. **Graceful degradation**: When the preferred format is not supported by a destination endpoint, Publisher falls back to the next best format rather than failing. The user is always informed of any fallback that occurs.
4. **Offline-first**: State and registry are maintained locally. Publication to remote endpoints is a side-effect, not a dependency for local operations.
5. **Audience-aware** *(design intent)*: Documents should be able to declare their intended audience. Audience drives format selection, endpoint routing, notification targeting, and access control. See the Audience System section.
6. **Human-in-the-loop**: Publisher surfaces draft watermarks, review status, and link mismatches before final publication. No document reaches a production endpoint silently.
7. **No lock-in**: The configuration model favors open formats (YAML, JSON, markdown). Provider implementations are swappable. There is no proprietary document format.
8. **Type-driven defaults**: Document types bundle format, template, citation style, and audience hints into named presets. Common document patterns — memo, report, journal paper, regulatory submission — work correctly out of the box without per-document configuration.

---

## Document Lifecycle

Publisher manages five stages of a document's life:

**Scaffold** — `axiom pub draft <title> [--type <type>] [--from-notes <path>] [--llm]` generates a structured markdown file pre-populated with the correct section headings for the declared document type. In file-based mode it extracts key points from notes; in LLM-assisted mode it drafts prose. The scaffold includes a front matter block with `type:`, `title:`, and `draft: true` already set.

**Draft** — The author writes and revises markdown. `axiom pub status` shows what has changed since the last publish, including any declared data sources that have been modified. `axiom pub check-links` validates cross-document references. Draft watermarks are injected automatically on draft-branch publishes. For multi-file documents, `axiom pub compile` assembles sources from a `.compile.yaml` manifest before generation.

**Review** — `axiom pub push --draft` generates and uploads a draft artifact to the configured review endpoint. Reviewers annotate it (e.g., via Word comments). `axiom pub pull` retrieves the annotated artifact. `axiom pub review` extracts reviewer comments and surfaces them in the terminal.

**Publish** — `axiom pub push` selects the best format for the document type and endpoint (via the format inference engine and fallback chain), generates the final artifact, rewrites cross-document links to registry URLs, uploads to the configured storage endpoint, records data source hashes for provenance tracking, updates `.publisher-state.json` and `.publisher-registry.json`, and notifies stakeholders. `axiom pub diff` shows what changed since the previous published version. For web publication, `axiom pub push --provider github-pages` delivers a self-contained HTML artifact (Tier 1) from the same markdown source with no additional toolchain. The `--draft` flag is valid for all destinations.

**Maintain** — `axiom pub status` continuously reports provenance warnings when declared data sources change after publication. `axiom pub overview` summarizes the state of every tracked document in the workspace. The publisher agent (`axiom pub agent scan`) proactively detects drift between remote published documents and their declared local source of truth, surfacing update proposals without requiring the author to initiate the check.

**Full command reference**:

| Command | Purpose |
|---------|---------|
| `axiom pub draft` | Scaffold a new document from a type template and optional source notes |
| `axiom pub push [path] [--draft] [--format] [--provider]` | Generate + upload; respects type, format inference, and fallback chain |
| `axiom pub push --draft` | Push as a watermarked draft for review |
| `axiom pub compile` | Assemble a multi-file document from a `.compile.yaml` manifest |
| `axiom pub generate` | Generate artifact locally without uploading |
| `axiom pub pull` | Retrieve a published artifact from the storage endpoint |
| `axiom pub pull-source <endpoint> [--doc <id>]` | Pull remote document content to local markdown; declare source-of-truth relationship |
| `axiom pub review` | Extract and display reviewer annotations from a pulled artifact |
| `axiom pub status` | Show document state, version, and provenance warnings |
| `axiom pub diff` | Show what changed since the last published version |
| `axiom pub overview` | Dashboard of all document states in the workspace |
| `axiom pub scan` | Scan a directory tree; report tracked, untracked, orphaned, stale |
| `axiom pub check-links` | Verify all cross-document registry links resolve |
| `axiom pub onboard` | Register a new document in the manifest |
| `axiom pub types` | List all registered document types with defaults |
| `axiom pub generators` | List registered providers and their availability status |
| `axiom pub endpoints` | Show endpoint catalog with format support matrix |
| `axiom pub agent scan [--endpoint <name>]` | Scan remote endpoint for drift against declared source relationships |
| `axiom pub agent propose <doc_id>` | Generate update proposal for a drifted document |
| `axiom pub template add <path>` | Import and auto-configure a template |
| `axiom pub template list [--json]` | List all registered templates |
| `axiom pub template validate <path>` | Dry-run template check |

---

## Core Capabilities

### PLT_PUB_001 · Document Generation

Converts markdown source to a rendered artifact suitable for distribution.

| Generator | Status | Notes |
|-----------|--------|-------|
| pandoc-docx | ✅ | Microsoft Word .docx output via pandoc |
| pandoc-pdf | 🔲 | PDF output via pandoc + LaTeX backend |
| pandoc-html | 🔲 | Standalone HTML output via pandoc |
| LaTeX (direct) | 🔲 | Native .tex output for academic / archival use |

Generation is invoked through the `GenerationProvider` interface. Each provider declares the format ID it produces. The engine selects the provider that matches the requested (or inferred) format.

---

### PLT_PUB_002 · Storage Endpoints

Uploads the generated artifact to a configured destination and returns a durable URL. The `↓` column indicates whether reverse ingestion (PLT_PUB_022) is also supported for that endpoint.

**Microsoft Ecosystem**

| Endpoint | Status | Pull | Notes |
|----------|--------|------|-------|
| `local` | ✅ | — | Filesystem copy to a configured output path |
| `onedrive` | ✅ | ✓ | Microsoft OneDrive via Graph API |
| `sharepoint` | 🔲 | ✓ | SharePoint document library via Microsoft Graph |
| `ms-teams` | 🔲 | — | Teams channel files (SharePoint-backed) |
| `azure-blob` | 🔲 | — | Azure Blob Storage |

**Google Workspace**

| Endpoint | Status | Pull | Notes |
|----------|--------|------|-------|
| `google-drive` | 🔲 | ✓ | Google Drive file upload via Drive API v3 |
| `google-docs` | 🔲 | ✓ | Creates/updates a Google Doc; enables native comment annotation |
| `google-sites` | 🔲 | — | Google Sites web publication |

**AWS / Object Storage**

| Endpoint | Status | Pull | Notes |
|----------|--------|------|-------|
| `s3` | 🔲 | — | AWS S3 or S3-compatible (SeaweedFS) |
| `s3-static` | 🔲 | — | S3 static website hosting |

**Git Platforms**

| Endpoint | Status | Pull | Notes |
|----------|--------|------|-------|
| `github-wiki` | 🔲 | ✓ | GitHub Wiki via `.wiki.git` push |
| `github-pages` | 🔲 | — | GitHub Pages via `gh-pages` branch |
| `gitlab-wiki` | 🔲 | ✓ | GitLab Wiki via REST API; primary drift detection target |
| `gitlab-pages` | 🔲 | — | GitLab Pages static site |

**Enterprise Knowledge Platforms**

| Endpoint | Status | Pull | Notes |
|----------|--------|------|-------|
| `confluence` | 🔲 | ✓ | Confluence page via REST API |
| `notion` | 🔲 | ✓ | Notion page via Notion API; supports block-level comments |
| `box` | 🔲 | — | Box enterprise cloud storage |
| `readthedocs` | 🔲 | ✓ | Read the Docs; push triggers webhook build |

Storage is invoked through the `StorageProvider` interface. Each provider self-registers with `PublisherFactory` at import time. New endpoints can be registered interactively via `axiom pub endpoint add` (see PLT_PUB_005).

---

### PLT_PUB_003 · Format-Endpoint Compatibility

🔲 Each storage endpoint declares which document formats it natively accepts via a `supported_formats` property on the `StorageProvider` ABC. Each format declaration carries a `quality_score` (0.0–1.0) reflecting how well the endpoint serves that format — a SharePoint endpoint may technically accept HTML but natively renders DOCX with full fidelity. The engine checks compatibility before generation begins, avoiding wasted work on a format the endpoint cannot serve. Compatibility declarations are backward-compatible: existing providers that do not override `supported_formats` default to `["docx"]`.

---

### PLT_PUB_004 · Graceful Format Fallback

🔲 When the user's preferred format is not supported by the target endpoint, Publisher attempts formats in a configurable chain rather than failing:

```
user preferred → pdf → html → txt
```

The chain is configurable per workspace. Each fallback attempt is logged; the final selection is stored in document state as `format_used` and `fallback_occurred`. When fallback happens, the terminal notification surfaces a clear warning:

```
WARNING  Format fallback occurred
         Requested : pdf
         Used      : html
         Reason    : endpoint 'onedrive' does not support 'pdf'
         Tip       : run `axiom pub endpoints` to find endpoints that support pdf
```

Fallback can be disabled globally (`allow_fallback: false`) or per document type (e.g., `regulatory-submission` sets `allow_format_fallback: false` by default — a regulatory document must be exactly the format required, not a substitute).

---

### PLT_PUB_005 · Endpoint Catalog

🔲 A machine-readable catalog of 19 built-in destinations ships with Publisher, covering local filesystem, the full Microsoft 365 ecosystem (OneDrive, SharePoint, Teams, Azure Blob), Google Workspace (Drive, Docs, Sites), AWS (S3, S3 Static), git platforms (GitHub Wiki, GitHub Pages, GitLab Wiki, GitLab Pages), enterprise knowledge platforms (Confluence, Notion, Box, Read the Docs), and institutional HPC archives. Each entry records endpoint name, kind, supported formats, auth/VPN requirements, and whether reverse ingestion (PLT_PUB_022) is supported.

The catalog is maintained as a community-contributable YAML file (`builtin_catalog.yaml`). Site admins extend it with site-specific entries via `catalog_extensions` in `workflow.yaml`, or interactively via `axiom pub endpoint add` — a CLI wizard that registers a new endpoint and optionally scaffolds a stub `StorageProvider` implementation without any manual config file editing.

`axiom pub endpoints` renders the full format support matrix with an optional `↓pull` column showing reverse ingestion capability. Filter flags: `--format pdf`, `--kind cloud`, `--pull` (pull-capable endpoints only), `--json`.

---

### PLT_PUB_006 · Feedback and Review

Retrieves published artifacts and extracts reviewer annotations.

| Provider | Status | Notes |
|----------|--------|-------|
| docx-comments | ✅ | Extracts Word review comments from .docx artifacts |
| Google Docs comments | 🔲 | Via Google Docs API |
| GitLab MR comments | 🔲 | Inline MR comments on markdown source |

Feedback is surfaced via `axiom pub review` after `axiom pub pull` retrieves the annotated artifact.

---

### PLT_PUB_007 · Notifications

Notifies stakeholders when a document is published or a review is ready.

| Provider | Status | Notes |
|----------|--------|-------|
| terminal | ✅ | Prints summary to stdout |
| smtp | ✅ | Email via configured SMTP relay |
| Microsoft Teams | 🔲 | Webhook or Graph API message |
| Slack | 🔲 | Incoming webhook |
| ntfy | 🔲 | Self-hosted push notification |

---

### PLT_PUB_008 · Cross-Document Link Rewriting

✅ When multiple documents reference one another, Publisher rewrites internal cross-document links to their published destination URLs at generation time. The link rewriting table is sourced from `.publisher-registry.json`, which maps each `doc_id` to its current published URL. This ensures that the rendered artifact contains live, resolvable links regardless of where it is delivered.

---

### PLT_PUB_009 · Audience Targeting

🔲 Documents declare their intended audience in YAML front matter under an `audience:` key specifying `org_scope` (internal / partner / public), `access_tier` (public / internal / restricted / regulatory), `roles`, and `notify_roles`. The `AudienceResolver` evaluates this declaration against the workspace `access_policy` and `audience_contacts` configuration to drive four decisions:

1. **Format selection** — `allow_annotation: true` promotes DOCX for review pushes; `access_tier: regulatory` promotes signed PDF with no fallback.
2. **Endpoint gating** — `access_tier` is checked against `access_policy.allowed_endpoint_kinds` before generation begins; a `regulatory` document cannot reach a `cloud` endpoint.
3. **Notification routing** — `notify_roles` filters recipients to only contacts mapped to declared roles.
4. **RAG corpus routing** — `org_scope` and `access_tier` route the embedded document to the appropriate scoped corpus (PLT_PUB_012).

Precedence: `CLI --audience` > front matter `audience:` > document type `audience_hint` > workspace default (`internal`). See §7 of the Architecture Spec for the full v1 design including `DocumentAudience` dataclass, `AudienceResolver` class, `workflow.yaml` schema, and 9 tests.

---

### PLT_PUB_010 · Versioning and State

✅ Publisher tracks document state in `.publisher-state.json` per workspace. Each publish operation records the source commit SHA, a SHA256 of the rendered artifact, a semantic or simple version number, and the publish timestamp. The no-op detector compares the current source hash against the last recorded hash and skips re-publication when the source is unchanged. Version schemes (semantic: `v1.2.3`; simple: `v1`) are configurable per document.

---

### PLT_PUB_011 · Draft Watermarking

✅ The pandoc-docx generation provider supports injecting a "DRAFT" watermark into rendered .docx artifacts when the document is on a draft branch or when `--draft` is passed explicitly. Watermarks are removed when the document is published to a production endpoint.

Branch policies (`publish_branches`, `draft_branches`, `require_clean`) in `workflow.yaml` control when draft vs. production artifacts are generated.

---

### PLT_PUB_012 · RAG / Embedding Integration

🔲 Embedding support is reserved in the configuration schema (`embedding` provider key in `workflow.yaml`) but is not yet implemented. The intended design is that each published document will also be chunked and embedded into a vector store for retrieval-augmented generation (RAG) workflows, enabling downstream AI tools to query the document corpus.

---

## Audience System Placeholder

> **Status**: Design intent — not yet specified. This section reserves the concept and explains the vision. A full design will appear in a future spec revision.

### What "Audience" Means

In the Publisher model, an *audience* is a declared property of a document that describes who it is intended for: their organizational role, their access tier, and the scope of disclosure (internal, partner-facing, or public). A single document may have multiple audiences — for example, an executive summary intended for leadership and a technical appendix intended for engineers.

### Why Audience Matters for Publishing

The format of a document, the endpoint it is delivered to, and who is notified when it is published are not independent choices — they are all consequences of who the document is for. A report destined for public release should be rendered as HTML or PDF and delivered to a web CDN. The same report prepared for internal review should be a .docx delivered to a SharePoint library so reviewers can annotate it. A regulatory submission should be a signed PDF delivered to a local, air-gapped endpoint with no external network calls. Encoding these decisions in an explicit audience declaration — rather than in ad-hoc configuration scattered across multiple files — is what makes Publisher's behavior reproducible, auditable, and easy to change.

### Vision

The audience system will allow document authors to declare:

- **Roles**: Who should receive and can access the document (e.g., `reviewer`, `approver`, `public`, `regulatory`)
- **Access tier**: The sensitivity level of the content, which maps to allowed storage endpoints
- **Org scope**: `internal`, `partner`, or `public` — controls which external endpoints are permitted
- **Notify on publish**: Whether each audience member or group receives a notification when the document is published

These declarations will feed directly into Publisher's format inference engine (PLT_PUB_003), endpoint selection logic (PLT_PUB_002), notification routing (PLT_PUB_007), and — eventually — access control metadata attached to the uploaded artifact. Maximizing the impact of a document requires knowing who will read it, and the audience system makes that knowledge explicit and machine-readable.

---

## Configuration Reference

Publisher configuration lives at `.axi/publisher/workflow.yaml` relative to the workspace root. The file declares which providers to use for each stage of the pipeline:

```yaml
# .axi/publisher/workflow.yaml
generation:
  provider: pandoc-docx

storage:
  provider: onedrive

feedback:
  provider: docx-comments

notification:
  provider: smtp

embedding:
  provider: null  # not yet implemented

versioning:
  scheme: semantic  # or: simple

branch_policy:
  publish_branches: [main]
  draft_branches: [draft, review]
  require_clean: true
```

Format preferences, fallback chains, and endpoint catalog overrides will be added to this schema when PLT_PUB_003–005 are implemented. See the Publisher Architecture Specification for the full schema additions.

---

### PLT_PUB_013 · Document Types and Templates

🔲 Every document can declare a *type* in its YAML front matter (e.g., `type: journal-paper`). A type bundles a default generation provider, preferred output format, TOC settings, citation style, and audience hint. Publisher ships seven built-in types: `memo`, `spec`, `report`, `journal-paper`, `grant-proposal`, `regulatory-submission`, and `proposal`. Type definitions are loaded in three-layer priority order (built-in → user-global → project), so teams can override defaults without modifying core.

| Built-in Type | Default Format | Key Defaults |
|---|---|---|
| `memo` | docx | No TOC, no citations, internal audience |
| `spec` | docx | TOC depth 3, no citations |
| `report` | docx | TOC depth 3, optional citations |
| `journal-paper` | pdf | Citations required, abstract required, no fallback |
| `grant-proposal` | docx | TOC, compilation mode enabled |
| `regulatory-submission` | pdf | No fallback, strict mode |
| `proposal` | pdf | TOC depth 2, compilation mode, partner audience; PDF preferred with HTML fallback for web publication |

The `axiom pub types` command lists all registered types with their defaults. Reference `.docx` templates are stored in `.axi/publisher/templates/` and referenced by filename in type definitions.

---

### PLT_PUB_014 · Citation and Bibliography Pipeline

🔲 Publisher exposes pandoc's native `--citeproc` citation processing without requiring authors to configure it manually. When a document is published, Publisher searches for a bibliography file in this order: (1) `bibliography:` in front matter, (2) a `.bib` file with the same stem as the source `.md`, (3) `refs.bib` in the same directory, (4) a `bibliography` key in the document type definition. Citation style (CSL) is resolved from front matter `csl:`, the document type, or a workspace default. Three built-in CSL styles ship with Publisher: `ieee`, `apa`, and `chicago-author-date`. If no bibliography is found, `--citeproc` is not passed and publication proceeds normally. All three generation providers (pandoc-docx, pandoc-pdf, pandoc-html) participate in this pipeline.

---

### PLT_PUB_015 · Draft Scaffold from Source Material

🔲 `axiom pub draft <title> [--type <type>] [--from-notes <path>] [--llm]` generates a structured markdown scaffold for a new document. In file-based mode (no LLM required), it creates a front matter block, ordered section headings matching the document type's expected structure, and optional HTML-comment blocks populated with headings and key sentences extracted from a specified notes directory. In LLM-assisted mode (`--llm`), the chat agent is invoked to replace placeholder comments with actual draft prose. The scaffold is written as a ready-to-edit `.md` file. This feature reduces document setup time and ensures all mandatory sections for a given document type are present from the start.

---

### PLT_PUB_016 · Data Source Provenance Tracking

🔲 Authors can declare data dependencies in YAML front matter under a `data-sources:` key. At every successful publish, Publisher records each declared source's SHA256 hash (for files) or modification time (for directories). On subsequent `axiom pub status` calls, Publisher compares current filesystem state against stored records and surfaces any changes as warnings. Modified, deleted, or directory-changed sources are reported with human-readable detail. A `provenance.strict: true` workspace option blocks publish when any declared source has changed since the last publish, ensuring that re-publication happens before new results are shared. This feature is designed for research workflows where published conclusions must be reproducible from declared inputs.

---

### PLT_PUB_017 · PDF and HTML Generation Providers

🔲 `PandocPdfProvider` and `PandocHtmlProvider` complete the PLT_PUB_001 generation matrix. The PDF provider invokes pandoc with `--pdf-engine` (defaulting to `xelatex`; `pdflatex` and `weasyprint` are also supported). The HTML provider invokes pandoc with `--standalone --self-contained` to produce a single portable `.html` file. Both providers implement `is_available() -> bool`, which checks for pandoc and the relevant backend at runtime. The `axiom pub generators` command displays availability status for each registered generation provider. Both providers accept the same citation pipeline arguments as `PandocDocxProvider` (see PLT_PUB_014).

---

### PLT_PUB_018 · Document Compilation

🔲 `axiom pub compile [manifest_path]` assembles a multi-file document from an ordered `.compile.yaml` manifest and publishes it as a single artifact through the existing generation pipeline. The manifest specifies the output name, source files (in order), document type, title, author, section break behavior, and an optional shared bibliography. The `Compiler` pre-processor concatenates the sources into a single temporary markdown file — stripping per-file front matter and replacing it with manifest-level metadata — then passes the combined file to `engine.publish()` unchanged. `axiom pub scan` detects `.compile.yaml` files in scanned directories and reports them as compilable document sets. Compilation mode is compatible with all existing generation, storage, and notification providers.

---

---

### PLT_PUB_019 · Interactive Web Publication

🔲 *(Design intent — partially buildable today)* Publisher's publication model extends from static artifacts (PDF, DOCX) to living, web-native documents. Three tiers define the path:

**Tier 1 — Enhanced HTML**: `PandocHtmlProvider` (PLT_PUB_017) generates a standalone, self-contained `.html` file from the markdown source. Delivered to a `github-pages` or `s3-static` endpoint with no additional toolchain. Buildable today.

**Tier 2 — Static Site**: A `StaticSiteProvider` wraps HTML output in a static site generator (MkDocs, Quarto) and publishes to a hosting endpoint. Multiple documents compose into a single navigable site with search and version history.

**Tier 3 — Full Interactive Experience**: An `ExperienceProvider` ABC (interface reserved in `providers/generation/experience.py`) supports embedded figures from declared data sources, a document-scoped Q&A widget via RAG (PLT_PUB_012), and per-section audience gating.

The key insight: the same markdown source that produces a formal PDF for sponsor review produces a polished website for program communication without additional authoring. Format is a delivery decision, not an authoring decision.

---

### PLT_PUB_020 · CLI and Slash Command Architecture

🔲 *(Design intent — slash command handlers not yet implemented)* Publisher's interaction model is two-layered:

**Full CLI** (`axiom pub <verb>`) is the machine API: deterministic, composable in scripts, `--json` friendly, idempotent. Every publishing operation is reachable as a discrete command (see §18.2 of the spec for the complete verb inventory).

**Slash commands** (`/pub`, `/draft`, `/review`, `/compile`) are the human UX layer, registered with the chat agent. They discover workspace context, present choices, confirm intent, and dispatch the appropriate CLI calls. Users who don't know the CLI flags use slash commands; experienced users and build pipelines use the CLI directly. **Slash commands transparently show the underlying CLI invocations they dispatched**, so the conversational interface teaches the machine API.

| Slash Command | Purpose |
|---------------|---------|
| `/pub [path]` | Publish one document — discovers unpublished changes, confirms, publishes |
| `/draft <title>` | Scaffold a new document — asks type, collects notes, runs `axiom pub draft` |
| `/review [id]` | Pull and surface reviewer annotations — runs `axiom pub pull` + `axiom pub review` |
| `/compile [manifest]` | Assemble multi-file document — shows section status, confirms, runs `axiom pub compile` |

---

### PLT_PUB_021 · Template Onboarding

🔲 *(Design intent)* When a new document template (`.docx` reference doc, CSS stylesheet, or
site theme) is introduced, Publisher should auto-configure itself to support it — not require
manual edits across multiple config files. `axiom pub template add <path>` is the entry point:
it detects the template type, registers it with the appropriate document types, and guides the
user through any configuration choices needed. See §20 of the Architecture Spec for the full
design.

Key properties:
- **Self-describing templates** — Templates carry a `.template-meta.yaml` sidecar (or embedded
  comment block) that declares their name, compatible document types, output formats, and any
  required configuration keys.
- **Auto-registration** — `axiom pub template add` reads the sidecar and writes the template
  into the document type registry and `workflow.yaml` without manual editing.
- **Onboarding wizard** — For templates that require additional setup (e.g., a Quarto site
  theme that needs a `_quarto.yml` project file), the wizard walks through the steps and
  generates starter config files.
- **Template catalog** — `axiom pub template list` shows all registered templates with their
  compatible types and origin (builtin / user-global / project).
- **Endpoint templates** — Not just document templates: a publication endpoint can also have a
  template (e.g., a GitHub Pages layout, a Confluence page template). These are registered via
  the same `axiom pub template add` flow.

| Command | Purpose |
|---------|---------|
| `axiom pub template add <path>` | Import and auto-configure a template |
| `axiom pub template list [--json]` | List all registered templates |
| `axiom pub template remove <name>` | Deregister a template |
| `axiom pub template validate <path>` | Dry-run: check a template without registering it |

---

### PLT_PUB_022 · Reverse Ingestion

🔲 `axiom pub pull-source <endpoint> [--doc <id>]` pulls the current content of a remote document (GitLab wiki page, Confluence page, GitHub wiki entry, etc.) and writes it as a local `.md` file. This is the inverse of `axiom pub push` and enables the Publisher system to take ownership of documents that were authored externally.

During pull-source, the system records a **source-of-truth declaration**: the author declares what local repo path (file or directory) the remote document describes. This declaration is stored in `.publisher-state.json` and is the foundation for all future drift detection. Without a declaration, the agent cannot assert which side of a disagreement is correct.

Three onboarding modes:
- **Declared**: Author specifies the authoritative source path during `axiom pub pull-source` — agent gains full drift detection authority
- **Disputed**: No declaration; agent surfaces disagreements for human resolution and records the outcome
- **Owned**: Document has no repo equivalent (wiki IS the source of truth); the agent tracks it for content changes only

`PullProvider` ABC is the inverse of `StorageProvider` — given an endpoint and document ID, pulls content and returns a local `.md` path. First implementation: `GitLabWikiPullProvider`.

---

### PLT_PUB_023 · Document Drift Detection

🔲 Compares a remote published document against its declared local source of truth and identifies semantic mismatches — claims in the published document that no longer reflect current repo state.

Drift detection is LLM-assisted: the agent reads the pulled document, queries the declared source path (via RAG or direct file read), and identifies specific disagreements as structured `DriftFinding` records: `{claim_in_doc, current_reality, confidence, line_ref}`. It does not assert which side is correct for undeclared documents — it presents the disagreement and asks.

For Publisher-managed documents (those pushed via `axiom pub push` with a recorded state), drift detection is additionally SHA-based: if the source has changed since the last push, the remote document is definitionally stale without requiring LLM comparison.

Three-mode reliability model:
- **Publisher-managed** (most reliable): state file proves derivation; drift is mechanical
- **Declared legacy** (reliable): human-declared relationship; agent enforces it
- **Unknown legacy** (surfacing only): agent shows disagreements without asserting direction

---

### PLT_PUB_024 · Publisher Agent

🔲 A new agent extension (`publisher_agent/`, per the Axiom agent naming convention) that provides autonomous document stewardship. It wraps the Publisher tool's engine and adds the scan→propose→approve→push loop.

Commands: `axiom pub agent scan`, `axiom pub agent propose <doc_id>`, `axiom pub agent review <doc_id>`

The agent does not push updates autonomously — every proposal requires human approval before any remote endpoint is modified. This is the human-in-the-loop principle (Design Principle 6) applied to proactive maintenance.

The "pleasantly surprised author" experience: the agent scans declared relationships, detects that a GitLab wiki page describing a module is out of date, generates a specific targeted update (not a full rewrite), and presents it to the author: "The wiki page for `publisher/` references `axiom pub publish` throughout — the command was renamed to `axiom pub push`. Here's the proposed update. Approve? [Y/n/edit]"

Integration with RAG (PLT_PUB_012): The agent queries the RAG-indexed repo to find current authoritative content when generating proposals, giving PLT_PUB_012 its first concrete use case.

---

## Capability Summary

| ID | Capability | Status | Notes |
|----|-----------|--------|-------|
| PLT_PUB_001 | Document Generation | ✅ / 🔲 | pandoc-docx ✅; pdf, html, LaTeX 🔲 |
| PLT_PUB_002 | Storage Endpoints | ✅ / 🔲 | local, onedrive ✅; others 🔲 |
| PLT_PUB_003 | Format-Endpoint Compatibility | 🔲 | New in v0.4 design |
| PLT_PUB_004 | Graceful Format Fallback | 🔲 | New in v0.4 design |
| PLT_PUB_005 | Endpoint Catalog | 🔲 | New in v0.4 design |
| PLT_PUB_006 | Feedback and Review | ✅ / 🔲 | docx-comments ✅; others 🔲 |
| PLT_PUB_007 | Notifications | ✅ / 🔲 | terminal, smtp ✅; Teams, Slack, ntfy 🔲 |
| PLT_PUB_008 | Cross-Document Link Rewriting | ✅ | Registry-based link rewriting at generation time |
| PLT_PUB_009 | Audience Targeting | 🔲 | v1 designed in §7 spec; AudienceResolver, access_policy, audience_contacts |
| PLT_PUB_010 | Versioning and State | ✅ | SHA-based no-op detection, semantic versioning |
| PLT_PUB_011 | Draft Watermarking | ✅ | pandoc-docx watermark support |
| PLT_PUB_012 | RAG / Embedding Integration | 🔲 | Schema reserved; not implemented |
| PLT_PUB_013 | Document Types and Templates | 🔲 | New in v0.5; 7 built-in types; `proposal` (PDF, partner) vs `grant-proposal` (DOCX, Word review cycle) are distinct types for distinct use cases |
| PLT_PUB_014 | Citation and Bibliography Pipeline | 🔲 | New in v0.5; automatic .bib discovery, 3 built-in CSL styles |
| PLT_PUB_015 | Draft Scaffold from Source Material | 🔲 | New in v0.5; `axiom pub draft`; file-based and LLM-assisted modes |
| PLT_PUB_016 | Data Source Provenance Tracking | 🔲 | New in v0.5; SHA256 tracking, strict mode |
| PLT_PUB_017 | PDF and HTML Generation Providers | 🔲 | New in v0.5; completes PLT_PUB_001 generation matrix |
| PLT_PUB_018 | Document Compilation | 🔲 | New in v0.5; `.compile.yaml` manifest; `axiom pub compile` |
| PLT_PUB_019 | Interactive Web Publication | 🔲 | New in v0.6 design; 3-tier model; Tier 1 buildable with PLT_PUB_017 |
| PLT_PUB_020 | CLI and Slash Command Architecture | 🔲 | New in v0.6 design; `/pub`, `/draft`, `/review`, `/compile` |
| PLT_PUB_021 | Template Onboarding (`axiom pub template`) | 🔲 | New in v0.6 design; self-configuring template import for doc and site templates |
| PLT_PUB_022 | Reverse Ingestion | 🔲 | New in v0.7; PullProvider ABC; source-of-truth declaration at onboarding |
| PLT_PUB_023 | Document Drift Detection | 🔲 | New in v0.7; three-mode reliability model; LLM-assisted semantic comparison |
| PLT_PUB_024 | Publisher Agent | 🔲 | New in v0.7; publisher_agent/ extension; scan→propose→approve→push loop |
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
