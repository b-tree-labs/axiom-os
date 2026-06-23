#!/usr/bin/env python
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ingest an orphan Word document and convert to tracked markdown.

Workflow:
1. Pull .docx from SharePoint by URL or use local file
2. Convert .docx → .md using pandoc
3. Extract media (images, diagrams)
4. Extract comments/feedback
5. Run cleanup on generated markdown
6. Archive source .docx locally
7. Create registry entry
8. Commit all artifacts to git

Usage:
    python first_ingest.py <doc_id> <sharepoint_url>
    python first_ingest.py <doc_id> --local-docx /path/to/file.docx

Example:
    python first_ingest.py experiment-manager-prd https://ut.sharepoint.com/...

"""

from __future__ import annotations

import argparse
import shutil

# Add parent to path for imports
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from ..cleanup import apply_cleanup
from ..converters.pandoc import ConversionOptions, PandocConverter
from ..validation import DocumentValidator


def main():
    parser = argparse.ArgumentParser(
        description="Ingest an orphan Word document as new tracked PRD"
    )
    parser.add_argument("doc_id", help="Document ID (e.g., experiment-manager-prd)")
    parser.add_argument(
        "sharepoint_url",
        nargs="?",
        help="SharePoint URL to the .docx file",
    )
    parser.add_argument(
        "--local-docx",
        type=Path,
        help="Path to local .docx file (alternative to SharePoint URL)",
    )

    args = parser.parse_args()

    repo_root = Path.cwd()
    docs_root = repo_root / "docs" / "prd"
    markdown_path = docs_root / f"{args.doc_id}.md"
    source_dir = docs_root / "_source"
    media_root = docs_root / "media" / args.doc_id

    print(f"📥 First Ingest Workflow: {args.doc_id}")
    print()

    # Step 1: Get source .docx
    print("1️⃣  Acquiring source .docx")

    source_docx: Path | None = None  # assigned below in both branches

    if args.local_docx:
        source_docx = Path(args.local_docx).resolve()
        if not source_docx.exists():
            print(f"❌ Local .docx not found: {source_docx}")
            return 1
        print(f"   ✓ Using local file: {source_docx}")

    elif args.sharepoint_url:
        print(f"   📡 Pulling from SharePoint: {args.sharepoint_url[:60]}...")

        try:
            from ..providers.sharepoint import SharePointProvider

            provider = SharePointProvider()
            download_dir = Path(".neut/downloads")
            download_dir.mkdir(parents=True, exist_ok=True)

            # Pull will try pandoc conversion; if it fails, save .docx
            temp_path = download_dir / f"{args.doc_id}_temp.docx"
            source_docx = provider.pull(args.sharepoint_url, temp_path)

            if source_docx.suffix == ".docx":
                # Pull returned .docx (pandoc not available or conversion skipped)
                print(f"   ✓ Downloaded .docx to {source_docx}")
            else:
                # Pull returned markdown (conversion happened)
                print(f"   ✓ Downloaded and converted to {source_docx}")
                # Extract the .docx that was saved alongside
                source_docx = temp_path

        except ImportError:
            print("   ❌ SharePoint provider not available")
            print("   Install: pip install msal requests")
            return 1
        except Exception as e:
            print(f"   ❌ Download failed: {e}")
            return 1
    else:
        print("❌ Must provide either <sharepoint_url> or --local-docx")
        return 1

    print()

    # Step 2: Convert .docx → .md
    print("2️⃣  Converting .docx → .md")

    try:
        converter = PandocConverter()

        options = ConversionOptions(
            extract_media=True,
            preserve_comments=True,
            media_dir=media_root,
        )

        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        assert source_docx is not None, "source_docx must be set by this point"
        result = converter.convert(
            source_docx,
            markdown_path,
            "docx",
            "gfm",  # GitHub-flavored Markdown
            options=options,
        )

        print(f"   ✓ Generated markdown ({result.size_bytes} bytes)")
        print(f"   ✓ Extracted {len(result.media_extracted)} media files")
        print(f"   ✓ Found {len(result.comments)} comments")

        if result.warnings:
            print("   ⚠️  Pandoc warnings:")
            for warn in result.warnings[:3]:  # Show first 3
                print(f"      - {warn}")

    except Exception as e:
        print(f"❌ Conversion failed: {e}")
        return 1

    print()

    # Step 3: Run cleanup on generated markdown
    print("3️⃣  Cleaning up markdown artifacts")

    try:
        fixes = apply_cleanup(markdown_path)
        if fixes:
            for fix_type, count in fixes.items():
                print(f"   ✓ {fix_type}: {count} fixes")
        else:
            print("   ✓ No cleanup needed")
    except Exception as e:
        print(f"⚠️  Cleanup encountered issues: {e}")

    print()

    # Step 4: Save comments metadata
    print("4️⃣  Saving comments metadata")

    comments_file = docs_root / f".comments-{args.doc_id}.md"

    if result.comments:
        comments_content = f"# Comments: {args.doc_id}\n\n"
        for comment in result.comments:
            comments_content += f"**{comment.get('author', 'Unknown')}** ({comment.get('timestamp', 'date unknown')})\n\n"
            comments_content += f"> {comment.get('text', '(no text)')}\n\n"
            comments_content += f"Status: {'✅ Resolved' if comment.get('resolved') else '⚠️ Pending'}\n\n"
            comments_content += "---\n\n"

        comments_file.write_text(comments_content, encoding="utf-8")
        print(f"   ✓ Saved {len(result.comments)} comments to {comments_file}")
    else:
        print("   ✓ No comments found")

    print()

    # Step 5: Archive source .docx
    print("5️⃣  Archiving source .docx")

    source_dir.mkdir(parents=True, exist_ok=True)
    archived_docx = source_dir / f"{args.doc_id}.docx"

    assert source_docx is not None
    shutil.copy2(source_docx, archived_docx)
    print(f"   ✓ Archived to {archived_docx}")

    print()

    # Step 6: Validate the ingested document
    print("6️⃣  Validating ingested document")

    validator = DocumentValidator(repo_root)
    validation = validator.validate_document(args.doc_id)

    if validation.is_valid:
        print("   ✓ Document is valid")
    else:
        for issue in validation.get_errors():
            print(f"   ❌ {issue.message}")

        for warning in validation.get_warnings():
            print(f"   ⚠️  {warning.message}")
            if warning.suggested_recovery:
                print(f"      → {warning.suggested_recovery}")

    print()

    # Step 7: Suggest next steps
    print("✅ First ingest workflow complete!")
    print()
    print("Next steps:")
    print(f"  1. Review generated markdown: {markdown_path}")
    print(f"  2. Check extracted media: {media_root}")
    if result.comments:
        print(f"  3. Review comments: {comments_file}")
    print("  4. Make any manual fixes (formatting, links, etc.)")
    print("  5. Commit to git: git add docs/prds/ && git commit -m 'publisher: ingest <doc_id>'")

    return 0


if __name__ == "__main__":
    exit(main())
