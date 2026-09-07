Table of Contents

DocFlow: Document Lifecycle Management System

Version: 0.2.0   Status: Specification (In Development)   Last Updated: February 17, 2026   Owner: Platform Team

Architecture pattern: DocFlow follows the same Factory/Provider pattern
established in the 
§6-8. Each concern is an Extension Point with an abstract Provider contract,
a Factory for instantiation, and facility-specific implementations registered
via configuration. See the 
for the complete mapping.Digital Twin Architecture SpecExtension Point Catalog

1. Executive Summary

DocFlow is a document lifecycle management system that treats markdown (.md) files as source code and published artifacts as deployment outputs. It automates publication, review, feedback incorporation, and embedding pipelines while maintaining a clean Git workflow.

DocFlow is storage-agnostic, format-agnostic, and feedback-agnostic. The core workflow engine (state machine, review periods, link registry, Git integration) knows nothing about OneDrive, Word documents, or Microsoft Graph. These are implementations of Provider contracts that facilities swap based on their environment.

Key Capabilities

• Single-source truth: .md files are the source; published artifacts are generated outputs

• Multi-stage publication: Local → Draft Review → Published → Archived

• Formal review cycles: Review periods with required/optional reviewers, deadline tracking

• Feedback collection: Extract reviewer feedback from published artifacts (format-dependent)

• Cross-document linking: Automatic URL rewriting for internal doc references

• CI/CD native: Git-based workflow, branch-aware publication

• Fully extensible: Factory/Provider pattern for storage, generation, feedback, notifications

• Human-in-the-loop: RACI-based autonomy levels with approval gates

2. Problem Statement

Current Pain Points

• Manual link management — After publishing, internal markdown links break.

   Must manually recreate all hyperlinks in the published format.

• Branch confusion — Multiple Git branches with divergent docs → unclear which

   version is "published" → conflicting URLs for the same document.

• Comment orphaning — Feedback on draft documents isn't tracked when document

   is promoted to published version.

• Scattered review cycle — No formal process for review periods, deadline

   enforcement, or tracking reviewer responses.

• Manual republication — Every doc update requires manual generation, upload,

   and link fixing.

• Environment lock-in — Current publishing workflow assumes OneDrive + Word.

   Facilities using Google Workspace, Nextcloud, S3+HTML, or other stacks    cannot adopt DocFlow without rewriting core logic.

3. Architecture Overview

DocFlow decomposes into five independent concerns, each with its own Provider contract. The core workflow engine orchestrates these providers without knowing their implementations.

┌─────────────────────────────────────────────────────────────────────────┐
│                        DocFlow Architecture                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  SOURCE OF TRUTH          PROVIDER LAYER               CORE ENGINE     │
│  ──────────────          ──────────────               ───────────      │
│                                                                         │
│  docs/**/*.md    ──→  GenerationProvider  ──→  artifacts (.docx, .pdf,  │
│  (Git)                                        .html, .tex)             │
│                                                                         │
│                  ──→  StorageProvider     ──→  Published URLs           │
│                       (OneDrive, GDrive,      (canonical, draft,       │
│                        S3, Nextcloud,          archive)                 │
│                        local filesystem)                                │
│                                                                         │
│                  ──→  FeedbackProvider   ──→  Structured comments      │
│                       (Word comments,         (author, timestamp,      │
│                        GDocs comments,         text, context)          │
│                        GitLab issues,                                   │
│                        email, Hypothes.is)                              │
│                                                                         │
│                  ──→  NotificationProvider ──→ Alerts & reminders      │
│                       (SMTP, Teams, Slack,                              │
│                        terminal, ntfy.sh)                               │
│                                                                         │
│                  ──→  EmbeddingProvider   ──→  RAG vector store        │
│                       (ChromaDB, pgvector,     (search & retrieval)    │
│                        Pinecone)                                        │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    CORE ENGINE (provider-agnostic)               │   │
│  ├──────────────────────────────────────────────────────────────────┤   │
│  │                                                                  │   │
│  │  State Machine:     LOCAL → DRAFT → PUBLISHED → ARCHIVED       │   │
│  │  Link Registry:     doc_id → published URL (storage-dependent)  │   │
│  │  Review Manager:    Deadlines, reviewers, responses             │   │
│  │  Git Integration:   Branch policies, sync detection             │   │
│  │  Autonomy Framework: RACI-based approval gates                  │   │
│  │  Scheduling:         Polling, reminders, deadlines              │   │
│  │                                                                  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

