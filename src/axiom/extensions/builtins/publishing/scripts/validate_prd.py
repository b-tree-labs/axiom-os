#!/usr/bin/env python
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Validate a PRD document — check integrity, state, and suggest recovery.

Usage:
    python validate_prd.py [doc_id]
    python validate_prd.py medical-isotope-prd
    python validate_prd.py --all    # Validate all PRDs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from ..validation import DocumentValidator, validate_all_documents


def format_result(result) -> str:
    """Format validation result for display."""
    lines = [
        f"\n{'='*70}",
        f"📋 {result.doc_id}",
        f"{'='*70}",
        f"Status: {result.publication_status.upper()}",
        f"Valid: {'✅ Yes' if result.is_valid else '❌ No'}",
        "",
    ]

    if result.issues:
        lines.append("Issues Found:")
        lines.append("-" * 70)

        # Group by severity
        errors = result.get_errors()
        warnings = result.get_warnings()
        infos = result.get_infos()

        if errors:
            lines.append(f"\n🔴 ERRORS ({len(errors)}):")
            for issue in errors:
                lines.append(f"  • {issue.message}")
                if issue.file_path:
                    lines.append(f"    Path: {issue.file_path}")
                if issue.suggested_recovery:
                    lines.append(f"    → {issue.suggested_recovery}")

        if warnings:
            lines.append(f"\n🟡 WARNINGS ({len(warnings)}):")
            for issue in warnings:
                lines.append(f"  • {issue.message}")
                if issue.file_path:
                    lines.append(f"    Path: {issue.file_path}")
                if issue.suggested_recovery:
                    lines.append(f"    → {issue.suggested_recovery}")

        if infos:
            lines.append(f"\n🔵 INFO ({len(infos)}):")
            for issue in infos:
                lines.append(f"  • {issue.message}")
    else:
        lines.append("✅ No issues found!")

    if result.recovery_steps:
        lines.append("\n" + "-" * 70)
        lines.append("Recovery Path:")
        for step in result.recovery_steps:
            lines.append(f"  {step}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Validate a PRD document for integrity and state"
    )
    parser.add_argument(
        "doc_id",
        nargs="?",
        default="medical-isotope-prd",
        help="Document ID to validate (default: medical-isotope-prd)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all PRDs in docs/prds/",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )

    args = parser.parse_args()

    repo_root = Path.cwd()
    validator = DocumentValidator(repo_root)

    if args.all:
        print("\n🔍 Validating all PRDs in docs/prds/...\n")
        results = validate_all_documents(repo_root)

        valid_count = sum(1 for r in results.values() if r.is_valid)
        total_count = len(results)

        for doc_id, result in sorted(results.items()):
            print(format_result(result))

        print(f"\n{'='*70}")
        print(f"Summary: {valid_count}/{total_count} documents valid")
        print(f"{'='*70}\n")

        return 0 if valid_count == total_count else 1

    else:
        # Validate single document
        print(f"\n🔍 Validating {args.doc_id}...\n")

        result = validator.validate_document(args.doc_id)
        print(format_result(result))

        return 0 if result.is_valid else 1


if __name__ == "__main__":
    sys.exit(main())
