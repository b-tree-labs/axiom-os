#!/usr/bin/env python
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unified publish script handling both first publication and updates.

Workflow:
1. Validate markdown file exists and is valid
2. Detect publication status (draft, published, orphan)
3. Run cleanup on markdown (fixing pandoc artifacts, etc.)
4. Convert markdown → .docx using pandoc
5. Upload .docx to SharePoint via provider (if --no-upload not set)
6. Create or update registry entry
7. Archive source .docx locally for future merges

Usage:
    python publish.py <doc_id> [--provider sharepoint] [--no-upload] [--force]

Examples:
    python publish.py medical-isotope-prd               # Update published doc
    python publish.py experiment-manager-prd --force    # Force re-publication
    python publish.py new-prd --no-upload               # Generate .docx only

Status transitions:
    draft → published (first publication)
    published → published (update with version bump)
    orphan → draft (manual recovery)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess

# Add parent to path for imports
import sys
from datetime import datetime
from pathlib import Path

from axiom.infra.state import LockedJsonFile

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from ..cleanup import apply_cleanup
from ..state import DocumentState, PublicationRecord, StateStore
from ..validation import DocumentValidator


def get_commit_sha() -> str:
    """Get current commit SHA. Fallback to timestamp hash if not in git repo."""
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path.cwd(),
            env=safe_git_env(Path.cwd()),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: use timestamp-based hash when not in git repo
    import hashlib
    timestamp = datetime.now().isoformat()
    return hashlib.md5(timestamp.encode()).hexdigest()[:7]


def count_commits_since(start_commit: str, end_commit: str = "HEAD") -> int:
    """Count commits between two refs (inclusive of end, exclusive of start)."""
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            ["git", "rev-list", f"{start_commit}..{end_commit}", "--"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path.cwd(),
            env=safe_git_env(Path.cwd()),
        )
        if result.returncode == 0:
            return len([line for line in result.stdout.strip().split("\n") if line])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return 0


def parse_version(version_str: str) -> tuple[int, int, int]:
    """Parse 'v1.2.3' → (1, 2, 3). Returns (1, 0, 0) if unparseable."""
    try:
        parts = version_str.lstrip("v").split(".")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return (1, 0, 0)


def get_next_version(doc: DocumentState | None, current_commit: str) -> str:
    """Determine semantic version based on commits since last publication.

    - First publication: v1.0.0
    - 1 commit: patch bump (v1.0.0 → v1.0.1)
    - 2-5 commits: minor bump (v1.0.0 → v1.1.0)
    - 6+ commits: major bump (v1.0.0 → v2.0.0)
    - No commits: keep existing version
    """
    if not doc or not doc.published:
        return "v1.0.0"  # First publication

    prev_commit = doc.published.commit_sha
    if current_commit == prev_commit:
        # No commit change: keep existing version
        return doc.published.version

    # Count commits since last publish
    commit_count = count_commits_since(prev_commit, current_commit)

    # Parse current version
    major, minor, patch = parse_version(doc.published.version)

    # Determine bump type based on commit count
    if commit_count >= 6:
        # Major bump: significant changes
        return f"v{major + 1}.0.0"
    elif commit_count >= 2:
        # Minor bump: moderate changes
        return f"v{major}.{minor + 1}.0"
    else:
        # Patch bump: small changes (1 commit or unknown)
        return f"v{major}.{minor}.{patch + 1}"


def update_markdown_version(markdown_path: Path, status: str, version: str, timestamp: str) -> None:
    """Update the version line in the markdown cover page.

    Replaces: **[status] vX.Y.Z** | Date
    """
    content = markdown_path.read_text(encoding="utf-8")

    # Pattern: **[anything] vX.Y.Z** | anything
    import re
    pattern = r"\*\*\[.*?\].*?\*\*\s*\|.*?$"
    replacement = f"**[{status}] {version}** | {timestamp}"

    updated = re.sub(pattern, replacement, content, count=1, flags=re.MULTILINE)
    markdown_path.write_text(updated, encoding="utf-8")