Design Principle: Core Knows Nothing

The core engine never imports or references any specific provider implementation. It works exclusively through the Provider ABCs. This means:

• The state machine transitions documents without knowing if they're .docx or .html

• The link registry resolves URLs without knowing if they're OneDrive or S3

• The review manager tracks deadlines without knowing if feedback comes from

  Word comments or GitLab issues

• A facility running Google Workspace replaces OneDriveStorageProvider +

  DocxFeedbackProvider with GoogleDriveStorageProvider +   GoogleDocsFeedbackProvider — the core engine doesn't change

4. Provider Contracts

4.1 GenerationProvider

Converts markdown source files into publishable artifacts.

class GenerationProvider(ABC):
    """Converts .md source files into publishable artifact format."""

    @abstractmethod
    def generate(self, source_path: Path, output_path: Path,
                 options: GenerationOptions) -> GenerationResult:
        """Generate artifact from markdown source.

        Args:
            source_path: Path to .md file
            output_path: Path for generated artifact
            options: Generation options (TOC, watermark, metadata, etc.)
        Returns:
            GenerationResult with output_path, format, size, warnings
        """
        ...

    @abstractmethod
    def rewrite_links(self, artifact_path: Path,
                      link_map: dict[str, str]) -> None:
        """Rewrite internal document links in the generated artifact.

        Args:
            artifact_path: Path to generated artifact
            link_map: Mapping of relative .md paths → published URLs
        """
        ...

    @abstractmethod
    def get_output_extension(self) -> str:
        """Return the file extension this provider produces
        (e.g., '.docx', '.pdf', '.html')."""
        ...

    @abstractmethod
    def supports_watermark(self) -> bool:
        """Whether this format supports draft watermarks."""
        ...

Implementations:

4.2 StorageProvider

Manages artifact storage, retrieval, sharing, and URL generation.

class StorageProvider(ABC):
    """Manages artifact storage and retrieval."""

    @abstractmethod
    def upload(self, local_path: Path, destination: str,
               metadata: dict) -> UploadResult:
        """Upload artifact to storage.

        Args:
            local_path: Path to local file
            destination: Logical destination path (e.g., "drafts/foo-prd")
            metadata: Document metadata (version, author, commit SHA, etc.)
        Returns:
            UploadResult with storage_id, canonical_url, version
        """
        ...

    @abstractmethod
    def download(self, storage_id: str, local_path: Path) -> Path:
        """Download artifact from storage."""
        ...

    @abstractmethod
    def move(self, storage_id: str, new_destination: str) -> UploadResult:
        """Move artifact (e.g., drafts → published, published → archive)."""
        ...

    @abstractmethod
    def get_canonical_url(self, storage_id: str) -> str:
        """Return the shareable URL for this artifact."""
        ...

    @abstractmethod
    def list_artifacts(self, prefix: str) -> list[StorageEntry]:
        """List artifacts under a logical prefix."""
        ...

    @abstractmethod
    def delete(self, storage_id: str) -> bool:
        """Delete an artifact from storage."""
        ...

Implementations:

4.3 FeedbackProvider

Extracts reviewer comments/feedback from published artifacts or external systems.

class FeedbackProvider(ABC):
    """Extracts reviewer feedback from published artifacts
    or external systems."""

    @abstractmethod
    def fetch_comments(self, artifact_ref: str) -> list[Comment]:
        """Fetch all comments/feedback for an artifact.

        Args:
            artifact_ref: Storage ID, URL, or issue ID depending on provider
        Returns:
            List of Comment objects with author, timestamp, text,
            context, resolved status
        """
        ...

    @abstractmethod
    def supports_inline_comments(self) -> bool:
        """Whether this provider supports comments anchored
        to specific content ranges."""
        ...

    @abstractmethod
    def mark_resolved(self, artifact_ref: str, comment_id: str) -> bool:
        """Mark a comment as resolved (if supported)."""
        ...

Note: Comment is a core data model, not provider-specific:


class Comment:
    comment_id: str
    author: str
    timestamp: datetime
    text: str
    context: str | None     # The text range the comment is anchored to
    resolved: bool
    replies: list['Comment']
    source: str             # Provider name that produced this comment

Implementations:

4.4 NotificationProvider

Sends alerts, reminders, and status updates to stakeholders.

class NotificationProvider(ABC):
    """Sends notifications to stakeholders."""

    @abstractmethod
    def send(self, recipients: list[str], subject: str, body: str,
             urgency: str = "normal") -> bool:
        """Send a notification.

        Args:
            recipients: List of identifiers (email, usernames, channels)
            subject: Notification subject/title
            body: Notification body (markdown supported where applicable)
            urgency: "low", "normal", "high"
        """
        ...

