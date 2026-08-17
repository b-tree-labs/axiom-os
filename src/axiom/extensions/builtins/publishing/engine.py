# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Publisher core workflow engine — provider-agnostic orchestration.

This module NEVER imports any specific provider. It works exclusively
through the Provider ABCs, creating instances via PublisherFactory.

Workflow: load config -> create providers via factory -> generate artifact
-> rewrite links via registry -> upload via storage -> update state -> notify
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.hash_utils import FULL, fingerprint_file

from .config import PublisherConfig, _state_dir, load_config
from .factory import PublisherFactory
from .git_integration import check_branch_policy, get_git_context, remote_url_to_web_url
from .providers.base import (
    GenerationOptions,
    GenerationProvider,
    NotificationProvider,
    StorageProvider,
)
from .registry import LinkRegistry
from .state import DocumentState, LinkEntry, PublicationRecord, StateStore


def _non_clobbering_path(target: Path) -> Path:
    """If ``target`` already exists, append ``" (1)"``, ``" (2)"``, ...
    to the stem until a free slot is found — Finder-style.

    PRESS uses this so repeated ``axi pub generate <file>`` runs preserve
    history side-by-side rather than overwriting. The bare name is always
    preferred when free; suffixed forms only appear after a collision.
    """
    if not target.exists():
        return target
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _find_source_filesystem_scope(start: Path) -> Path | None:
    """Resolve the source document's filesystem scope.

    Strategy (in order):

    1. Ask git: ``git rev-parse --show-toplevel`` from the source's parent
       dir. Correctly handles worktrees (returns the worktree's own root,
       not the bare/main repo).
    2. Fall back to walking up for a ``.git`` file or dir. Matches the
       worktree case explicitly (``.git`` is a file pointing at the main
       repo's ``.git/worktrees/<name>/``).
    3. Return None when no git scope is found (e.g. a loose .md in /tmp);
       caller decides what to do.

    Used by ``PublisherEngine.generate`` so PRESS writes generated output
    into the source doc's repo/worktree rather than into whichever repo
    PRESS happens to be running from.
    """
    import subprocess

    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent

    # 1. Git authoritative answer (cheapest correct path for worktrees).
    try:
        proc = subprocess.run(
            ["git", "-C", str(cur), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        if proc.returncode == 0:
            root = Path(proc.stdout.strip())
            if root.exists():
                return root
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Walk for .git (handles offline / no-git-binary case).
    for ancestor in (cur, *cur.parents):
        if (ancestor / ".git").exists():
            return ancestor

    return None


class PublisherEngine:
    """Core workflow engine — orchestrates providers through ABCs only."""

    def __init__(self, config: PublisherConfig | None = None):
        if config is None:
            config = load_config()
        self.config = config

        # Ensure provider registration by importing providers package
        try:
            import axiom.extensions.builtins.publishing.providers  # noqa: F401
        except ImportError:
            pass

        # State and registry paths — use .neut/ subdir when outside a git repo
        state_root = _state_dir(config.repo_root)
        self.registry = LinkRegistry(state_root / "publisher-registry.json")
        self.state_store = StateStore(state_root / "publisher-state.json")

    # Map user-facing --format/--to values to generation provider names.
    _FORMAT_PROVIDERS = {
        "docx": "pandoc-docx",
        "pdf": "pandoc-pdf",
        # Compile an existing LaTeX project (.tex) to PDF via Tectonic — distinct
        # from "pdf" (markdown → branded PDF). See ADR-090.
        "latex": "latex-pdf",
        "tex": "latex-pdf",
    }

    def _create_generation_provider(
        self, provider_override: str | None = None
    ) -> GenerationProvider:
        """Create the configured (or overridden) generation provider.

        ``provider_override`` accepts either a registered provider name
        (e.g. ``pandoc-pdf``) or a user-facing format (``pdf`` / ``docx``).
        """
        name = self.config.generation.provider
        settings = self.config.generation.settings
        if provider_override:
            name = self._FORMAT_PROVIDERS.get(provider_override, provider_override)
            if name != self.config.generation.provider:
                settings = {}
        return PublisherFactory.create("generation", name, settings)

    def _create_storage_provider(
        self, override: str | None = None
    ) -> StorageProvider:
        """Create the configured (or overridden) storage provider."""
        name = override or self.config.storage.provider
        # Preserve configured settings when override matches configured provider
        if override and override != self.config.storage.provider:
            settings = {}
        else:
            settings = self.config.storage.settings
        return PublisherFactory.create("storage", name, settings)

    def _create_notification_provider(self) -> NotificationProvider:
        """Create the configured notification provider."""
        return PublisherFactory.create(
            "notification",
            self.config.notification.provider,
            self.config.notification.settings,
        )

    def generate(
        self,
        source_path: Path,
        output_dir: Path | None = None,
        options: GenerationOptions | None = None,
        footer_metadata: dict[str, Any] | None = None,
        format_override: str | None = None,
        brand_overrides: dict[str, Any] | None = None,
    ) -> Path:
        """Generate an artifact from a markdown source file.

        This is a local operation — no upload, no state change.

        Args:
            source_path: Path to .md file
            output_dir: Output directory (default: docs/_tools/generated)
            options: Generation options
            footer_metadata: Optional footer metadata (source URL, version, date)
            format_override: Output format / provider (e.g. ``pdf``, ``docx``,
                or a registered provider name). Defaults to configured provider.
            brand_overrides: Generic branding inputs (brand_color, org, logo,
                ...) that override the document's frontmatter for branded
                formats. Ignored by formats that don't brand.
        """
        # A .tex source is an author-owned LaTeX project — compile it with the
        # LaTeX engine (Tectonic) rather than re-typesetting through markdown.
        # The downstream Mermaid/frontmatter pre-processing is a no-op for .tex
        # (no fenced mermaid, no YAML frontmatter), so it passes straight to the
        # latex-pdf provider. Explicit format_override still wins. See ADR-090.
        if format_override is None and source_path.suffix.lower() == ".tex":
            format_override = "latex"

        gen = self._create_generation_provider(format_override)
        ext = gen.get_output_extension()

        if output_dir is None:
            # PRESS follows the SOURCE's filesystem scope: generated
            # output lands in the source's repo/worktree, not in PRESS's
            # repo. Worktrees are handled correctly because git's
            # `rev-parse --show-toplevel` returns the worktree root.
            scope = _find_source_filesystem_scope(source_path) or self.config.repo_root
            output_dir = scope / "docs" / "_tools" / "generated"
            anchor = scope
        else:
            anchor = self.config.repo_root

        # Preserve directory structure relative to docs/, anchored to the
        # scope we chose above (source repo OR PRESS repo when overridden).
        try:
            rel = source_path.relative_to(anchor / "docs")
            output_path = output_dir / rel.with_suffix(ext)
        except ValueError:
            output_path = output_dir / source_path.with_suffix(ext).name

        # Finder-style non-clobbering: if the target exists, append
        # " (1)", " (2)", … until a free slot. Re-runs of `axi pub
        # generate <file>` preserve history side-by-side rather than
        # silently overwriting yesterday's draft.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = _non_clobbering_path(output_path)

        if options is None:
            options = GenerationOptions(
                toc=True,
                toc_depth=3,
            )

        # Add footer metadata if provided
        if footer_metadata:
            options.footer_metadata = footer_metadata

        # Branding overrides flow to branded providers via options.metadata.
        if brand_overrides:
            options.metadata = {**(options.metadata or {}), "brand": brand_overrides}

        # Pre-process Mermaid blocks → PNGs so pandoc renders them as
        # images instead of stripping the fenced code. The renderer
        # prefers local ``mmdc`` (mermaid-cli); fallback to mermaid.ink.
        # Resilient: any rendering failure logs + degrades to the
        # original (un-rendered) markdown so the docx still produces.
        effective_source = source_path
        try:
            md_content = source_path.read_text(encoding="utf-8")
            if "```mermaid" in md_content:
                from .mermaid_renderer import render_mermaid_blocks

                mermaid_result = render_mermaid_blocks(
                    md_content, output_path.parent
                )
                processed_md = output_path.parent / f"{source_path.stem}.processed.md"
                processed_md.write_text(
                    mermaid_result.content, encoding="utf-8"
                )
                effective_source = processed_md
                if not mermaid_result.all_succeeded:
                    print(
                        f"  Warning: {mermaid_result.failed}/{mermaid_result.total}"
                        f" Mermaid diagram(s) failed to render; the docx"
                        f" will embed plain-text fallbacks for them",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001 — pre-process is best-effort
            print(
                f"  Warning: Mermaid pre-render failed ({exc}); generating"
                f" from raw source",
                file=sys.stderr,
            )

        print(f"Rebuilding {output_path.name}...", flush=True)
        result = gen.generate(effective_source, output_path, options)

        if result.warnings:
            for w in result.warnings:
                print(f"  Warning: {w}", file=sys.stderr)

        # Rewrite links if we have a registry
        link_map = self.registry.build_link_map()
        if link_map:
            print(f"  Rewriting {len(link_map)} cross-document links...", flush=True)
            gen.rewrite_links(result.output_path, link_map)

        print(
            f"  Generated: {result.output_path} "
            f"({result.size_bytes / 1024:.1f} KB)",
            flush=True,
        )
        return result.output_path

    def _compute_source_hash(self, source_path: Path) -> str:
        """Compute SHA256 hash of the markdown source file.

        This is more reliable than hashing the artifact because artifacts
        may contain timestamps or other non-deterministic content.
        """
        return fingerprint_file(source_path, length=FULL)

    def _has_artifact_changes(self, source_path: Path, doc_id: str) -> bool:
        """Check if the source document has changed since last publication.

        Returns True if:
        - No previous publication exists
        - Source content differs from last published
        - Cooldown has elapsed since last publish (prevents churn during active editing)

        Returns False if source is identical to last published version,
        or if the doc was published within the cooldown window.
        """
        existing = self.state_store.get(doc_id)
        if not existing or not existing.published:
            return True  # First publish, always changes

        # If we have a previous source hash stored, compare it first.
        # Content changes always win — cooldown only suppresses identical-content republishes.
        if not existing.published.artifact_hash:
            return True  # No hash recorded, assume changed

        current_hash = self._compute_source_hash(source_path)
        if current_hash != existing.published.artifact_hash:
            return True  # Content changed — always publish

        # Content is identical. Apply cooldown: skip if published recently.
        if existing.published.published_at:
            try:
                from datetime import datetime
                published_at = datetime.fromisoformat(existing.published.published_at)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                age_seconds = (datetime.now(UTC) - published_at).total_seconds()

                # Read cooldown from settings (default: 5 minutes)
                cooldown = 300
                try:
                    from axiom.extensions.builtins.settings.store import SettingsStore
                    cooldown = int(SettingsStore().get("publisher.cooldown_seconds", 300))
                except Exception:
                    pass

                if age_seconds < cooldown:
                    return False  # Identical content published recently — skip
            except Exception:
                pass

        return False  # Content unchanged

    def publish(
        self,
        source_path: Path,
        storage_override: str | None = None,
        draft: bool = False,
        force: bool = False,
    ) -> PublicationRecord | None:
        """Full publish workflow: generate + upload + update state.

        Args:
            source_path: Path to .md file.
            storage_override: Override the configured storage provider.
            draft: If True, publish as draft (any branch allowed).
            force: If True, publish even if artifact is unchanged. Always increments version.
        """
        # Git context
        git_ctx = get_git_context(self.config.repo_root)

        # When git is unavailable, skip all branch policy and dirty-tree checks
        if git_ctx.git_available:
            policy = check_branch_policy(
                git_ctx.current_branch,
                self.config.git.publish_branches,
                self.config.git.draft_branches,
            )

            if not draft and policy == "local":
                print(
                    f"Branch '{git_ctx.current_branch}' only allows local generation. "
                    f"Use --draft or switch to a publish branch.",
                    file=sys.stderr,
                )
                return None

            if not draft and policy == "draft":
                print(
                    f"Branch '{git_ctx.current_branch}' only allows draft publishing.",
                    file=sys.stderr,
                )
                draft = True

            # Check for dirty state
            if self.config.git.require_clean and git_ctx.is_dirty:
                print(
                    "Working tree has uncommitted changes. "
                    "Commit or stash before publishing.",
                    file=sys.stderr,
                )
                return None

        # Determine doc_id for state checking
        doc_id = source_path.stem

        # Check for no-op condition (no changes and not --force)
        if not force and not self._has_artifact_changes(source_path, doc_id):
            existing = self.state_store.get(doc_id)
            if existing and existing.published:
                print("[-] No changes detected. Skipping publish (use --force to override)")
                print(f"    Version: {existing.published.version}")
                print(f"    Last published: {existing.published.published_at}")
                return existing.published

        # Determine destination and relative source path
        try:
            rel = source_path.relative_to(self.config.repo_root)
            source_rel = str(rel)
        except ValueError:
            source_rel = source_path.name

        # Compute version BEFORE generating (needed for footer)
        existing = self.state_store.get(doc_id)
        if existing:
            latest_version = "v0"
            if existing.published:
                latest_version = existing.published.version
            if existing.active_draft:
                # Handle both semantic (v2.1.3) and simple (v2) versions
                draft_version_str = existing.active_draft.version.lstrip("v")
                pub_version_str = latest_version.lstrip("v")

                # Compare semantic versions if they contain dots
                if "." in draft_version_str or "." in pub_version_str:
                    draft_parts = [int(x) for x in draft_version_str.split(".")]
                    pub_parts = [int(x) for x in pub_version_str.split(".")]
                    draft_parts += [0] * (3 - len(draft_parts))  # Pad to 3 parts
                    pub_parts += [0] * (3 - len(pub_parts))
                    if draft_parts > pub_parts:
                        latest_version = existing.active_draft.version
                else:
                    draft_num = int(draft_version_str) if draft_version_str else 0
                    pub_num = int(pub_version_str) if pub_version_str else 0
                    if draft_num > pub_num:
                        latest_version = existing.active_draft.version

            # Increment version - handle semantic versions
            version_str = latest_version.lstrip("v")
            if "." in version_str:
                # Semantic version: increment patch
                parts = [int(x) for x in version_str.split(".")]
                parts += [0] * (3 - len(parts))  # Pad to at least 3 parts
                parts[2] += 1  # Increment patch
                version = f"v{parts[0]}.{parts[1]}.{parts[2]}"
            else:
                # Simple version: just increment
                version_num = int(version_str) if version_str else 0
                version_num += 1
                version = f"v{version_num}"
        else:
            version = "v1"

        # Build footer metadata with git source URL
        footer_metadata = {}
        if git_ctx.git_available and git_ctx.remote_url:
            source_url = remote_url_to_web_url(
                git_ctx.remote_url,
                Path(source_rel),
                git_ctx.commit_sha,
            )
            if source_url:
                footer_metadata["source_url"] = source_url

        # Add version and publication date to footer
        now = datetime.now(UTC).isoformat()
        footer_metadata["version"] = version
        footer_metadata["published_at"] = now

        # Generate artifact with footer metadata
        artifact_path = self.generate(source_path, footer_metadata=footer_metadata)

        # Create providers for upload
        storage = self._create_storage_provider(storage_override)
        gen_provider = self._create_generation_provider()

        destination = f"{doc_id}{gen_provider.get_output_extension()}"

        metadata = {
            "version": version,
            "commit_sha": git_ctx.commit_sha,
            "branch": git_ctx.current_branch,
            "source": source_rel,
            "draft": draft,
        }

        print(f"Uploading to {self.config.storage.provider}...", flush=True)
        upload_result = storage.upload(artifact_path, destination, metadata)
        print(f"  Published: {upload_result.canonical_url}", flush=True)

        # Create publication record with source hash for no-op detection
        now = datetime.now(UTC).isoformat()
        source_hash = self._compute_source_hash(source_path)
        record = PublicationRecord(
            storage_id=upload_result.storage_id,
            url=upload_result.canonical_url,
            version=version,
            published_at=now,
            commit_sha=git_ctx.commit_sha,
            generation_provider=self.config.generation.provider,
            storage_provider=storage_override or self.config.storage.provider,
            artifact_hash=source_hash,
        )

        # Update state
        if existing:
            doc_state = existing
        else:
            doc_state = DocumentState(doc_id=doc_id, source_path=source_rel)

        if draft:
            doc_state.status = "draft"
            doc_state.active_draft = record
            doc_state.draft_history.append(record)
        else:
            doc_state.status = "published"
            doc_state.published = record

        doc_state.last_commit = git_ctx.commit_sha
        doc_state.last_branch = git_ctx.current_branch

        self.state_store.update(doc_state)

        # Update link registry
        link_entry = LinkEntry(
            doc_id=doc_id,
            source_path=source_rel,
            published_url=upload_result.canonical_url if not draft else "",
            draft_url=upload_result.canonical_url if draft else None,
            storage_id=upload_result.storage_id,
            last_published=now,
            version=version,
            commit_sha=git_ctx.commit_sha,
        )
        self.registry.update(link_entry)

        # Notify
        try:
            notifier = self._create_notification_provider()
            action = "Draft published" if draft else "Published"
            notifier.send(
                recipients=[],
                subject=f"{action}: {doc_id} ({version})",
                body=f"URL: {upload_result.canonical_url}\nSource: {source_rel}",
            )
        except Exception:
            pass  # Non-fatal

        return record

    def status(self, source_path: Path | None = None) -> list[DocumentState]:
        """Get status of tracked documents."""
        if source_path:
            doc_id = source_path.stem
            doc = self.state_store.get(doc_id)
            return [doc] if doc else []
        return self.state_store.list_by_status()

    def check_links(self) -> dict[str, list[str]]:
        """Verify all cross-document links resolve."""
        return self.registry.check_links(self.config.repo_root / "docs")

    def diff(self) -> list[str]:
        """Show docs changed since last publish."""
        from .git_integration import get_changed_docs

        # Find the earliest commit SHA from published docs
        earliest_sha = None
        for doc in self.state_store.list_by_status():
            if doc.last_commit and (earliest_sha is None):
                earliest_sha = doc.last_commit

        if earliest_sha is None:
            earliest_sha = "HEAD~10"  # Default fallback

        return get_changed_docs(self.config.repo_root, earliest_sha)

    def pull(
        self,
        doc_id: str,
        dry_run: bool = False,
        include_comments: bool = False,
    ) -> dict[str, Any]:
        """Pull a document from external storage and update local .md.

        This is the reverse of publish:
        - Downloads the artifact from storage
        - Extracts text content (and optionally comments)
        - Updates the local .md file (or returns diff if dry_run)

        Args:
            doc_id: Document identifier to pull
            dry_run: If True, show diff without updating local file
            include_comments: If True, extract and return comments

        Returns:
            Dict with keys: changed, source_path, diff, comments
        """
        import difflib
        import tempfile

        # Find the document in state
        doc_state = self.state_store.get(doc_id)
        if not doc_state:
            raise ValueError(f"Document not found: {doc_id}")

        if not doc_state.published and not doc_state.active_draft:
            raise ValueError(f"Document '{doc_id}' has no published version to pull from")

        # Get storage info
        pub_record = doc_state.published or doc_state.active_draft
        assert pub_record is not None  # Guaranteed by check above
        storage_id = pub_record.storage_id
        storage_provider = pub_record.storage_provider

        if not storage_id:
            raise ValueError(f"Document '{doc_id}' has no storage_id")

        # Create storage provider
        storage = self._create_storage_provider(storage_provider)

        # Download to temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / f"{doc_id}.docx"
            storage.download(storage_id, tmp_path)

            # Extract text from downloaded artifact
            external_content = self._extract_text_from_artifact(tmp_path)
            comments = []
            if include_comments:
                comments = self._extract_comments_from_artifact(tmp_path)

        # Read local .md
        source_path = self.config.repo_root / doc_state.source_path
        if source_path.exists():
            local_content = source_path.read_text(encoding="utf-8")
        else:
            local_content = ""

        # Compare
        local_lines = local_content.splitlines(keepends=True)
        external_lines = external_content.splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            local_lines,
            external_lines,
            fromfile=f"local/{doc_state.source_path}",
            tofile=f"external/{doc_id}",
        ))
        diff_text = "".join(diff_lines)
        changed = bool(diff_lines)

        result = {
            "changed": changed,
            "source_path": str(source_path),
            "diff": diff_text if diff_text else None,
            "comments": comments,
        }

        # Update local file if not dry_run and changed
        if changed and not dry_run:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(external_content, encoding="utf-8")

        return result

    def _extract_text_from_artifact(self, artifact_path: Path) -> str:
        """Extract plain text from a generated artifact (.docx, .pdf, etc.)."""
        suffix = artifact_path.suffix.lower()

        if suffix == ".docx":
            try:
                import docx
                doc = docx.Document(str(artifact_path))
                paragraphs = [p.text for p in doc.paragraphs]
                return "\n\n".join(p for p in paragraphs if p.strip())
            except ImportError:
                raise ImportError("python-docx required: pip install python-docx")

        elif suffix == ".md":
            return artifact_path.read_text(encoding="utf-8")

        else:
            raise ValueError(f"Unsupported artifact format: {suffix}")

    def _extract_comments_from_artifact(self, artifact_path: Path) -> list[dict]:
        """Extract comments from an artifact file."""
        suffix = artifact_path.suffix.lower()
        comments = []

        if suffix == ".docx":
            try:
                # python-docx has limited comment support; parse XML directly
                import xml.etree.ElementTree as ET
                from zipfile import ZipFile

                with ZipFile(artifact_path) as zf:
                    if "word/comments.xml" in zf.namelist():
                        comments_xml = zf.read("word/comments.xml")
                        root = ET.fromstring(comments_xml)
                        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

                        for comment in root.findall(".//w:comment", ns):
                            author = comment.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}author", "Unknown")
                            date = comment.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}date", "")
                            text_parts = []
                            for t in comment.findall(".//w:t", ns):
                                if t.text:
                                    text_parts.append(t.text)
                            comments.append({
                                "author": author,
                                "date": date,
                                "text": "".join(text_parts),
                            })
            except Exception:
                pass  # Comment extraction is best-effort

        return comments

    def list_providers(self) -> dict[str, list[str]]:
        """List all registered providers."""
        result = PublisherFactory.available()
        assert isinstance(result, dict)  # No category arg → returns dict
        return result

    def load_manifests(self, folders: list[Path]) -> dict[str, dict[str, Any]]:
        """Load all .publisher.json manifests from specified folders.

        Returns a dict mapping doc_id to manifest entry:
        {
            "doc_id": {
                "source_path": str,
                "published_url": str | None,
                "manifest_path": Path
            }
        }
        """
        result = {}
        for folder in folders:
            manifest_path = folder / ".publisher.json"
            if not manifest_path.exists():
                continue

            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)

                for entry in manifest.get("tracked_files", []):
                    doc_id = entry.get("doc_id")
                    if not doc_id:
                        continue

                    result[doc_id] = {
                        "source_path": entry.get("source_path"),
                        "published_url": entry.get("published_url"),
                        "manifest_path": manifest_path,
                    }
            except Exception as e:
                print(f"Warning: Failed to load manifest {manifest_path}: {e}", file=sys.stderr)

        return result

    def scan_docs(self, folders: list[Path]) -> dict[str, list[str]]:
        """Scan folders for markdown files and compare against manifests.

        Returns dict with keys:
        - "tracked": List of doc_ids found in manifest
        - "untracked": List of .md files not in any manifest
        - "orphaned": List of doc_ids in manifest but not on disk
        """
        # Load manifests
        manifests = self.load_manifests(folders)
        tracked_by_id = {
            doc_id: entry["source_path"]
            for doc_id, entry in manifests.items()
        }

        # Find all .md files in folders
        all_md_files = {}  # Map: filename -> full_path
        for folder in folders:
            if not folder.exists():
                continue
            for md_file in folder.glob("*.md"):
                # Skip README and other non-PRD/spec files
                if md_file.name.startswith("_") or md_file.name == "README.md":
                    continue
                all_md_files[md_file.name] = md_file

        # Find tracked files
        tracked_files = set()
        for doc_id, source_filename in tracked_by_id.items():
            tracked_files.add(source_filename)

        # Find untracked and orphaned
        untracked = []
        for filename, _path in all_md_files.items():
            if filename not in tracked_files:
                untracked.append(filename)

        orphaned = []
        for doc_id, source_filename in tracked_by_id.items():
            found = False
            for folder in folders:
                if (folder / source_filename).exists():
                    found = True
                    break
            if not found:
                orphaned.append(doc_id)

        return {
            "tracked": list(tracked_by_id.keys()),
            "untracked": sorted(untracked),
            "orphaned": sorted(orphaned),
        }

    def onboard_doc(
        self,
        doc_id: str,
        source_path: Path,
        manifest_path: Path,
        published_url: str | None = None,
    ) -> bool:
        """Add a document to a manifest.

        Args:
            doc_id: Identifier for the document
            source_path: Relative path to .md file (from manifest folder)
            manifest_path: Path to .publisher.json file
            published_url: Optional SharePoint URL

        Returns:
            True if successful, False otherwise
        """
        try:
            # Load existing manifest
            if manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
            else:
                manifest = {
                    "publisher_version": "1.0",
                    "tracked_files": []
                }

            # Check if already exists
            for entry in manifest["tracked_files"]:
                if entry.get("doc_id") == doc_id:
                    print(f"Document '{doc_id}' already in manifest")
                    return False

            # Add new entry
            manifest["tracked_files"].append({
                "source_path": source_path.name if isinstance(source_path, Path) else source_path,
                "doc_id": doc_id,
                "published_url": published_url
            })

            # Write back
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)

            print(f"Onboarded: {doc_id}")
            return True
        except Exception as e:
            print(f"Error onboarding '{doc_id}': {e}", file=sys.stderr)
            return False