def update_registry(state_path: Path, doc_id: str, metadata: dict) -> None:
    """Update or create registry entry for document (multi-process safe)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with LockedJsonFile(state_path, exclusive=True) as f:
        state = f.read()
        if not isinstance(state, dict) or "documents" not in state:
            state = {"documents": {}}
        if doc_id not in state["documents"]:
            state["documents"][doc_id] = {}
        state["documents"][doc_id].update(metadata)
        f.write(state)


def main():
    parser = argparse.ArgumentParser(
        description="Publish markdown documents (first publication or updates)"
    )
    parser.add_argument("doc_id", help="Document ID (e.g., medical-isotope-prd)")
    parser.add_argument(
        "--provider",
        default="sharepoint",
        help="Publication provider (default: sharepoint)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip upload, just generate .docx locally",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force publication even if document is orphan or has issues",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".neut/generated"),
        help="Directory for generated artifacts",
    )

    args = parser.parse_args()

    repo_root = Path.cwd()
    docs_root = repo_root / "docs" / "prd"
    markdown_path = docs_root / f"{args.doc_id}.md"
    source_dir = docs_root / "_source"
    state_path = repo_root / ".neut" / "publisher" / "publisher-state.json"

    print(f"🚀 Publish Workflow: {args.doc_id}")
    print()

    # Step 1: Validate markdown exists
    print(f"1️⃣  Validating markdown at {markdown_path}")
    if not markdown_path.exists():
        print(f"❌ Markdown file not found: {markdown_path}")
        return 1

    validator = DocumentValidator(repo_root)
    result = validator.validate_document(args.doc_id)

    publication_status = result.publication_status
    print(f"   ✓ Document status: {publication_status.upper()}")
    print(f"   ✓ File size: {markdown_path.stat().st_size} bytes")

    # Validate status allows publication
    if publication_status == "orphan" and not args.force:
        print("   ⚠️  Document is orphan (not in registry)")
        print("   Use --force to publish anyway, or add to registry manually")
        return 1

    print()

    # Step 2: Run cleanup on markdown
    print("2️⃣  Cleaning up markdown (fixing pandoc artifacts, etc.)")
    try:
        fixes = apply_cleanup(markdown_path)
        if fixes:
            for fix_type, count in fixes.items():
                print(f"   ✓ {fix_type}: {count} fixes")
        else:
            print("   ✓ No cleanup needed (document is clean)")
    except Exception as e:
        print(f"⚠️  Cleanup encountered issues: {e}")

    print()

    # Step 2b: Calculate version and update registry metadata
    print("2b️⃣  Determining version")
    store = StateStore(state_path)
    doc = store.get(args.doc_id)
    current_commit = get_commit_sha()
    version = get_next_version(doc, current_commit)
    now_str = datetime.now().strftime("%B %d, %Y")

    # Update markdown with version before conversion
    update_markdown_version(markdown_path, publication_status, version, now_str)
    print(f"   ✓ Version: {version}")
    print(f"   ✓ Commit: {current_commit}")

    print()

    # Step 3: Convert markdown → .docx
    print("3️⃣  Converting markdown → .docx")

    pandoc = shutil.which("pandoc")
    if not pandoc:
        print("❌ Pandoc not found. Install with: brew install pandoc")
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Include workflow state in filename: state-doc-id.docx (brackets in filename cause issues)
    docx_filename = f"{args.doc_id}.docx"
    docx_path = args.output_dir / docx_filename

    try:
        # Run pandoc from the docs/prds directory so relative image paths resolve
        cmd = [
            pandoc,
            "-f", "markdown",
            "-t", "docx",
            "-o", str(docx_path),
            args.doc_id + ".md"
        ]
        result = subprocess.run(
            cmd,
            cwd=str(docs_root),
            capture_output=True,
            text=True,
            check=True
        )
        print(f"   ✓ Generated {docx_path} ({docx_path.stat().st_size} bytes)")
    except subprocess.CalledProcessError as e:
        print(f"❌ Pandoc conversion failed: {e.stderr}")
        return 1

    print()

    # Step 4: Upload to SharePoint (if --no-upload not set)
    if not args.no_upload:
        print("4️⃣  Uploading to SharePoint")
        print("   (Requires DOCFLOW_CLIENT_ID, DOCFLOW_TENANT_ID env vars)")

        try:
            from ..providers.sharepoint import SharePointProvider

            SharePointProvider()
            # TODO: Implement actual upload
            print("   ℹ️  Upload would push to SharePoint")
            print(f"   (Manual step: upload {docx_path} to SharePoint PRD folder)")
        except ImportError:
            print("   ⚠️  SharePoint provider not available (msal not installed)")
        except Exception as e:
            print(f"   ⚠️  Upload failed: {e}")
            print(f"   📦 .docx saved locally at {docx_path} for manual upload")

    print()

    # Step 5: Archive source .docx locally
    print("5️⃣  Archiving source .docx")
    source_dir.mkdir(parents=True, exist_ok=True)
    archived_docx = source_dir / docx_filename

    shutil.copy2(docx_path, archived_docx)
    print(f"   ✓ Archived to {archived_docx}")

    print()

    # Step 6: Create or update registry entry
    print("6️⃣  Updating registry")

    now = datetime.now().isoformat()

    # Create/update document state
    if not doc:
        doc = DocumentState(
            doc_id=args.doc_id,
            source_path=str(markdown_path.relative_to(repo_root)),
            status="published"
        )
    else:
        doc.status = "published"

    # Create publication record with version and commit
    pub_record = PublicationRecord(
        storage_id=f"{docx_filename}",
        url=str(docx_path),
        version=version,
        published_at=now,
        commit_sha=current_commit,
        generation_provider="pandoc-docx",
        storage_provider="local"
    )

    doc.published = pub_record
    store.update(doc)

    action = "Created" if publication_status == "draft" else "Updated"
    print(f"   ✓ {action} registry entry")
    print(f"      - doc_id: {args.doc_id}")
    print("      - status: published")
    print(f"      - version: {version}")
    print(f"      - commit_sha: {current_commit}")
    print(f"      - published_at: {now}")

    print()

    # Summary
    if publication_status == "draft":
        print("✅ First publish complete!")
        print()
        print("Next steps:")
        print("  1. Verify .docx on SharePoint looks correct")
        print("  2. Share SharePoint link with stakeholders")
        print("  3. Collect feedback in .comments- files")
        print("  4. Update .md and re-publish with: python publish.py " + args.doc_id)
    else:
        print("✅ Publish update complete!")
        print()
        print("Next steps:")
        print("  1. Verify updated .docx on SharePoint")
        print("  2. Notify stakeholders of changes")
        print("  3. Continue with next document: python publish.py <doc_id>")

    return 0


if __name__ == "__main__":
    exit(main())