Implementations:

4.5 EmbeddingProvider

Indexes document content for RAG retrieval.

class EmbeddingProvider(ABC):
    """Indexes document content for retrieval-augmented generation."""

    @abstractmethod
    def index_document(self, doc_id: str, content: str,
                       metadata: dict) -> bool:
        """Index a document's content for retrieval."""
        ...

    @abstractmethod
    def search(self, query: str, k: int = 10) -> list[SearchResult]:
        """Search indexed documents."""
        ...

    @abstractmethod
    def remove_document(self, doc_id: str) -> bool:
        """Remove a document from the index."""
        ...

Implementations:

5. Core Engine (Provider-Agnostic)

The core engine contains no provider-specific logic. It orchestrates providers through their ABCs.

5.1 Document Lifecycle State Machine

┌────────────┐
│   LOCAL    │  .md file in Git (any branch)
└────┬───────┘
     │ generate (any branch)
     │ publish --draft (feature branches)
     │ publish (publish branches only)
     ▼
┌────────────────────┐
│  DRAFT REVIEW      │  Uploaded to storage draft location
│  (+ Review Period) │  • Required reviewers assigned
└────┬───────────────┘  • Deadline tracked (default 7 days)
     │                  • FeedbackProvider polls for comments
     │
     │ [all required reviewers responded
     │  OR deadline passes]
     │
     │ promote
     ▼
┌────────────────────┐
│   PUBLISHED        │  Canonical URL in storage
│   (PRODUCTION)     │  • Version numbered (v1.0, v2.0, etc.)
└────┬───────────────┘  • Link registry updated
     │                  • Feedback loop active
     │
     │ [superseded by new version]
     ▼
┌────────────┐
│  ARCHIVED  │  Moved to archive location (read-only)
└────────────┘

The state machine is the same regardless of whether artifacts are .docx on OneDrive, .html on S3, or .pdf on a local NFS share.

5.2 Link Registry

The registry maps document identifiers to their published URLs. URL format is entirely determined by the StorageProvider — the registry just stores whatever get_canonical_url() returns.


class LinkEntry:
    doc_id: str             # e.g., "experiment-manager-prd"
    source_path: str        # e.g., "docs/prds/prd-experiment-manager.md"
    published_url: str      # From StorageProvider.get_canonical_url()
    draft_url: str | None   # If currently in review
    storage_id: str         # Provider-specific reference
    last_published: str     # ISO 8601 timestamp
    version: str            # e.g., "v2.1"
    commit_sha: str         # Git commit at time of publication

Persisted to .doc-registry.json in repo root.

Link rewriting is a collaboration between the registry and the GenerationProvider:

• Registry provides a link_map: dict[str, str] mapping relative .md paths

   to published URLs

• GenerationProvider.rewrite_links() applies the map in the format-specific

   way (Word hyperlinks, HTML hrefs, PDF links, etc.)

5.3 Review Manager

The review manager tracks review periods, reviewer responses, and deadline enforcement. It is provider-agnostic — it calls FeedbackProvider.fetch_comments() and NotificationProvider.send() without knowing the implementations.


class ReviewPeriod:
    review_id: str
    doc_id: str
    started_at: datetime
    ends_at: datetime
    extended_to: datetime | None

    required_reviewers: list[str]
    optional_reviewers: list[str]

    responses: dict[str, ReviewerResponse]
    status: str  # "open", "extended", "closed", "promoted"
    outcome: str | None  # "approved", "approved_with_changes", "needs_revision"

5.4 Git Integration


class GitContext:
    current_branch: str
    commit_sha: str
    is_dirty: bool
    ahead_count: int
    behind_count: int

class SyncStatus(Enum):
    IN_SYNC = "in_sync"
    LOCAL_AHEAD = "local_ahead"      # Local changes not published
    REMOTE_AHEAD = "remote_ahead"    # Feedback not incorporated
    DIVERGED = "diverged"

Branch policies (configurable):

5.5 Autonomy Framework (RACI-Based)

class AutonomyLevel(IntEnum):
    MANUAL = 0          # Human does work
    SUGGEST = 1         # AI proposes, human approves
    CONFIRM = 2         # AI acts after timeout (unless vetoed)
    NOTIFY = 3          # AI acts, human notified after
    AUTONOMOUS = 4      # AI acts silently

Per-action defaults:

6. Document State Model


class DocumentState:
    doc_id: str
    source_path: str

    # Lifecycle
    status: str  # "local", "draft", "published", "archived"

    # Publication records
    published: PublicationRecord | None
    active_draft: PublicationRecord | None
    draft_history: list[PublicationRecord]

    # Git tracking
    last_commit: str
    last_branch: str

    # Review
    active_review: ReviewPeriod | None
    review_history: list[ReviewPeriod]

    # Feedback
    pending_comments: list[Comment]  # Unincorporated

    # Stakeholders
    stakeholders: list[str]


class PublicationRecord:
    storage_id: str
    url: str
    version: str
    published_at: datetime
    commit_sha: str
    generation_provider: str   # Which provider generated this artifact
    storage_provider: str      # Which provider stores this artifact

Note that PublicationRecord tracks which providers were used. This supports mixed environments (e.g., migrating from OneDrive to S3 — old versions reference OneDrive, new versions reference S3).

7. Extension Point Catalog

Following the established consumer-layer pattern from the  §8:Digital Twin Architecture Spec

Industry Adoption Scenarios

Factory Registration

# docflow/factory.py

class DocFlowFactory:
    """Central factory that instantiates providers from configuration."""

    _registries: dict[str, dict[str, type]] = {
        "generation": {},
        "storage": {},
        "feedback": {},
        "notification": {},
        "embedding": {},
    }

    @classmethod
    def register(cls, category: str, name: str, provider_cls: type):
        if category not in cls._registries:
            raise ValueError(f"Unknown provider category: {category}")
        cls._registries[category][name] = provider_cls

    @classmethod
    def create(cls, category: str, name: str, config: dict):
        registry = cls._registries.get(category, {})
        if name not in registry:
            available = list(registry.keys())
            raise ValueError(
                f"Unknown {category} provider: {name}. "
                f"Available: {available}"
            )
        return registry[name](config)

    @classmethod
    def available(cls, category: str) -> list[str]:
        return list(cls._registries.get(category, {}).keys())

Built-in providers register themselves on import:

# docflow/providers/generation/pandoc_docx.py

from docflow.factory import DocFlowFactory
from docflow.providers.base import GenerationProvider

class PandocDocxProvider(GenerationProvider):
    ...

DocFlowFactory.register("generation", "pandoc-docx", PandocDocxProvider)

8. Configuration Schema

# .doc-workflow.yaml (repo root)

# --- Git policies (provider-agnostic) ---
git:
  publish_branches: [main, release/*]
  draft_branches: [feature/*, dev]
  require_clean: true
  require_pushed: true

# --- Provider selection ---
generation:
  provider: pandoc-docx          # or pandoc-pdf, pandoc-html, sphinx, mkdocs
  pandoc-docx:                   # Provider-specific config block
    toc: true
    toc_depth: 3
    reference_doc: null           # Optional template
    mermaid_renderer: mermaid-cli  # or mermaid.ink

storage:
  provider: onedrive             # or google-drive, s3, nextcloud, local
  onedrive:
    client_id: ${MS_GRAPH_CLIENT_ID}
    client_secret: ${MS_GRAPH_CLIENT_SECRET}
    tenant_id: ${MS_GRAPH_TENANT_ID}
    draft_folder: /Documents/Drafts/
    published_folder: /Documents/Published/
    archive_folder: /Documents/Published/Archive/
  # s3:
  #   bucket: example-consumer-docs
  #   prefix: published/
  #   region: us-east-1
  #   public_url_base: https://docs.facility.edu/

feedback:
  provider: docx-comments        # or google-docs, gitlab-issues, email, hypothesis
  docx-comments: {}
  # gitlab-issues:
  #   project_id: 42
  #   label: "doc-review"

notifications:
  provider: terminal             # or smtp, teams, slack, ntfy
  smtp:
    host: smtp.example.org
    from_address: docflow@example.org

embedding:
  enabled: false                 # Opt-in
  provider: chromadb
  chromadb:
    collection: axiom_docs
    persist_directory: .docflow/embeddings

# --- Review defaults (provider-agnostic) ---
review:
  default_days: 7
  reminders:
    - days_before: 3
    - days_before: 1

# --- Autonomy levels (provider-agnostic) ---
autonomy:
  default_level: suggest
  overrides:
    poll_for_feedback: autonomous
    fetch_comments: autonomous
    analyze_feedback: notify
    update_source_file: suggest
    republish_approved_doc: confirm
    republish_new_doc: suggest
    promote_draft: suggest

Environment Profiles

Facilities select a coherent set of providers. Common profiles:

9. CLI Commands

DocFlow is accessed as a subcommand of the neut CLI: neut doc (alias: neut docflow). See docs/specs/spec-neut-cli.md for the full neut command hierarchy.

# --- Publishing ---
neut doc publish docs/prds/prd_foo.md              # Generate + publish
neut doc publish --draft docs/prds/prd_foo.md      # Publish as draft with review period
neut doc publish --all --changed-only          # Batch publish all changed docs
neut doc generate docs/prds/prd_foo.md             # Generate locally only (no upload)

# --- Review management ---
neut doc review list                          # Active reviews
neut doc review extend foo --days 3           # Extend deadline
neut doc review close foo --outcome approved  # Close review
neut doc review comments foo                  # Show extracted feedback
neut doc review incorporate foo               # Apply feedback to .md (gated)
neut doc review promote foo                   # Draft → Published (gated)

# --- Monitoring ---
neut doc status                               # Overall status (all docs)
neut doc status docs/prds/prd_foo.md               # Single doc status
neut doc check-links                          # Verify all cross-doc links resolve
neut doc diff                                 # Show docs changed since last publish

# --- Configuration ---
neut doc init                                 # Create .doc-workflow.yaml interactively
neut doc providers                            # List available providers

10. File Structure

tools/docflow/
  __init__.py
  cli.py                     # CLI entry point
  factory.py                 # DocFlowFactory — central provider registry
  models.py                  # DocumentState, LinkEntry, Comment, PublicationRecord
  config.py                  # Load .doc-workflow.yaml
  engine.py                  # Core workflow engine (provider-agnostic)
  registry.py                # Link registry (.doc-registry.json)
  reviewer.py                # Review period management
  state.py                   # Document state persistence
  git_integration.py         # Branch detection, sync status
  providers/
    __init__.py              # Auto-imports all built-in providers
    base.py                  # All five Provider ABCs
    generation/
      __init__.py
      pandoc_docx.py         # PandocDocxProvider
      pandoc_pdf.py          # PandocPdfProvider (stub — Phase 2)
      pandoc_html.py         # PandocHtmlProvider (stub — Phase 2)
    storage/
      __init__.py
      onedrive.py            # OneDriveStorageProvider
      local.py               # LocalStorageProvider (filesystem)
      s3.py                  # S3StorageProvider (stub — Phase 2)
    feedback/
      __init__.py
      docx_comments.py       # DocxFeedbackProvider (parse word/comments.xml)
      gitlab_issues.py       # GitLabIssueFeedbackProvider (stub — Phase 2)
    notification/
      __init__.py
      terminal.py            # TerminalNotificationProvider
      smtp.py                # SmtpNotificationProvider (stub — Phase 2)
    embedding/
      __init__.py
      chromadb_provider.py   # ChromaDbEmbeddingProvider (stub — Phase 2)

11. Relationship to Existing Code

docs/_tools/publish_to_onedrive.py (existing)

This file contains working logic for:

• Markdown → DOCX generation via pandoc

• OneDrive upload via MS Graph API

• Interactive file selection

• Mermaid rendering

DocFlow does NOT rewrite this from scratch. Instead:

• PandocDocxProvider extracts the pandoc invocation and Mermaid rendering logic

• OneDriveStorageProvider extracts the MS Graph upload logic

• The existing script can continue working independently — DocFlow is additive

tools/infra/gateway.py (shared)

DocFlow's analyze_feedback action (Phase 2) uses the same LLM gateway as Neut Sense. This avoids duplicate LLM configuration.

Neut Sense Integration

DocFlow publishes artifacts. Neut Sense can detect when artifacts receive feedback. Future integration: Neut Sense watches for new comments via the configured FeedbackProvider and creates Signals that flow into the weekly synthesis.

12. CI/CD Integration

# .gitlab-ci.yml

stages:
  - validate
  - preview
  - publish

validate-docs:
  script:
    - neut doc check-links
    - neut doc diff
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

generate-preview:
  script:
    - neut doc generate --changed-only --output artifacts/
  artifacts:
    paths: [artifacts/*]
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

publish-docs:
  script:
    - neut doc publish --all --changed-only
  environment: production
  rules:
    - if: $CI_COMMIT_BRANCH == "main"

13. Implementation Roadmap

Phase 1: MVP (Weeks 1-2)

• [ ] Provider ABCs (providers/base.py) — all five contracts

• [ ] Factory with registration pattern (factory.py)

• [ ] Core models (models.py) — DocumentState, LinkEntry, Comment, PublicationRecord

• [ ] Link registry (registry.py) — .doc-registry.json persistence

• [ ] Git integration (git_integration.py) — branch detection, sync status

• [ ] Document state persistence (state.py) — .doc-state.json

• [ ] PandocDocxProvider — extract generation logic from publish_to_onedrive.py

• [ ] LocalStorageProvider — filesystem storage for testing

• [ ] OneDriveStorageProvider — extract upload logic from publish_to_onedrive.py

• [ ] TerminalNotificationProvider — stdout + macOS notifications

• [ ] Core engine (engine.py) — orchestrate generate → rewrite links → upload

• [ ] CLI basics (cli.py) — publish, generate, status, check-links, providers

• Milestone: docflow publish docs/prds/prd_foo.md generates .docx with rewritten links and uploads to OneDrive. docflow publish --storage local generates to local filesystem. Core never references OneDrive.

Phase 2: Review & Feedback (Weeks 3-4)

• [ ] DocxFeedbackProvider — parse word/comments.xml from .docx ZIP

• [ ] Review manager (reviewer.py) — deadlines, reviewer tracking

• [ ] Draft publication with watermark

• [ ] Promotion workflow (draft → published → archive)

• [ ] SmtpNotificationProvider — email reminders

• [ ] PandocHtmlProvider — HTML generation alternative

• Milestone: Complete review cycle: draft → reviewers comment → extract → incorporate → promote

Phase 3: Extended Providers (Weeks 5-6)

• [ ] S3StorageProvider — SeaweedFS / AWS S3 storage

• [ ] GitLabIssueFeedbackProvider — review via issue comments

• [ ] PandocPdfProvider — PDF generation

• [ ] Autonomy framework — RACI-based approval gates

• Milestone: A non-Microsoft facility can run DocFlow with S3 + GitLab + PDF

Phase 4: Intelligence (Weeks 7-8)

• [ ] ChromaDbEmbeddingProvider — RAG document indexing

• [ ] Feedback analysis via LLM gateway (categorize comments)

• [ ] Scheduling daemon for polling and reminders

• Milestone: Document insights flow to RAG; automated feedback triage

Phase 5: Polish & Extension (Weeks 9-10)

• [ ] GoogleDriveStorageProvider

• [ ] GoogleDocsFeedbackProvider

• [ ] SlackNotificationProvider

• [ ] CLI documentation

• [ ] Unit & integration tests

• [ ] Error handling & recovery

• Milestone: Ready for OSS release; three environment profiles fully supported

14. Open Questions

• Mermaid rendering — Currently uses mermaid.ink (external service).

   Need self-hosted fallback for air-gapped facilities?    GenerationProvider could abstract this.

• Rate limiting — MS Graph, Google APIs have throttling.

   StorageProvider implementations should handle backoff internally.

• Batch operations — Publishing 50+ docs efficiently. StorageProvider

   could support a batch_upload() method with default serial fallback.

• Access control mapping — Document approval status → sharing permissions.

   This is storage-specific and belongs in StorageProvider config, not core.

• Offline mode — Queue actions when no network. Core engine queues;

   StorageProvider flushes on restore. Aligns with neut CLI offline-first.

15. Security Considerations

• Secrets management: Provider credentials use environment variables,

  not YAML values. OS keyring integration for daemon mode.

• Token refresh: StorageProvider implementations handle OAuth token

  lifecycle internally.

• Data classification: StorageProvider config can specify share scope

  (organization, team, public) per publication target.

• Audit logging: All state transitions logged with attribution and

  timestamp in .doc-state.json.

Success Metrics

• [ ] neut doc publish works with at least two StorageProviders (OneDrive + Local)

• [ ] Cross-document links rewritten correctly regardless of storage backend

• [ ] A non-Microsoft facility can configure DocFlow without code changes

• [ ] Review feedback incorporated in < 1 day (vs current multi-day)

• [ ] User reports < 2 minutes per publish cycle (vs current 15-20)

• [ ] Factory pattern verified: swapping provider in YAML changes behavior,

      no core engine changes required

Contributors

• Platform Team — Lead architect

References

•  — Factory/Provider pattern precedent (§6-8)Consumer-Layer Digital Twin Architecture

•  — PhilosophyPragmatic Programmer

• python-docx Documentation

• MS Graph API

• LangGraph

This specification is a living document and will be updated as the system evolves.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
