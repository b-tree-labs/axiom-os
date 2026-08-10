# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi signal` — program awareness.

User commands (shown in default help):
    axi signal brief                     Catch up on what happened
    axi signal media search <query>      Search recordings
    axi signal draft                     Generate weekly changelog
    axi signal status                    Check pipeline health

Pipeline commands (under `axi signal pipeline`):
    axi signal pipeline ingest --source voice   Process voice memos
    axi signal pipeline review                  Guided correction review
    axi signal pipeline review --transcripts    Transcript correction
    axi signal pipeline suggest --run           LLM signal-to-PRD matching
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC
from difflib import get_close_matches
from pathlib import Path


class SuggestingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that suggests similar commands on typos."""

    def __init__(self, *args, valid_subcommands: list[str] | None = None,
                 custom_help: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._valid_subcommands = valid_subcommands or []
        self._custom_help = custom_help

    def print_help(self, file=None) -> None:
        if self._custom_help:
            print(self._custom_help, file=file or sys.stdout)
        else:
            super().print_help(file)

    def error(self, message: str) -> None:  # type: ignore[override]
        # Check if this is an "invalid choice" error for a subcommand
        match = re.search(r"invalid choice: '([^']+)'", message)
        if match and self._valid_subcommands:
            typo = match.group(1)
            suggestions = get_close_matches(typo, self._valid_subcommands, n=2, cutoff=0.5)
            self.print_usage(sys.stderr)
            err_msg = f"{self.prog}: error: {message}\n"
            if suggestions:
                err_msg += f"\nDid you mean: {' or '.join(suggestions)}?\n"

                # Interactive terminal + single close match → offer to run corrected command
                if (len(suggestions) == 1
                        and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
                        and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
                    sys.stderr.write(err_msg)
                    try:
                        from axiom.setup.renderer import prompt_yn
                        corrected = suggestions[0]
                        if prompt_yn(f"Run `axi signal {corrected}` instead?", default=True):
                            # Re-parse with corrected subcommand
                            fixed_argv = sys.argv[:]
                            # Replace the typo with the corrected command
                            try:
                                idx = fixed_argv.index(typo)
                                fixed_argv[idx] = corrected
                            except ValueError:
                                fixed_argv = ["neut", "sense", corrected]
                            # Re-enter via main with corrected args
                            sys.argv = fixed_argv
                            parser = get_parser()
                            args = parser.parse_args(fixed_argv[2:])  # skip "axi signal"
                            _dispatch(args)
                            self.exit(0)
                    except (KeyboardInterrupt, EOFError):
                        pass  # Ctrl+C / Ctrl+D — fall through to exit
                    except ImportError:
                        pass  # Missing deps — fall through to exit
                    except Exception:
                        pass  # Dispatch failed — fall through to exit

            self.exit(2, err_msg)
        else:
            super().error(message)

# Resolve paths relative to tools/agents/
from axiom import REPO_ROOT as _REPO_ROOT  # noqa: E402

_RUNTIME_DIR = _REPO_ROOT / "runtime"
INBOX_RAW = _RUNTIME_DIR / "inbox" / "raw"
INBOX_PROCESSED = _RUNTIME_DIR / "inbox" / "processed"
INBOX_ARCHIVED = _RUNTIME_DIR / "inbox" / "archived"  # Processed raw files move here
DRAFTS_DIR = _RUNTIME_DIR / "drafts"
EXPORTS_DIR = _RUNTIME_DIR.parent / "exports"

# ASCII art newt with fuel rod tail and emanating neutrons
NEWT_ASCII = r"""
        .  *  .
     *    \|/    *
   .   ----●----   .
     *   /|\   *
        . | .        ___
          |      .-'`   `'-.
    ╔═════╪═════/  ^    ^   \
    ║ ≋≋≋ ╪ ≋≋≋ |    ◡      |
    ╚═════╪═════\   '----'  /
          |      '-._    _.-'
        . | .        `--'
     *   \|/   *
   .   ----●----   .
     *    |    *
        .   .
"""

NEWT_SMALL = "🦎⚛"


def cmd_status(args: argparse.Namespace) -> None:
    """Show what's in inbox, what's processed, what drafts exist."""
    print("axi signal — status")
    print()

    # Inbox raw
    raw_counts: dict[str, int] = {}
    if INBOX_RAW.exists():
        for child in INBOX_RAW.iterdir():
            if child.is_dir():
                files = list(child.rglob("*"))
                file_count = sum(1 for f in files if f.is_file() and f.name != ".gitkeep")
                if file_count:
                    raw_counts[child.name] = file_count
            elif child.is_file() and child.name != ".gitkeep":
                raw_counts.setdefault("root", 0)
                raw_counts["root"] += 1

    if raw_counts:
        print("Inbox (raw):")
        for folder, count in sorted(raw_counts.items()):
            print(f"  {folder}/: {count} file(s)")
    else:
        print("Inbox (raw): empty")

    # Processed
    processed_count = 0
    if INBOX_PROCESSED.exists():
        processed_count = sum(
            1 for f in INBOX_PROCESSED.rglob("*")
            if f.is_file() and f.name != ".gitkeep"
        )
    print(f"Processed: {processed_count} file(s)")

    # Drafts
    draft_count = 0
    latest_draft = None
    if DRAFTS_DIR.exists():
        drafts = sorted(DRAFTS_DIR.glob("changelog_*.md"), reverse=True)
        draft_count = len(drafts)
        if drafts:
            latest_draft = drafts[0].name

    print(f"Drafts: {draft_count} changelog(s)")
    if latest_draft:
        print(f"  Latest: {latest_draft}")

    # GitLab exports
    export_count = 0
    latest_export = None
    if EXPORTS_DIR.exists():
        exports = sorted(EXPORTS_DIR.glob("gitlab_export_*.json"), reverse=True)
        export_count = len(exports)
        if exports:
            latest_export = exports[0].name

    print(f"GitLab exports: {export_count} file(s)")
    if latest_export:
        print(f"  Latest: {latest_export}")

    # Config
    from .correlator import CONFIG_DIR, CONFIG_EXAMPLE_DIR

    config_dir = CONFIG_DIR if CONFIG_DIR.exists() else CONFIG_EXAMPLE_DIR
    people_path = config_dir / "people.md"
    init_path = config_dir / "initiatives.md"
    print(f"\nConfig: {config_dir}")
    print(f"  people.md: {'found' if people_path.exists() else 'missing'}")
    print(f"  initiatives.md: {'found' if init_path.exists() else 'missing'}")

    # Gateway
    from axiom.infra.gateway import Gateway

    gw = Gateway()
    print(f"  LLM gateway: {'available' if gw.available else 'no providers configured'}")


def cmd_ingest(args: argparse.Namespace) -> None:
    """Run extractors on inbox data."""
    from datetime import datetime
    from pathlib import Path

    from axiom.infra.gateway import Gateway

    from .correlator import Correlator
    from .models import Signal

    correlator = Correlator()
    gateway = Gateway()

    source = args.source
    all_signals: list[Signal] = []

    # Handle single file mode
    single_file = None
    if hasattr(args, 'file') and args.file:
        single_file = Path(args.file).expanduser().resolve()
        if not single_file.exists():
            print(f"File not found: {single_file}")
            return
        print(f"Processing single file: {single_file.name}")

    # Parse reprocess-from date
    reprocess_from = None
    if hasattr(args, 'reprocess_from') and args.reprocess_from and not single_file:
        if args.reprocess_from.lower() == 'today':
            reprocess_from = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                reprocess_from = datetime.strptime(args.reprocess_from, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                print(f"Invalid date format: {args.reprocess_from}. Use YYYY-MM-DD or 'today'")
                return
        print(f"Reprocessing files modified since: {reprocess_from.strftime('%Y-%m-%d %H:%M UTC')}")

    if source in ("gitlab", "all"):
        signals = _ingest_gitlab(correlator)
        all_signals.extend(signals)

    if source in ("voice", "all") or single_file:
        signals = _ingest_voice(gateway, correlator, reprocess_from=reprocess_from, single_file=single_file)
        all_signals.extend(signals)

        # Auto-correct if requested
        if hasattr(args, 'correct') and args.correct and signals:
            print("\n" + "="*50)
            print("Running transcript correction...")
            _run_voice_correction()

    if source in ("freetext", "all"):
        signals = _ingest_freetext(gateway, correlator)
        all_signals.extend(signals)

    if source in ("transcript", "all"):
        signals = _ingest_transcripts(gateway, correlator)
        all_signals.extend(signals)

    if source in ("prd", "all"):
        signals = _ingest_prd_comments(correlator)
        all_signals.extend(signals)

    if source in ("publisher", "all"):
        signals = _ingest_publisher(gateway, correlator)
        all_signals.extend(signals)

    # Save extracted signals
    if all_signals:
        INBOX_PROCESSED.mkdir(parents=True, exist_ok=True)
        from datetime import datetime

        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        output = INBOX_PROCESSED / f"signals_{ts}.json"
        data = [s.to_dict() for s in all_signals]
        output.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nSaved {len(all_signals)} signal(s) to {output.name}")
    else:
        print("\nNo signals extracted.")


def _ingest_gitlab(correlator) -> list:
    """Run GitLab diff extractor."""
    from .extractors.gitlab_diff import GitLabDiffExtractor

    print("GitLab diff extractor")
    print("-" * 40)

    extractor = GitLabDiffExtractor()

    # Find the two most recent exports
    exports = sorted(EXPORTS_DIR.glob("gitlab_export_*.json"), reverse=True)

    if not exports:
        print("  No gitlab export files found in tools/exports/")
        print("  Run: ./scripts/run_gitlab_export.sh")
        return []

    current = exports[0]
    previous = exports[1] if len(exports) > 1 else None

    print(f"  Current: {current.name}")
    if previous:
        print(f"  Previous: {previous.name}")
        extraction = extractor.extract(current, previous=previous)
    else:
        print("  No previous export for diff — summarizing current only")
        extraction = extractor.extract(current)

    if extraction.errors:
        for err in extraction.errors:
            print(f"  Error: {err}")

    # Resolve people and initiatives via correlator
    correlator.resolve_signals(extraction.signals)

    print(f"  Extracted {len(extraction.signals)} signal(s)")
    for s in extraction.signals[:5]:
        print(f"    [{s.signal_type}] {s.detail[:80]}")
    if len(extraction.signals) > 5:
        print(f"    ... and {len(extraction.signals) - 5} more")

    return extraction.signals


def _ingest_voice(gateway, correlator, reprocess_from=None, single_file=None) -> list:
    """Process voice memos from inbox/raw/voice/ or a single file.

    Args:
        gateway: Gateway instance for signal routing
        correlator: Correlator instance for signal correlation
        reprocess_from: Optional datetime - only process files modified since this date
        single_file: Optional Path - process only this specific file
    """
    from datetime import datetime

    from .extractors.voice import VoiceExtractor

    print("\nVoice extractor")
    print("-" * 40)

    extractor = VoiceExtractor()
    all_signals = []
    skipped = 0

    # Single file mode
    if single_file:
        if not extractor.can_handle(single_file):
            print(f"  Cannot process: {single_file.name} (unsupported format)")
            print(f"  Supported: {', '.join(sorted(extractor.SUPPORTED_EXTENSIONS))}")
            return []

        print(f"  Processing: {single_file.name}")
        extraction = extractor.extract(
            single_file, gateway=gateway, correlator=correlator
        )
        if extraction.errors:
            for err in extraction.errors:
                print(f"    Warning: {err}")
        all_signals.extend(extraction.signals)
        print(f"    Extracted {len(extraction.signals)} signal(s)")
        return all_signals

    # Directory mode
    voice_dir = INBOX_RAW / "voice"
    if not voice_dir.exists():
        print("  No voice directory found (inbox/raw/voice/)")
        return []

    import shutil
    archive_voice_dir = INBOX_ARCHIVED / "voice"

    for audio_file in sorted(voice_dir.iterdir()):
        if extractor.can_handle(audio_file):
            # Check modification time if reprocessing with date filter
            if reprocess_from:
                mtime = datetime.fromtimestamp(audio_file.stat().st_mtime, tz=UTC)
                if mtime < reprocess_from:
                    skipped += 1
                    continue

            print(f"  Processing: {audio_file.name}")
            extraction = extractor.extract(
                audio_file, gateway=gateway, correlator=correlator
            )
            if extraction.errors:
                for err in extraction.errors:
                    print(f"    Warning: {err}")
            all_signals.extend(extraction.signals)
            print(f"    Extracted {len(extraction.signals)} signal(s)")

            # Archive processed audio file (move to inbox/archived/voice/)
            if not extraction.errors:
                archive_voice_dir.mkdir(parents=True, exist_ok=True)
                archived_path = archive_voice_dir / audio_file.name
                shutil.move(str(audio_file), str(archived_path))
                print(f"    Archived: {audio_file.name}")

    if skipped:
        print(f"  Skipped {skipped} file(s) older than reprocess date")
    if not all_signals:
        print("  No voice memos found" + (" matching date filter" if reprocess_from else ""))

    return all_signals


def _run_voice_correction() -> None:
    """Run transcript correction on all voice transcripts."""
    from .corrector import TranscriptCorrector
    from .extractors.voice import VoiceExtractor

    corrector = TranscriptCorrector()
    extractor = VoiceExtractor()

    # Find all voice transcript files
    voice_dir = INBOX_RAW / "voice"
    if not voice_dir.exists():
        return

    for audio_file in sorted(voice_dir.iterdir()):
        if extractor.can_handle(audio_file):
            # Find matching transcript
            transcript_name = audio_file.stem + "_transcript.txt"
            transcript_path = voice_dir / transcript_name
            if transcript_path.exists():
                print(f"  Correcting: {transcript_name}")
                result = corrector.correct_file(transcript_path)
                if result and result.corrections:
                    print(f"    Applied {len(result.corrections)} correction(s)")


def _ingest_freetext(gateway, correlator) -> list:
    """Process freetext files from inbox/raw/."""
    from .extractors.freetext import FreetextExtractor

    print("\nFreetext extractor")
    print("-" * 40)

    extractor = FreetextExtractor()
    all_signals = []

    # Process .md and .txt files directly in inbox/raw/
    for path in sorted(INBOX_RAW.iterdir()):
        if extractor.can_handle(path):
            print(f"  Processing: {path.name}")
            extraction = extractor.extract(
                path, gateway=gateway, correlator=correlator
            )
            if extraction.errors:
                for err in extraction.errors:
                    print(f"    Warning: {err}")
            all_signals.extend(extraction.signals)
            print(f"    Extracted {len(extraction.signals)} signal(s)")

    if not all_signals:
        print("  No freetext files found in inbox/raw/")

    return all_signals


def _ingest_transcripts(gateway, correlator) -> list:
    """Process meeting transcripts from inbox/raw/teams/."""
    from .extractors.transcript import TranscriptExtractor

    print("\nTranscript extractor")
    print("-" * 40)

    teams_dir = INBOX_RAW / "teams"
    if not teams_dir.exists():
        print("  No teams directory found (inbox/raw/teams/)")
        return []

    extractor = TranscriptExtractor()
    all_signals = []

    for path in sorted(teams_dir.iterdir()):
        if extractor.can_handle(path):
            print(f"  Processing: {path.name}")
            extraction = extractor.extract(
                path, gateway=gateway, correlator=correlator
            )
            if extraction.errors:
                for err in extraction.errors:
                    print(f"    Warning: {err}")
            all_signals.extend(extraction.signals)
            print(f"    Extracted {len(extraction.signals)} signal(s)")

    if not all_signals:
        print("  No transcripts found in inbox/raw/teams/")

    return all_signals


def _ingest_prd_comments(correlator) -> list:
    """Fetch PRD comments from OneDrive via Microsoft Graph."""
    from .extractors.prd_comments import PRDCommentsExtractor

    print("\nPRD comments extractor")
    print("-" * 40)

    extractor = PRDCommentsExtractor()

    # Look for config file in inbox/raw/
    config_path = INBOX_RAW / "prd_comments_config.json"
    if not config_path.exists():
        # Create default config
        config_path.write_text('{"folder_path": "/Documents/ConsumerDocs/PRDs", "days_back": 14}')
        print(f"  Created default config: {config_path.name}")

    extraction = extractor.extract(config_path, correlator=correlator)

    if extraction.errors:
        for err in extraction.errors:
            print(f"  Note: {err}")

    print(f"  Extracted {len(extraction.signals)} comment(s)")
    for s in extraction.signals[:3]:
        print(f"    [{s.signal_type}] {s.detail[:70]}...")
    if len(extraction.signals) > 3:
        print(f"    ... and {len(extraction.signals) - 3} more")

    return extraction.signals


def _ingest_publisher(gateway, correlator) -> list:
    """Process Office 365 documents from inbox/raw/publisher/ and registry.

    Extracts changes from Word/Excel/PowerPoint documents that are
    linked to PRDs in the publisher registry.
    """
    from .extractors.docflow_review import DocFlowReviewExtractor

    print("\nPublisher extractor (Office 365 documents)")
    print("-" * 40)

    # Check for local docs in inbox/raw/publisher/
    publisher_dir = INBOX_RAW / "publisher"
    local_docs = []
    if publisher_dir.exists():
        for ext in (".docx", ".xlsx", ".pptx"):
            local_docs.extend(publisher_dir.glob(f"*{ext}"))

    if local_docs:
        print(f"  Found {len(local_docs)} local document(s)")
        for doc in local_docs[:3]:
            print(f"    - {doc.name}")
        if len(local_docs) > 3:
            print(f"    ... and {len(local_docs) - 3} more")

    # Initialize extractor
    extractor = DocFlowReviewExtractor()

    all_signals = []

    # Process from registry (MS Graph sync)
    try:
        extraction = extractor.extract_all()

        if extraction.errors:
            for err in extraction.errors[:3]:
                print(f"  Note: {err}")

        if extraction.signals:
            print(f"  Extracted {len(extraction.signals)} signal(s) from registry")
            all_signals.extend(extraction.signals)
        else:
            print("  No changes detected in registered documents")

    except Exception as e:
        print(f"  Registry sync skipped: {e}")

    # Process local docs (if any)
    for doc_path in local_docs:
        try:
            signals = extractor.extract_local_doc(doc_path, correlator=correlator)
            if signals:
                print(f"  {doc_path.name}: {len(signals)} signal(s)")
                all_signals.extend(signals)
        except Exception as e:
            print(f"  {doc_path.name}: Error - {e}")

    if not all_signals and not local_docs:
        print("  No documents in inbox/raw/publisher/ and no registered docs")
        print("  To register a PRD: axi pub register --prd <id> --uri <sharepoint_url>")

    return all_signals


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the HTTP inbox ingestion server."""
    from .serve import run_server

    run_server(
        host=args.host,
        port=args.port,
        process=args.process,
        webhook=args.webhook,
    )


def cmd_draft(args: argparse.Namespace) -> None:
    """Synthesize all processed signals into a changelog draft."""
    from .models import Signal
    from .synthesizer import Synthesizer

    include_all = getattr(args, 'include_all', False)
    dry_run = getattr(args, 'dry_run', False)

    print("axi signal — draft synthesis")
    print("-" * 40)

    if include_all:
        print("  Mode: Including ALL signals (--all)")
    else:
        print("  Mode: New signals only (previously reported signals excluded)")

    # Load all processed signals
    all_signals = []
    if INBOX_PROCESSED.exists():
        for signal_file in sorted(INBOX_PROCESSED.glob("signals_*.json")):
            try:
                data = json.loads(signal_file.read_text())
                for item in data:
                    all_signals.append(Signal.from_dict(item))
            except Exception as e:
                print(f"  Warning: Failed to load {signal_file.name}: {e}")

    if not all_signals:
        print("  No processed signals found. Run 'axi signal ingest' first.")
        return

    print(f"  Loaded {len(all_signals)} signal(s) from processed files")

    # Re-resolve people and initiatives against current config
    # (handles config changes since signals were originally extracted)
    from .correlator import Correlator
    correlator = Correlator()
    correlator.resolve_signals(all_signals)
    print(f"  Re-resolved against {len(correlator.people)} people, {len(correlator.initiatives)} initiatives")

    synthesizer = Synthesizer()
    print(f"  Previously reported: {synthesizer.get_reported_count()} signal(s)")

    changelog = synthesizer.synthesize(all_signals, include_all=include_all, correlator=correlator)

    if not changelog.entries:
        print("\n  No new signals to report!")
        if not include_all:
            print("  Use --all to include previously reported signals.")
        return

    # Update blocker tracker with any blocker signals
    from .blocker_tracker import BlockerTracker
    blocker_tracker = BlockerTracker()
    blocker_signals = [s for s in all_signals if s.signal_type == "blocker"]
    if blocker_signals:
        blocker_tracker.update(blocker_signals)
        active = blocker_tracker.get_active_blockers()
        cross = blocker_tracker.get_cross_cutting_blockers()
        print(f"  Blockers: {len(active)} active ({len(cross)} cross-cutting)")

    changelog_path = synthesizer.write_changelog(changelog)
    summary_path = synthesizer.write_weekly_summary(changelog, correlator=correlator, blocker_tracker=blocker_tracker)

    print(f"\n  Changelog: {changelog_path}")
    print(f"  Summary:   {summary_path}")
    print(f"\n  {changelog.summary}")

    # Mark signals as reported (unless dry run or including all)
    if not dry_run and not include_all:
        marked = synthesizer.mark_as_reported()
        print(f"\n  ✓ Marked {marked} signal(s) as reported")
        print("    (These won't appear in future changelogs unless you use --all)")
    elif dry_run:
        print("\n  (Dry run — signals NOT marked as reported)")

    print("\n  Review the drafts, then move approved files to agents/approved/")


def cmd_correct(args: argparse.Namespace) -> None:
    """Correct transcription errors in voice memo transcripts."""
    from .corrector import correct_transcript

    print()
    print("axi signal — transcript correction")
    print("-" * 40)

    # Find transcript(s) to correct
    if args.path:
        transcript_path = Path(args.path)
        if not transcript_path.exists():
            print(f"  Error: File not found: {args.path}")
            sys.exit(1)
        transcripts = [transcript_path]
    elif args.correct_all:
        transcripts = sorted(INBOX_PROCESSED.glob("*_transcript.md"))
        if not transcripts:
            print("  No transcripts found in processed/")
            return
        print(f"  Found {len(transcripts)} transcript(s)")
    else:
        # Find most recent transcript
        transcripts = sorted(INBOX_PROCESSED.glob("*_transcript.md"), reverse=True)
        if not transcripts:
            print("  No transcripts found. Run 'axi signal ingest --source voice' first.")
            return
        transcripts = [transcripts[0]]
        print(f"  Using most recent: {transcripts[0].name}")

    for transcript_path in transcripts:
        print(f"\n  Processing: {transcript_path.name}")
        try:
            result = correct_transcript(
                transcript_path,
                auto_apply=args.apply,
                min_confidence=args.min_confidence,
            )

            # Display corrections
            if result.corrections:
                print(f"\n  Found {len(result.corrections)} potential correction(s):")
                for i, c in enumerate(result.corrections, 1):
                    conf_bar = "█" * int(c.confidence * 10)
                    print(f"\n  {i}. [{c.category}] {c.confidence:.0%} {conf_bar}")
                    print(f"     \"{c.original}\" → \"{c.corrected}\"")
                    print(f"     Reason: {c.reason}")
            else:
                print("  No corrections needed.")

        except ImportError as e:
            print(f"  Error: {e}")
            print("  Install langextract: pip install langextract")
            sys.exit(1)
        except Exception as e:
            print(f"  Error: {e}")
            sys.exit(1)

    print()


def cmd_routes(args: argparse.Namespace) -> None:
    """Show signal routing status and optionally deliver queued signals."""
    from .router import Router

    router = Router()

    # Get status
    status = router.status()

    # If specific endpoint requested
    if args.endpoint:
        endpoint = router.endpoints.get(args.endpoint)
        if not endpoint:
            print(f"  Unknown endpoint: {args.endpoint}")
            print(f"  Available: {', '.join(router.endpoints.keys())}")
            sys.exit(1)

        print(f"\n  Endpoint: {endpoint.name}")
        print("  ─────────────────────────────────────────")
        print(f"  ID:       {endpoint.id}")
        print(f"  Enabled:  {'✓' if endpoint.enabled else '✗'}")
        print(f"  Backend:  {endpoint.delivery_method}")
        print(f"  Frequency: {endpoint.frequency}")

        print("\n  Interests:")
        if endpoint.signal_types != "all":
            types = endpoint.signal_types if isinstance(endpoint.signal_types, list) else [endpoint.signal_types]
            print(f"    Signal types: {', '.join(types)}")
        else:
            print("    Signal types: all")
        if endpoint.initiatives != "all":
            inits = endpoint.initiatives if isinstance(endpoint.initiatives, list) else [endpoint.initiatives]
            print(f"    Initiatives:  {', '.join(inits)}")
        if endpoint.min_confidence > 0:
            print(f"    Min confidence: {endpoint.min_confidence:.0%}")

        # Show pending for this endpoint
        pending = router.get_pending(args.endpoint)
        if pending:
            print(f"\n  Pending signals: {len(pending)}")
            for rec in pending[:5]:
                print(f"    • {rec.signal_id[:40]}...")
            if len(pending) > 5:
                print(f"    ... and {len(pending) - 5} more")
        print()
        return

    # If --pending, show queued signals by endpoint
    if args.pending:
        all_pending = router.get_pending()
        print("\n  ╭─ Pending Signal Deliveries ──────────────────────╮")

        if not all_pending:
            print("  │  No signals queued for delivery                  │")
        else:
            # Group by endpoint
            by_endpoint: dict[str, list] = {}
            for rec in all_pending:
                ep_id = rec.endpoint_id
                by_endpoint.setdefault(ep_id, []).append(rec)

            for ep_id, records in by_endpoint.items():
                ep = router.endpoints.get(ep_id)
                ep_name = ep.name if ep else ep_id
                print(f"  │  {ep_name}: {len(records)} signal(s)")
                for rec in records[:3]:
                    print(f"  │    • {rec.signal_id[:40]}...")
                if len(records) > 3:
                    print(f"  │    ... and {len(records) - 3} more")

        print("  ╰────────────────────────────────────────────────────╯")
        print()
        return

    # If --deliver, push to endpoints
    if args.deliver:
        print("\n  Delivering queued signals...")
        results = router.deliver()

        for ep_id, count in results.items():
            ep = router.endpoints.get(ep_id)
            ep_name = ep.name if ep else ep_id
            if count > 0:
                print(f"  ✓ {ep_name}: delivered {count} signal(s)")
            else:
                print(f"  · {ep_name}: nothing to deliver")
        print()
        return

    # Default: show overview
    print("\n  ╭─ Signal Routing Status ────────────────────────────╮")
    print(f"  │  Endpoints configured: {status['endpoints_count']:>3}                        │")
    print(f"  │  Endpoints enabled:    {status['enabled_count']:>3}                        │")
    print(f"  │  Signals in transit:   {status['transit_count']:>3}                        │")
    print("  ├──────────────────────────────────────────────────────┤")

    # List endpoints with status
    for ep_id, ep in router.endpoints.items():
        status_icon = "✓" if ep.enabled else "○"
        print(f"  │  {status_icon} {ep.name[:30]:<30} {ep.delivery_method:>8} {ep.frequency:>10} │")

    print("  ╰──────────────────────────────────────────────────────┤")
    print("\n  Commands:")
    print("    axi signal routes --pending     Show queued signals")
    print("    axi signal routes --deliver     Push to endpoints")
    print("    axi signal routes --endpoint X  Show endpoint details")
    print()


def cmd_suggest(args: argparse.Namespace) -> None:
    """LLM-powered signal-to-PRD relevance matching."""
    from .smart_router import SmartRouter

    router = SmartRouter()

    # Handle accept/reject actions
    if args.accept:
        try:
            signal_id, prd_id = args.accept.split(":", 1)
            if router.accept_suggestion(signal_id, prd_id):
                print(f"  ✓ Accepted: {signal_id[:20]}... → {prd_id}")
            else:
                print("  ✗ Suggestion not found")
        except ValueError:
            print("  Error: Use format signal_id:prd_id")
            sys.exit(1)
        return

    if args.reject:
        try:
            signal_id, prd_id = args.reject.split(":", 1)
            if router.reject_suggestion(signal_id, prd_id):
                print(f"  ✓ Rejected: {signal_id[:20]}... → {prd_id}")
            else:
                print("  ✗ Suggestion not found")
        except ValueError:
            print("  Error: Use format signal_id:prd_id")
            sys.exit(1)
        return

    # Run LLM matching
    if args.run:
        print("\n  Running LLM signal-to-PRD matching...")

        # Load recent signals (same as cmd_draft)
        from .models import Signal
        all_signals = []
        if INBOX_PROCESSED.exists():
            for signal_file in sorted(INBOX_PROCESSED.glob("signals_*.json")):
                try:
                    data = json.loads(signal_file.read_text())
                    for item in data:
                        all_signals.append(Signal.from_dict(item))
                except Exception:
                    pass

        if not all_signals:
            print("  No signals found. Run 'axi signal ingest' first.")
            return

        # Limit to recent signals (last 20)
        signals = all_signals[-20:]
        print(f"  Analyzing {len(signals)} signal(s) against {len(router.prds)} PRD(s)...")

        prd_filter = [args.prd] if args.prd else None
        matches = router.match_to_prds(
            signals,
            prd_filter=prd_filter,
            min_relevance=args.min_relevance,
        )

        if not matches:
            print("  No relevant matches found.")
            return

        # Queue as suggestions
        added = router.suggest(matches)
        print(f"\n  Found {len(matches)} match(es), queued {added} new suggestion(s)")

        # Show top matches
        print("\n  Top matches:")
        for i, m in enumerate(matches[:10], 1):
            rel_bar = "█" * int(m.relevance_score * 10)
            signal_preview = m.signal.detail or m.signal.raw_text[:50]
            print(f"\n  {i}. [{m.suggested_action}] {m.relevance_score:.0%} {rel_bar}")
            print(f"     Signal: {signal_preview[:50]}")
            print(f"     PRD: {m.prd.title}")
            print(f"     Reason: {m.reasoning[:80]}...")

        print()
        return

    # Default: show status and pending suggestions
    status = router.status()

    print("\n  ╭─ Smart Router Status ──────────────────────────────╮")
    print(f"  │  PRDs loaded:       {status['prds_loaded']:>3}                             │")
    print(f"  │  PRDs active:       {status['prds_active']:>3}                             │")
    print(f"  │  Total suggestions: {status['total_suggestions']:>3}                             │")
    print(f"  │  Pending review:    {status['pending_review']:>3}                             │")
    print(f"  │  Accepted:          {status['accepted']:>3}                             │")
    print("  ╰────────────────────────────────────────────────────────╯")

    # Show pending suggestions
    pending = router.get_pending_suggestions(args.prd)
    if pending:
        print(f"\n  Pending suggestions ({len(pending)}):")
        for s in pending[:10]:
            print(f"\n    [{s['suggested_action']}] {s['relevance_score']:.0%}")
            print(f"    Signal: {s.get('signal_detail', s.get('signal_title', ''))[:40]}...")
            print(f"    PRD: {s['prd_title']}")
            print(f"    Reason: {s['reasoning'][:60]}...")
            print(f"    ID: {s['signal_id'][:20]}...:{s['prd_id']}")

        if len(pending) > 10:
            print(f"\n    ... and {len(pending) - 10} more")

    print("\n  Commands:")
    print("    axi signal suggest --run                 Run LLM matching")
    print("    axi signal suggest --prd <id>            Filter by PRD")
    print("    axi signal suggest --accept sig:prd      Accept suggestion")
    print("    axi signal suggest --reject sig:prd      Reject suggestion")
    print()


def cmd_brief(args: argparse.Namespace) -> None:
    """Generate executive briefing on recent signals."""
    from datetime import datetime, timedelta

    from .briefing import BriefingService

    briefer = BriefingService()

    # Status only
    if args.status:
        status = briefer.status()
        print("\n  ╭─ Briefing Service Status ────────────────────────────╮")

        if status.get("last_consumption"):
            lc = status["last_consumption"]
            print(f"  │  Last activity: {lc['type']:<30}    │")
            print(f"  │                 {lc['relative']:<30}    │")
        else:
            print("  │  Last activity: none recorded                       │")

        if status.get("last_briefing"):
            lb = status["last_briefing"]
            print(f"  │  Last briefing: {lb['relative']:<30}    │")
            print(f"  │                 {lb['signal_count']} signals                            │")
        else:
            print("  │  Last briefing: none                                │")

        print(f"  │  Total briefings: {status.get('briefings_generated', 0):>3}                            │")
        print("  ╰──────────────────────────────────────────────────────╯")
        print()
        return

    # Re-index signals for RAG
    if args.index:
        print("  Re-indexing signals for RAG...")
        try:
            from .signal_rag import reindex_all_signals
            count = reindex_all_signals()
            print(f"  ✓ Indexed {count} signals")
        except Exception as e:
            print(f"  ✗ Indexing failed: {e}")
        return

    # Show available topics
    if args.topics:
        topics = briefer.get_available_topics()
        print("\n  ╭─ Available Briefing Topics ──────────────────────────╮")
        print("  │                                                      │")
        print("  │  Built-in categories:                                │")
        for cat in topics.get("categories", [])[:6]:
            print(f"  │    • {cat:<46} │")
        if len(topics.get("categories", [])) > 6:
            print(f"  │    ... and {len(topics['categories']) - 6} more                              │")
        print("  │                                                      │")

        if topics.get("initiatives"):
            print("  │  Initiatives detected:                               │")
            for init in topics["initiatives"][:5]:
                print(f"  │    • {init[:46]:<46} │")

        if topics.get("detected_topics"):
            print("  │                                                      │")
            print("  │  Long-running topics (auto-detected):                │")
            for topic in topics["detected_topics"][:5]:
                print(f"  │    • {topic[:46]:<46} │")

        print("  │                                                      │")
        print("  ╰──────────────────────────────────────────────────────╯")
        print()
        print("  Usage: axi signal brief \"Kevin\"")
        print("         axi signal brief \"blockers\"")
        print("         axi signal brief \"project alpha\"")
        print()
        return

    # Mark caught up (no briefing)
    if args.caught_up:
        briefer.mark_caught_up()
        print("  ✓ Marked as caught up. Future briefings will start from now.")
        return

    # Determine time window
    since = None
    if args.since:
        since = args.since
    elif args.hours:
        since = datetime.now(UTC) - timedelta(hours=args.hours)

    # Get topic (positional or flag)
    topic = args.topic if hasattr(args, 'topic') and args.topic else None

    # Generate briefing
    print()
    print("  " + "═" * 60)
    if topic:
        print(f"  NEUT BRIEFING: {topic.upper()}")
    else:
        print("  NEUT EXECUTIVE BRIEFING")
    print("  " + "═" * 60)

    brief = briefer.brief_me(since=since, topic=topic, acknowledge=args.ack)

    # Show time window info
    conf_bar = "●" * int(brief.confidence * 5) + "○" * (5 - int(brief.confidence * 5))
    print(f"\n  Time window: {brief.time_window_start.strftime('%Y-%m-%d %H:%M')} → {brief.time_window_end.strftime('%H:%M')}")
    print(f"  Confidence:  {conf_bar} ({brief.confidence:.0%})")
    print(f"  Reason:      {brief.time_window_reason}")
    print(f"  Signals:     {brief.signal_count}")

    if brief.topic != "general":
        print(f"  Topic:       {brief.topic}" + (f" ({brief.topic_query})" if brief.topic_query and brief.topic_query != brief.topic else ""))

    if brief.signals_by_type:
        breakdown = ", ".join(f"{v} {k}" for k, v in sorted(brief.signals_by_type.items(), key=lambda x: -x[1]))
        print(f"  Breakdown:   {breakdown}")

    # The summary
    print("\n  " + "─" * 60)
    print()

    # Format summary with proper indentation
    for line in brief.summary.split("\n"):
        if line.strip():
            # Wrap long lines
            words = line.split()
            current_line = "  "
            for word in words:
                if len(current_line) + len(word) + 1 > 70:
                    print(current_line)
                    current_line = "  " + word
                else:
                    current_line += " " + word if current_line != "  " else word
            if current_line.strip():
                print(current_line)
        else:
            print()

    print("\n  " + "─" * 60)

    # Key signals
    if brief.key_signals:
        print("\n  Key signals:")
        for sig in brief.key_signals[:5]:
            sig_type = sig.get("signal_type", "unknown")
            detail = sig.get("detail", sig.get("summary", ""))[:60]
            print(f"    • [{sig_type}] {detail}")

    # Footer
    print()
    if not args.ack:
        print("  Tip: Use --ack to acknowledge this briefing")
    else:
        print("  ✓ Briefing acknowledged")
    print()

    # Enticement into chat — only in interactive terminals
    if sys.stdin.isatty() and sys.stdout.isatty():
        print("  " + "─" * 60)
        print()
        print("  Discuss with Neut — ask about this briefing,")
        print("  dig into signals, or change the time window.")
        print()
        try:
            from axiom.setup.renderer import prompt_yn

            if prompt_yn("Enter chat?", default=True):
                from axiom.extensions.builtins.chat.entry import (
                    _format_briefing_context,
                    enter_chat,
                )

                topic_label = brief.topic if brief.topic != "general" else "executive"
                ctx_md = _format_briefing_context(brief.to_dict())
                enter_chat(
                    context_markdown=ctx_md,
                    context_data=brief.to_dict(),
                    title=f"Briefing: {topic_label}",
                    suggestions=[
                        "What are the key takeaways?",
                        "Any blockers I should worry about?",
                        "Summarize the most important signals",
                    ],
                    source="neut_sense_brief",
                )
        except (KeyboardInterrupt, EOFError):
            pass  # Ctrl+C / Ctrl+D — return to shell quietly


# ---------------------------------------------------------------------------
# Command Registry (auto-synced to chat slash commands)
# ---------------------------------------------------------------------------

# User-facing commands — shown in default help
USER_COMMANDS: dict[str, str] = {
    "brief":  "Catch up on what happened",
    "media":  "Search, play, and discuss recordings",
    "draft":  "Generate your weekly changelog",
    "status": "Check pipeline health",
}

# Pipeline internals — accessible via `axi signal pipeline <cmd>`
PIPELINE_COMMANDS: dict[str, str] = {
    "ingest":      "Run signal extractors",
    "review":      "Review and correct AI transcriptions",
    "suggest":     "Match signals to PRD documents",
    "sources":     "Manage signal sources",
    "subscribers": "Manage signal subscribers",
    "routes":      "Signal routing and delivery",
    "providers":   "Docflow provider status",
    "serve":       "HTTP ingestion server",
    "voice":       "Voice identification status",
    "timestamps":  "Regenerate word-level timestamps",
}

# Merged dict — backward compat for chat slash commands, CLI registry
COMMANDS: dict[str, str] = {**USER_COMMANDS, **PIPELINE_COMMANDS}


def _build_custom_help() -> str:
    """Build the clean user-facing help text."""
    lines = [
        "axi signal -- Program awareness",
        "",
    ]
    # User commands
    for cmd, desc in USER_COMMANDS.items():
        lines.append(f"  {cmd:<12}{desc}")
    lines.append("")
    lines.append("Run 'axi signal pipeline' for advanced pipeline commands.")
    lines.append("Run 'axi signal <command> --help' for details.")
    return "\n".join(lines)


def _build_pipeline_help() -> str:
    """Build help text for the pipeline subcommand group."""
    lines = [
        "axi signal pipeline -- Advanced pipeline commands",
        "",
    ]
    for cmd, desc in PIPELINE_COMMANDS.items():
        lines.append(f"  {cmd:<14}{desc}")
    lines.append("")
    lines.append("Run 'axi signal pipeline <command> --help' for details.")
    return "\n".join(lines)


def _add_review_args(review_parser: argparse.ArgumentParser) -> None:
    """Add arguments to a review subparser (shared between top-level and pipeline)."""
    review_parser.add_argument(
        "--transcripts",
        action="store_true",
        help="Correct transcription errors (runs transcript correction)",
    )
    review_parser.add_argument(
        "--stats",
        action="store_true",
        help="Show learning/training statistics",
    )
    review_parser.add_argument(
        "--errors",
        action="store_true",
        help="Show recently flagged errors",
    )
    review_parser.add_argument(
        "--play",
        action="store_true",
        help="Auto-play audio clips during guided review (macOS only)",
    )
    review_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max corrections to review (default: all pending)",
    )
    review_parser.add_argument(
        "--by",
        type=str,
        default=os.environ.get("USER", "reviewer"),
        help="Who is providing feedback (default: $USER)",
    )
    # Passthrough args for transcript correction mode (--transcripts)
    review_parser.add_argument(
        "path",
        nargs="?",
        help="Path to transcript file (used with --transcripts)",
    )
    review_parser.add_argument(
        "--apply",
        action="store_true",
        help="Auto-apply high-confidence corrections (used with --transcripts)",
    )
    review_parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.8,
        help="Minimum confidence for auto-apply (used with --transcripts, default: 0.8)",
    )
    review_parser.add_argument(
        "--all",
        action="store_true",
        dest="correct_all",
        help="Correct all transcripts (used with --transcripts)",
    )


def _add_pipeline_subparsers(subparsers) -> None:
    """Register all pipeline command subparsers.

    Called for both the top-level parser (hidden from help) and the pipeline
    subcommand group. This ensures `axi signal pipeline ingest` works the
    same as if we registered ingest at the top level.
    """
    # ingest
    ingest_parser = subparsers.add_parser("ingest", help="Run extractors on inbox data")
    ingest_parser.add_argument(
        "--source",
        choices=["gitlab", "voice", "freetext", "transcript", "prd", "publisher", "all"],
        default="all",
        help="Which source(s) to ingest (default: all)",
    )
    ingest_parser.add_argument(
        "--reprocess-from",
        type=str,
        metavar="DATE",
        dest="reprocess_from",
        help="Reprocess files modified since DATE (YYYY-MM-DD or 'today')",
    )
    ingest_parser.add_argument(
        "--correct",
        action="store_true",
        help="Auto-run transcript correction after voice ingestion",
    )
    ingest_parser.add_argument(
        "--file",
        type=str,
        metavar="PATH",
        help="Process a single file (bypasses date filter and directory scan)",
    )

    # review (merged correct + corrections)
    review_parser = subparsers.add_parser(
        "review",
        help="Review and correct AI transcriptions",
    )
    _add_review_args(review_parser)

    # suggest
    suggest_parser = subparsers.add_parser("suggest", help="LLM-powered signal-to-PRD matching")
    suggest_parser.add_argument(
        "--run",
        action="store_true",
        help="Run LLM matching on recent signals",
    )
    suggest_parser.add_argument(
        "--prd",
        type=str,
        help="Filter to specific PRD (e.g., 'scheduling-system-prd')",
    )
    suggest_parser.add_argument(
        "--min-relevance",
        type=float,
        default=0.3,
        help="Minimum relevance score (0-1, default: 0.3)",
    )
    suggest_parser.add_argument(
        "--accept",
        type=str,
        metavar="SIGNAL_ID:PRD_ID",
        help="Accept a suggestion (signal_id:prd_id)",
    )
    suggest_parser.add_argument(
        "--reject",
        type=str,
        metavar="SIGNAL_ID:PRD_ID",
        help="Reject a suggestion (signal_id:prd_id)",
    )

    # sources
    sources_parser = subparsers.add_parser("sources", help="Manage signal sources")
    sources_parser.add_argument("--list", "-l", action="store_true", dest="list_sources",
                                help="List all registered sources")
    sources_parser.add_argument("--check", action="store_true",
                                help="Check configuration status of all sources")
    sources_parser.add_argument("--inbox", action="store_true",
                                help="Show inbox status for all sources")
    sources_parser.add_argument("--init", action="store_true",
                                help="Create all inbox directories")
    sources_parser.add_argument("--fetch", type=str, metavar="SOURCE",
                                help="Fetch new data from a PULL source")

    # subscribers
    subs_parser = subparsers.add_parser("subscribers", help="Manage signal subscribers")
    subs_parser.add_argument("--list", "-l", action="store_true", dest="list_subs",
                             help="List all registered subscribers")
    subs_parser.add_argument("--status", action="store_true",
                             help="Show subscriber status and publish counts")

    # routes
    routes_parser = subparsers.add_parser("routes", help="Signal routing and delivery")
    routes_parser.add_argument("--deliver", action="store_true",
                               help="Deliver queued signals to endpoints")
    routes_parser.add_argument("--endpoint", type=str,
                               help="Show details for specific endpoint")
    routes_parser.add_argument("--pending", action="store_true",
                               help="Show only pending/queued signals")

    # providers
    providers_parser = subparsers.add_parser("providers", help="Docflow provider status")
    providers_parser.add_argument("--sync", action="store_true",
                                  help="Sync all configured folder providers")
    providers_parser.add_argument("--test", type=str, metavar="SLUG",
                                  help="Test connectivity for a specific provider")

    # serve
    serve_parser = subparsers.add_parser("serve", help="HTTP ingestion server")
    serve_parser.add_argument("--port", type=int, default=8765,
                              help="Port to listen on (default: 8765)")
    serve_parser.add_argument("--host", default="0.0.0.0",
                              help="Host to bind to (default: 0.0.0.0)")
    serve_parser.add_argument("--process", action="store_true",
                              help="Auto-transcribe voice memos on upload")
    serve_parser.add_argument("--webhook", type=str,
                              help="URL to POST notifications when items are processed")

    # voice
    subparsers.add_parser("voice", help="Voice identification status")

    # timestamps
    timestamps_parser = subparsers.add_parser("timestamps",
                                              help="Regenerate word-level timestamps")
    timestamps_parser.add_argument("--all", action="store_true",
                                   help="Regenerate timestamps for ALL transcripts (slow)")
    timestamps_parser.add_argument("--file", "-f", type=str,
                                   help="Regenerate timestamps for a specific transcript")
    timestamps_parser.add_argument("--dry-run", action="store_true",
                                   help="Show what would be regenerated without doing it")
    timestamps_parser.add_argument("--model", type=str, default="base",
                                   help="Whisper model size (default: base)")


def get_parser() -> SuggestingArgumentParser:
    """Build and return the argument parser.

    Exposed for CLI registry introspection. Commands auto-sync to chat.
    """
    all_commands = list(COMMANDS.keys()) + ["pipeline"]

    parser = SuggestingArgumentParser(
        prog="axi signal",
        description="Agentic signal ingestion pipeline",
        valid_subcommands=all_commands,
        custom_help=_build_custom_help(),
    )

    subparsers = parser.add_subparsers(dest="command")

    # ── User-facing commands (shown in default help) ──────────────

    # brief (executive briefing)
    brief_parser = subparsers.add_parser("brief", help="Catch up on what happened")
    brief_parser.add_argument(
        "topic", nargs="?", type=str,
        help="Topic to focus on (person, initiative, category, or free-form query)",
    )
    brief_parser.add_argument("--since", type=str,
        help="Start time: ISO date, relative (2d, 3h, 1w), or natural (yesterday, 'last week')")
    brief_parser.add_argument("--hours", type=int, help="Look back N hours from now")
    brief_parser.add_argument("--ack", action="store_true",
        help="Acknowledge this briefing (affects future time windows)")
    brief_parser.add_argument("--caught-up", action="store_true", dest="caught_up",
        help="Mark yourself as caught up (without generating briefing)")
    brief_parser.add_argument("--status", action="store_true",
        help="Show briefing service status")
    brief_parser.add_argument("--topics", action="store_true",
        help="Show available topics for focused briefings")
    brief_parser.add_argument("--index", action="store_true",
        help="Re-index signals for RAG (improves topic matching)")

    # media library
    media_parser = subparsers.add_parser("media",
        help="Search, play, and discuss recordings")
    media_subparsers = media_parser.add_subparsers(dest="media_action")

    media_search_parser = media_subparsers.add_parser("search",
        help="Search recordings by keyword or concept")
    media_search_parser.add_argument("query", nargs="+",
        help="Search query (auto-detects keyword vs semantic)")
    media_search_parser.add_argument("--mode",
        choices=["auto", "keyword", "semantic", "hybrid"], default="auto",
        help="Search mode (default: auto-detect)")
    media_search_parser.add_argument("--limit", "-n", type=int, default=5,
        help="Maximum results to show (default: 5)")
    media_search_parser.add_argument("--play", action="store_true",
        help="Play matched segment of first result")
    media_search_parser.add_argument("--discuss", action="store_true",
        help="Start interactive discussion with Neut about first result")

    media_play_parser = media_subparsers.add_parser("play",
        help="Play a recording or segment")
    media_play_parser.add_argument("media_id", help="Media ID to play")
    media_play_parser.add_argument("--start", type=float, help="Start time in seconds")
    media_play_parser.add_argument("--duration", type=float, default=15.0,
        help="Duration to play in seconds (default: 15)")

    media_discuss_parser = media_subparsers.add_parser("discuss",
        help="Discuss a recording with Neut (interactive)")
    media_discuss_parser.add_argument("media_id", help="Media ID to discuss")
    media_discuss_parser.add_argument("--question", "-q",
        help="Ask a single question (non-interactive)")
    media_discuss_parser.add_argument("--summary", action="store_true",
        help="Get a quick summary")
    media_discuss_parser.add_argument("--concepts", action="store_true",
        help="Explain technical concepts")
    media_discuss_parser.add_argument("--actions", action="store_true",
        help="Extract action items")

    media_subparsers.add_parser("stats", help="Show media library statistics")

    media_index_parser = media_subparsers.add_parser("index",
        help="Rebuild media index")
    media_index_parser.add_argument("--force", action="store_true",
        help="Force full re-index")

    media_list_parser = media_subparsers.add_parser("list",
        help="List all indexed recordings")
    media_list_parser.add_argument("--limit", "-n", type=int, default=10,
        help="Maximum recordings to show (default: 10)")
    media_list_parser.add_argument("--with", dest="with_person",
        help="Filter by participant (person ID or name)")

    # draft
    draft_parser = subparsers.add_parser("draft",
        help="Generate your weekly changelog")
    draft_parser.add_argument("--all", "-a", dest="include_all", action="store_true",
        help="Include previously reported signals (default: only new signals)")
    draft_parser.add_argument("--dry-run", action="store_true",
        help="Generate changelog but don't mark signals as reported")

    # status
    subparsers.add_parser("status", help="Check pipeline health")

    # watch — live change detection on connected endpoints
    watch_parser = subparsers.add_parser("watch", help="Watch connected endpoints for changes")
    watch_parser.add_argument("--source", default="onedrive", help="Source to watch (default: onedrive)")
    watch_parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    watch_parser.add_argument("--once", action="store_true", help="Check once and exit")

    # ── Pipeline subcommand group ─────────────────────────────────

    pipeline_parser = subparsers.add_parser(
        "pipeline",
        help="Advanced pipeline commands",
    )
    pipeline_parser._custom_help = _build_pipeline_help()
    # Override print_help on the pipeline parser
    _orig_pipeline_help = pipeline_parser.print_help
    def _pipeline_help(file=None, _orig=_orig_pipeline_help):
        print(_build_pipeline_help(), file=file or sys.stdout)
    pipeline_parser.print_help = _pipeline_help

    pipeline_subparsers = pipeline_parser.add_subparsers(dest="pipeline_command")
    _add_pipeline_subparsers(pipeline_subparsers)

    # ── Legacy corrections parser (kept for `axi signal corrections --help`) ──
    # Still registered so existing scripts/docs referencing it get a hint
    corrections_parser = subparsers.add_parser(
        "corrections",
        help="(moved to: axi signal pipeline review)",
    )
    corrections_parser.add_argument("--list", "-l", action="store_true", dest="list_awaiting")
    corrections_parser.add_argument("--errors", action="store_true")
    corrections_parser.add_argument("--flag", type=str, metavar="ID")
    corrections_parser.add_argument("--confirm", type=str, metavar="ID")
    corrections_parser.add_argument("--actual", type=str)
    corrections_parser.add_argument("--reason", type=str, default="")
    corrections_parser.add_argument("--by", type=str,
        default=os.environ.get("USER", "reviewer"))
    corrections_parser.add_argument("--stats", action="store_true")
    corrections_parser.add_argument("--resynthesis", action="store_true")
    corrections_parser.add_argument("--guided", action="store_true")
    corrections_parser.add_argument("--play", action="store_true")
    corrections_parser.add_argument("--defer", type=int, nargs="?", const=24, metavar="HOURS")
    corrections_parser.add_argument("--cleanup", action="store_true")
    corrections_parser.add_argument("--limit", type=int, default=None)
    corrections_parser.add_argument("--check-prompt", action="store_true", dest="check_prompt")

    # ── Legacy correct parser ──
    correct_parser = subparsers.add_parser(
        "correct",
        help="(moved to: axi signal pipeline review --transcripts)",
    )
    correct_parser.add_argument("path", nargs="?")
    correct_parser.add_argument("--apply", action="store_true")
    correct_parser.add_argument("--min-confidence", type=float, default=0.8)
    correct_parser.add_argument("--all", action="store_true", dest="correct_all")

    # ── Legacy db parser (promoted to axi db) ──
    subparsers.add_parser("db", help="(moved to: axi db)")

    return parser


def _dispatch_pipeline_command(command: str, args: argparse.Namespace) -> None:
    """Dispatch a pipeline command by name."""
    dispatch = {
        "ingest": cmd_ingest,
        "review": cmd_review,
        "suggest": cmd_suggest,
        "sources": cmd_sources,
        "subscribers": cmd_subscribers,
        "routes": cmd_routes,
        "providers": cmd_providers,
        "serve": cmd_serve,
        "voice": cmd_voice,
        "timestamps": cmd_timestamps,
    }
    handler = dispatch.get(command)
    if handler:
        handler(args)
    else:
        print(_build_pipeline_help())
        sys.exit(1)


def _dispatch(args: argparse.Namespace) -> None:
    """Dispatch a parsed command to its handler.

    Extracted from main() so self-healing can retry with fixed args.
    """
    # ── User-facing commands ──
    if args.command == "brief":
        cmd_brief(args)
    elif args.command == "media":
        cmd_media(args)
    elif args.command == "draft":
        cmd_draft(args)
    elif args.command == "status":
        cmd_status(args)

    # ── Pipeline subcommand group ──
    elif args.command == "pipeline":
        pipeline_cmd = getattr(args, "pipeline_command", None)
        if not pipeline_cmd:
            print(_build_pipeline_help())
            sys.exit(1)
        _dispatch_pipeline_command(pipeline_cmd, args)

    # ── Legacy commands (still work, kept for backward compat) ──
    elif args.command == "corrections":
        cmd_corrections(args)
    elif args.command == "correct":
        cmd_correct(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "suggest":
        cmd_suggest(args)
    elif args.command == "sources":
        cmd_sources(args)
    elif args.command == "subscribers":
        cmd_subscribers(args)
    elif args.command == "routes":
        cmd_routes(args)
    elif args.command == "providers":
        cmd_providers(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "voice":
        cmd_voice(args)
    elif args.command == "timestamps":
        cmd_timestamps(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "db":
        print("'axi signal db' has moved to 'axi db'.")
        sys.exit(1)

    elif args.command == "watch":
        from .extractors.onedrive_watcher import poll_onedrive_folder, watch_onedrive
        source = getattr(args, "source", "onedrive")
        interval = getattr(args, "interval", 30)
        once = getattr(args, "once", False)

        if source != "onedrive":
            print(f"Unknown watch source: {source}")
            sys.exit(1)

        if once:
            from axiom.infra.branding import get_branding as _gb_od
            changes = poll_onedrive_folder(_gb_od().product_name)
            if changes:
                for c in changes:
                    icon = {"modified": "✏️", "new": "📄", "deleted": "🗑️"}.get(c.event_type, "?")
                    print(f"  {icon} {c.event_type}: {c.file_name} (by {c.editor})")
            else:
                print("  No changes detected.")
        else:
            from axiom.infra.branding import get_branding as _gb_od
            watch_onedrive(_gb_od().product_name, interval_seconds=interval)

    # ── No subcommand → custom help ──
    else:
        print(_build_custom_help())
        sys.exit(1)


def main():
    """CLI entry point for axi signal."""
    parser = get_parser()
    args = parser.parse_args()

    if not getattr(args, "command", None):
        print(_build_custom_help())
        sys.exit(1)

    try:
        _dispatch(args)
    except (ValueError, TypeError, AttributeError) as e:
        import traceback

        from axiom.infra.bus import EventBus, set_default_eventbus
        from axiom.infra.self_heal import attempt_recovery, emit_cli_error

        # Capture the full traceback while we're still in the except block
        tb_str = traceback.format_exc()

        bus = EventBus(log_path=_RUNTIME_DIR / "logs" / "cli_events.jsonl")
        # Pin this bus as the process default so manifest-discovered
        # diagnostics/hygiene handlers publish through it. Per spec-hooks
        # §7 + §9, the registry attaches the doctor handlers to this bus
        # via the extension's [[extension.provides]] kind = "hook" blocks
        # — no boot-time bus.subscribe() ceremony.
        set_default_eventbus(bus)
        try:
            from axiom.extensions.contracts import parse_manifest
            from axiom.infra.hooks import HookBus
            from axiom.infra.hooks.registry import HookRegistry

            registry = HookRegistry(hookbus=HookBus(), eventbus=bus)
            from pathlib import Path as _P
            for ext_name in ("diagnostics", "hygiene"):
                manifest_path = (
                    _P(__file__).resolve().parent.parent
                    / ext_name
                    / "axiom-extension.toml"
                )
                if manifest_path.exists():
                    try:
                        ext = parse_manifest(manifest_path)
                        registry.register_extensions([ext])
                    except Exception:
                        pass
        except ImportError:
            pass

        # GitLab as fallback for doctor failures (soft — no-ops if unavailable)
        try:
            from axiom.infra.subscribers.gitlab_issues import gitlab_issue_handler
            bus.subscribe("doctor.patch_failed", gitlab_issue_handler)
            bus.subscribe("doctor.llm_unavailable", gitlab_issue_handler)
        except ImportError:
            pass

        recovered_args = attempt_recovery(args.command, args, e)
        if recovered_args:
            _dispatch(recovered_args)

        emit_cli_error(
            bus, args.command, sys.argv, e,
            recovered=recovered_args is not None,
            traceback_str=tb_str,
        )

        if not recovered_args:
            print(f"\n  Error: {e}")
            print(f"  Run 'axi signal {args.command} --help' for usage.\n")
            sys.exit(1)


def _play_audio_clip(clip_path: str) -> bool:
    """Play audio clip using system audio player (macOS: afplay)."""
    import shutil
    import subprocess
    from pathlib import Path

    if not Path(clip_path).exists():
        return False

    # Try afplay on macOS
    if shutil.which("afplay"):
        try:
            subprocess.run(["afplay", clip_path], check=True, timeout=30)
            return True
        except (subprocess.SubprocessError, subprocess.TimeoutExpired):
            return False

    # Could add other players here (aplay for Linux, etc.)
    return False


def _get_centered_context(context: str, original: str, width: int = 100) -> str:
    """Get a display snippet of context centered on the original term.

    Instead of showing the first N characters (which may not include the term),
    this finds the term in the context and shows text centered around it.

    Args:
        context: Full context string
        original: The original term to center on
        width: Total width of the display snippet

    Returns:
        A snippet like "...text before [ORIGINAL] text after..."
    """
    if not context:
        return "(no context)"

    # Find the original term (case-insensitive)
    lower_context = context.lower()
    lower_original = original.lower()
    idx = lower_context.find(lower_original)

    if idx == -1:
        # Term not found - just show what we have, truncated
        if len(context) <= width:
            return context
        return context[:width] + "..."

    # Calculate window centered on the term
    term_len = len(original)
    half_width = (width - term_len) // 2

    start = max(0, idx - half_width)
    end = min(len(context), idx + term_len + half_width)

    snippet = context[start:end]

    # Add ellipsis indicators
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(context) else ""

    return f"{prefix}{snippet}{suffix}"


def _play_audio_segment(source_path: str, start_sec: float, duration_sec: float = 12.0) -> bool:
    """Play a segment from an audio file without extracting a clip.

    Uses ffplay (from ffmpeg) to play directly from the source file.
    Falls back to extracting a temp clip if ffplay isn't available.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not Path(source_path).exists():
        return False

    # Try ffplay first (cleanest - no temp files)
    if shutil.which("ffplay"):
        try:
            # ffplay with -nodisp (no video window), -autoexit (quit when done)
            # -ss for start time, -t for duration
            subprocess.run([
                "ffplay", "-nodisp", "-autoexit",
                "-ss", str(start_sec),
                "-t", str(duration_sec),
                source_path
            ], check=True, timeout=duration_sec + 5,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass

    # Fallback: extract temp clip with ffmpeg and play with afplay
    if shutil.which("ffmpeg") and shutil.which("afplay"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
                tmp_path = tmp.name

            # Extract segment to temp file
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(start_sec),
                "-i", source_path,
                "-t", str(duration_sec),
                "-c", "copy", tmp_path
            ], check=True, timeout=10,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Play temp file
            subprocess.run(["afplay", tmp_path], check=True, timeout=duration_sec + 5)

            # Clean up
            Path(tmp_path).unlink(missing_ok=True)
            return True
        except (subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass

    return False


def _tag_speaker_voice(audio_path: str, start_time: float, end_time: float, agents_dir: Path) -> None:
    """Tag the speaker in an audio segment, enrolling their voice.

    Interactive flow:
    1. Try to identify speaker from enrolled profiles
    2. If identified with high confidence, confirm
    3. If not identified, show team roster for selection
    4. Enroll the segment as the selected person's voice
    """

    try:
        from .voice_id import SpeakerIdentifier, VoiceProfileStore
    except ImportError:
        print("  ⚠️ Voice ID not available (install: pip install pyannote.audio torch)")
        return

    import os
    if not os.environ.get("HF_TOKEN"):
        print("  ⚠️ Voice ID requires HF_TOKEN environment variable")
        print("     Get one at: https://huggingface.co/settings/tokens")
        return

    profiles = VoiceProfileStore(agents_dir)

    # Try to identify the speaker
    print("  🎤 Analyzing voice...")
    try:
        identifier = SpeakerIdentifier(profiles)

        # Get embedding for this segment
        embedding = profiles.extract_embedding(audio_path, start_time, end_time)

        # Match against enrolled profiles
        best_match, confidence = identifier._match_embedding(embedding)

        if best_match and confidence > 0.7:
            print(f"  ✓ Recognized: {best_match} ({int(confidence * 100)}% match)")
            try:
                confirm = input("    Is this correct? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

            if confirm not in ('n', 'no'):
                # Add as additional sample to strengthen profile
                profiles.enroll(best_match, audio_path, start_time, end_time)
                print(f"    Added sample to {best_match}'s profile")
                return
    except Exception:
        # Voice ID failed, fall through to manual selection
        pass

    # Show team roster for selection
    people_file = agents_dir / "config" / "people.md"
    team_members = []

    if people_file.exists():
        content = people_file.read_text()
        for line in content.split("\n"):
            if line.startswith("|") and "---" not in line and "Name" not in line:
                parts = line.split("|")
                if len(parts) > 1:
                    name = parts[1].strip()
                    if name:
                        team_members.append(name)

    if not team_members:
        print("  ⚠️ No team roster found (config/people.md)")
        return

    print("\n  Who is speaking?")
    for i, name in enumerate(team_members[:15], 1):
        print(f"    [{i:2d}] {name}")
    if len(team_members) > 15:
        print(f"    ... and {len(team_members) - 15} more")
    print("    [S] Skip / Unknown")

    try:
        choice = input(f"\n  Select (1-{min(15, len(team_members))}) or name: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if choice.lower() in ('s', 'skip', ''):
        print("  Skipped")
        return

    # Parse selection
    selected_name = None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(team_members):
            selected_name = team_members[idx]
    else:
        # Try to match by name
        choice_lower = choice.lower()
        for name in team_members:
            if choice_lower in name.lower():
                selected_name = name
                break

    if not selected_name:
        print(f"  Unknown selection: {choice}")
        return

    # Enroll the voice
    try:
        print(f"  🎤 Enrolling voice for {selected_name}...")
        profile = profiles.enroll(selected_name, audio_path, start_time, end_time)
        print(f"  ✓ Enrolled! ({profile.sample_count} sample{'s' if profile.sample_count != 1 else ''} total)")
    except Exception as e:
        print(f"  ❌ Enrollment failed: {e}")


def _get_audio_timing_for_correction(corr, agents_dir: Path) -> dict | None:
    """Get audio source and timing for a correction using Whisper timestamps.

    Uses multiple strategies to find accurate timing:
    1. Fuzzy match the context string against timestamp words
    2. Search for the original term with fuzzy matching
    3. Fall back to word-position estimation

    Returns dict with source_audio, start_sec, duration_sec or None if not found.
    """
    import json
    from difflib import SequenceMatcher
    from pathlib import Path

    # transcript_path is like: .../inbox/processed/Recording_transcript.md
    # timestamps are at:       .../inbox/processed/Recording_timestamps.json
    # audio is at:             .../inbox/raw/voice/Recording.m4a
    transcript_path = Path(corr.transcript_path)
    if not transcript_path.exists():
        return None

    stem = transcript_path.stem.replace("_transcript", "")
    timestamps_path = transcript_path.parent / f"{stem}_timestamps.json"

    # Find audio file (check raw/voice first, then archived/voice)
    raw_voice_dir = transcript_path.parent.parent / "raw" / "voice"
    archived_voice_dir = transcript_path.parent.parent / "archived" / "voice"
    audio_path = None
    for search_dir in [raw_voice_dir, archived_voice_dir]:
        for ext in [".m4a", ".mp3", ".wav", ".webm"]:
            candidate = search_dir / f"{stem}{ext}"
            if candidate.exists():
                audio_path = candidate
                break
        if audio_path:
            break

    if not audio_path:
        return None

    # Try to get precise timing from timestamps
    start_sec = 0.0
    duration_sec = 12.0
    found_match = False

    if timestamps_path.exists():
        try:
            data = json.loads(timestamps_path.read_text(encoding="utf-8"))
            words = data.get("words", [])

            if words:
                # STRATEGY 1: Use context string for fuzzy sliding-window match
                # The context has ~10 words around the term and is more reliable
                context_clean = corr.context.lower().strip().strip(".")
                # Extract middle portion (where the original term is)
                context_words = context_clean.split()

                if len(context_words) >= 5:
                    # Try different window sizes centered on context
                    best_score = 0.0
                    best_start_idx = 0
                    best_end_idx = 0

                    for window_size in [5, 7, 10, 15, len(context_words)]:
                        if window_size > len(words):
                            continue
                        for i in range(len(words) - window_size + 1):
                            window_text = " ".join(
                                w.get("word", "").lower().strip(".,!?;:\"'")
                                for w in words[i:i + window_size]
                            )
                            # Match against center of context
                            center_start = max(0, (len(context_words) - window_size) // 2)
                            context_slice = " ".join(context_words[center_start:center_start + window_size])
                            score = SequenceMatcher(None, context_slice, window_text).ratio()
                            if score > best_score:
                                best_score = score
                                best_start_idx = i
                                best_end_idx = i + window_size - 1

                    if best_score > 0.5:  # Reasonable match
                        # Center on the match, with padding
                        match_start = words[best_start_idx].get("start", 0)
                        match_end = words[best_end_idx].get("end", match_start + 5)
                        center_time = (match_start + match_end) / 2
                        duration_sec = max(8.0, match_end - match_start + 4)
                        start_sec = max(0, center_time - duration_sec / 2)
                        found_match = True

                # STRATEGY 2: Direct search for original term with fuzzy matching
                if not found_match:
                    original_clean = corr.original.lower().strip()
                    original_words = original_clean.split()

                    if original_words:
                        best_score = 0.0
                        best_idx = 0

                        for i in range(len(words)):
                            # Build window of same size as search term
                            end_i = min(i + len(original_words), len(words))
                            window = " ".join(
                                w.get("word", "").lower().strip(".,!?;:\"'")
                                for w in words[i:end_i]
                            )
                            score = SequenceMatcher(None, original_clean, window).ratio()
                            if score > best_score:
                                best_score = score
                                best_idx = i

                        if best_score > 0.4:  # Allow loose match
                            word_info = words[best_idx]
                            start_sec = max(0, word_info.get("start", 0) - 3)
                            end_idx = min(best_idx + len(original_words), len(words) - 1)
                            end_sec = words[end_idx].get("end", start_sec + 8) + 3
                            duration_sec = max(8.0, end_sec - start_sec)
                            found_match = True
        except Exception:
            pass

    # STRATEGY 3: Fallback - estimate from word position in transcript
    if not found_match:
        try:
            transcript_text = transcript_path.read_text(encoding="utf-8")
            # Search for context snippet (more reliable than just original)
            context_snippet = corr.context[:50].lower() if corr.context else corr.original.lower()
            context_pos = transcript_text.lower().find(context_snippet)
            if context_pos < 0:
                context_pos = transcript_text.lower().find(corr.original.lower())
            if context_pos > 0:
                words_before = len(transcript_text[:context_pos].split())
                # Estimate time at ~2.5 words/second, then CENTER the clip
                estimated_time = words_before / 2.5
                duration_sec = 12.0
                start_sec = max(0, estimated_time - (duration_sec / 2))
        except Exception:
            pass

    return {
        "source_audio": str(audio_path),
        "start_sec": start_sec,
        "duration_sec": duration_sec,
    }


def _discuss_correction_with_ai(corr, context_display: str) -> tuple[str, bool]:
    """Open an interactive AI chat to discuss potential corrections.

    Returns:
        (suggested_term, accepted) - The term agreed upon and whether to use it
    """
    from axiom.infra.gateway import Gateway

    gateway = Gateway()
    if not gateway.available:
        print("  (AI discussion unavailable - no LLM configured)")
        return "", False

    print("\n  💬 Neut Correction Discussion")
    print("  " + "-"*50)
    print(f"  Discussing: \"{corr.original}\" → \"{corr.corrected}\"")
    print(f"  Category: {corr.category}")
    print("  Type your ideas, or:")
    print("    /accept <term>  - Accept a term and apply it")
    print("    /done           - Exit without applying")
    print("  " + "-"*50)

    # Build system prompt with correction context
    system_prompt = f"""You are helping review a transcription correction.

Original (what was transcribed): "{corr.original}"
LLM suggestion: "{corr.corrected}"
Category: {corr.category}
Context: {context_display}

Your role:
- Help the user figure out what the correct term should be
- Consider phonetic similarity (what sounds like the original?)
- Consider domain context (engineering, software, research)
- Suggest alternatives based on the user's ideas
- Be concise and practical

When the user shares an idea, evaluate it:
1. Does it sound like the original transcription?
2. Does it fit the context?
3. Rate confidence: high/medium/low

Keep responses brief (2-3 sentences max)."""

    history = []
    suggested_term = ""

    while True:
        try:
            user_input = input("  you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  (Discussion ended)")
            return "", False

        if not user_input:
            continue

        # Check for commands
        if user_input.lower().startswith("/accept"):
            parts = user_input.split(maxsplit=1)
            if len(parts) > 1:
                suggested_term = parts[1].strip()
                print(f"  ✓ Accepted: \"{suggested_term}\"")
                return suggested_term, True
            else:
                print("  Usage: /accept <term>")
                continue

        if user_input.lower() in ("/done", "/exit", "/quit", "/q"):
            print("  (Discussion ended - no change applied)")
            return "", False

        # Send to LLM
        history.append({"role": "user", "content": user_input})

        # Build conversation for LLM
        if len(history) > 1:
            history_text = "\n".join(
                f"{m['role'].upper()}: {m['content']}"
                for m in history[-6:]
            )
            prompt = f"Conversation so far:\n{history_text}\n\nRespond to the user's latest message."
        else:
            prompt = user_input

        response = gateway.complete(
            prompt=prompt,
            system=system_prompt,
            task="briefing",
            max_tokens=200,
        )

        if response.success:
            print(f"  neut> {response.text}")
            history.append({"role": "assistant", "content": response.text})
        else:
            print(f"  (Error: {response.error})")


def _run_interactive_guided_review(
    guided,
    system,
    limit: int = 5,
    auto_play: bool = False,
    reviewer: str = "reviewer",
) -> None:
    """Run interactive guided correction review with optional audio playback."""
    from pathlib import Path

    # Get pending corrections (all if no limit)
    fetch_limit = 10000 if limit is None else limit * 2
    pending = system.get_unfeedback_corrections(limit=fetch_limit)

    if not pending:
        print("No corrections pending review.")
        print("\n🎉 All caught up! Your training data is growing.")
        return

    agents_dir = Path(__file__).parent.parent

    print("\n🎧 Interactive Correction Review")
    print("="*73)
    print("Commands: Y=confirm, N=wrong, U=unknown, D=discuss with AI, S=skip, Q=quit")
    print("Audio:    R=replay, < earlier, > later, + longer, - shorter")
    print("Voice:    V=tag speaker (teaches voice recognition)")
    print("Pattern:  Confirming auto-confirms all matching corrections")
    if auto_play:
        print("Audio playback: ENABLED (plays from source recording)")
    else:
        print("Audio playback: disabled (use --play to enable)")
    print("="*73)

    confirmed = 0
    auto_confirmed = 0
    flagged = 0
    skipped = 0

    # Track confirmed patterns to skip duplicates in review
    confirmed_patterns = set()  # (original, corrected) tuples
    unknown_count = 0

    # Session stats for summary
    session_training = []  # Track what we added to training
    session_glossary_candidates = []  # Track potential glossary terms

    review_list = pending if limit is None else pending[:limit]
    total_count = len(review_list)

    for i, corr in enumerate(review_list, 1):
        # Skip if this pattern was already confirmed (auto-confirmed as sibling)
        pattern = (corr.original, corr.corrected)
        if pattern in confirmed_patterns:
            continue
        print(f"\n[{i}/{total_count}] {corr.id}")
        print(f"  Original:  \"{corr.original}\"")
        print(f"  Corrected: \"{corr.corrected}\"")
        print(f"  Category:  {corr.category} | Confidence: {int(corr.confidence * 100)}%")
        # Show context centered on the original term
        context_display = _get_centered_context(corr.context, corr.original, width=100)
        print(f"  Context:   {context_display}")

        # Get audio timing (plays directly from source - no clip files)
        audio_info = _get_audio_timing_for_correction(corr, agents_dir)

        # Track current playback window (can be adjusted during review)
        current_start = audio_info["start_sec"] if audio_info else 0.0
        current_duration = audio_info["duration_sec"] if audio_info else 12.0

        if audio_info:
            print(f"  Audio:     @ {current_start:.1f}s ({current_duration:.1f}s segment)")

            if auto_play:
                print("  🔊 Playing from source...")
                _play_audio_segment(
                    audio_info["source_audio"],
                    current_start,
                    current_duration
                )
        else:
            print("  Audio:     No audio available")

        # Interactive prompt
        while True:
            try:
                response = input("\n  [Y/N/U/D/S/R/Q] or [<] [>] [+] [-] for audio? ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n\nReview interrupted.")
                break

            if response in ('y', 'yes'):
                system.confirm_correct(corr.id, confirmed_by=reviewer)
                confirmed += 1

                # Auto-confirm all corrections with the same pattern
                siblings = system.batch_confirm_pattern(
                    original=corr.original,
                    corrected=corr.corrected,
                    confirmed_by=reviewer,
                    exclude_id=corr.id,
                )
                if siblings:
                    auto_confirmed += len(siblings)
                    print(f"  ✓ Confirmed (+{len(siblings)} matching patterns auto-confirmed)")
                    confirmed_patterns.add((corr.original, corr.corrected))
                else:
                    print("  ✓ Confirmed")
                # Track for session summary
                session_training.append((corr.original, corr.corrected, corr.category))
                if corr.category in ('technical_term', 'acronym', 'project_name', 'person_name'):
                    session_glossary_candidates.append((corr.corrected, corr.category))
                break
            elif response in ('n', 'no'):
                actual = input("  What should it be? ").strip()
                if actual:
                    system.flag_error(corr.id, flagged_by=reviewer, actual_correct=actual)
                    print(f"  ✗ Flagged: should be \"{actual}\"")
                    flagged += 1
                    # Track for session summary (learning what NOT to do)
                    session_training.append((corr.original, actual, corr.category))
                else:
                    print("  (Skipped - no correction provided)")
                    skipped += 1
                break
            elif response in ('u', 'unknown', '?'):
                system.mark_unknown(corr.id, marked_by=reviewer)
                print("  ? Marked unknown (won't appear again)")
                unknown_count += 1
                break
            elif response in ('d', 'discuss', 'chat'):
                # Open AI discussion about the correction
                suggested, accepted = _discuss_correction_with_ai(corr, context_display)
                if accepted and suggested:
                    # User accepted a term from discussion
                    if suggested.lower() == corr.corrected.lower():
                        # They agreed with the LLM's original suggestion
                        system.confirm_correct(corr.id, confirmed_by=reviewer)
                        confirmed += 1
                        print("  ✓ Confirmed (agreed with original suggestion)")
                        session_training.append((corr.original, corr.corrected, corr.category))
                    else:
                        # They found a different correction
                        system.flag_error(corr.id, flagged_by=reviewer, actual_correct=suggested)
                        flagged += 1
                        print(f"  ✓ Applied: \"{suggested}\"")
                        session_training.append((corr.original, suggested, corr.category))
                    if corr.category in ('technical_term', 'acronym', 'project_name', 'person_name'):
                        session_glossary_candidates.append((suggested, corr.category))
                    break
                # If not accepted, continue the review loop for this item
                print("  (Back to review)")
            elif response in ('s', 'skip'):
                print("  → Skipped")
                skipped += 1
                break
            elif response in ('r', 'replay'):
                if audio_info:
                    print(f"  🔊 Replaying @ {current_start:.1f}s ({current_duration:.1f}s)...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
                # Don't break - continue prompting
            elif response in ('<<', '[['):
                # Shift window earlier by 10 seconds (double)
                if audio_info:
                    current_start = max(0, current_start - 10)
                    print(f"  ⏪⏪ Jump earlier @ {current_start:.1f}s ({current_duration:.1f}s)...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('<', '[', 'earlier', 'back'):
                # Shift window earlier by 5 seconds
                if audio_info:
                    current_start = max(0, current_start - 5)
                    print(f"  ⏪ Playing earlier @ {current_start:.1f}s ({current_duration:.1f}s)...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('>>', ']]'):
                # Shift window later by 10 seconds (double)
                if audio_info:
                    current_start = current_start + 10
                    print(f"  ⏩⏩ Jump later @ {current_start:.1f}s ({current_duration:.1f}s)...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('>', ']', 'later', 'forward'):
                # Shift window later by 5 seconds
                if audio_info:
                    current_start = current_start + 5
                    print(f"  ⏩ Playing later @ {current_start:.1f}s ({current_duration:.1f}s)...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response == '++':
                # Extend duration by 10 seconds (double)
                if audio_info:
                    current_duration = current_duration + 10
                    print(f"  📏📏 Extended to {current_duration:.1f}s @ {current_start:.1f}s...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('+', 'longer', 'expand', 'e'):
                # Extend duration by 5 seconds
                if audio_info:
                    current_duration = current_duration + 5
                    print(f"  📏 Extended to {current_duration:.1f}s @ {current_start:.1f}s...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response == '--':
                # Shorten duration by 10 seconds (double, min 5s)
                if audio_info:
                    current_duration = max(5, current_duration - 10)
                    print(f"  📏📏 Shortened to {current_duration:.1f}s @ {current_start:.1f}s...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('-', 'shorter'):
                # Shorten duration by 5 seconds (min 5s)
                if audio_info:
                    current_duration = max(5, current_duration - 5)
                    print(f"  📏 Shortened to {current_duration:.1f}s @ {current_start:.1f}s...")
                    _play_audio_segment(
                        audio_info["source_audio"],
                        current_start,
                        current_duration
                    )
                else:
                    print("  (No audio available)")
            elif response in ('v', 'voice', 'tag'):
                # Voice tagging - identify/enroll the speaker
                if not audio_info:
                    print("  (No audio available for voice tagging)")
                else:
                    _tag_speaker_voice(
                        audio_path=audio_info["source_audio"],
                        start_time=current_start,
                        end_time=current_start + current_duration,
                        agents_dir=agents_dir,
                    )
            elif response in ('q', 'quit'):
                print("\n\nReview ended early.")
                _print_session_summary(
                    confirmed, auto_confirmed, flagged, unknown_count, skipped,
                    session_training, session_glossary_candidates, system
                )
                return
            else:
                print("  Y/N/U/D/S/R/Q/V or < > + - for audio control")

    print("\n" + "="*60)
    print("Review complete!")
    _print_session_summary(
        confirmed, auto_confirmed, flagged, unknown_count, skipped,
        session_training, session_glossary_candidates, system
    )

    total_processed = confirmed + auto_confirmed + flagged + unknown_count + skipped
    remaining = len(pending) - total_processed
    if remaining > 0:
        print(f"\n{remaining} more correction(s) pending.")
        print("Run: axi signal corrections --guided")


def _print_session_summary(
    confirmed: int,
    auto_confirmed: int,
    flagged: int,
    unknown: int,
    skipped: int,
    session_training: list,
    session_glossary_candidates: list,
    system,
) -> None:
    """Print a rewarding summary of the review session's impact."""
    print("\n📊 Session Results:")
    print(f"  Confirmed: {confirmed}" + (f" (+{auto_confirmed} auto)" if auto_confirmed else ""))
    print(f"  Flagged:   {flagged}")
    print(f"  Unknown:   {unknown}")
    print(f"  Skipped:   {skipped}")

    # Show training data collected
    training_added = confirmed + auto_confirmed + flagged
    if training_added > 0:
        print("\n📊 Training Data Added:")
        print(f"  +{training_added} labeled examples collected")

        # Group by category
        categories = {}
        for _orig, _corr, cat in session_training:
            categories[cat] = categories.get(cat, 0) + 1
        if categories:
            print(f"  Categories: {', '.join(f'{cat}({n})' for cat, n in sorted(categories.items()))}")

    # Show glossary potential
    if session_glossary_candidates:
        unique_terms = list(set(term for term, _ in session_glossary_candidates))
        print("\n📖 Glossary Potential:")
        print(f"  {len(unique_terms)} new term(s) learned: {', '.join(unique_terms[:10])}")
        if len(unique_terms) > 10:
            print(f"    ...and {len(unique_terms) - 10} more")

    # Show overall stats
    try:
        stats = system.get_training_stats()
        print("\n📈 Overall Training Data:")
        print(f"  Total confirmed:  {stats['total_confirmed']} examples")
        print(f"  Total errors:     {stats['total_errors']} negative examples")
        accuracy_pct = stats['accuracy'] * 100
        print(f"  System accuracy:  {accuracy_pct:.1f}%")
        if stats['feedback_pending'] > 0:
            print(f"  Still pending:    {stats['feedback_pending']} corrections")
    except Exception:
        pass  # Stats unavailable


def cmd_corrections(args):
    """Non-blocking correction feedback system."""
    from .correction_review import (
        CorrectionReviewSystem,
        print_recent_errors,
        print_training_stats,
        print_unfeedback_corrections,
    )
    from .correction_review_guided import GuidedCorrectionReview

    system = CorrectionReviewSystem()
    guided = GuidedCorrectionReview()

    # Guided review mode - interactive with optional audio playback
    if args.guided:
        _run_interactive_guided_review(
            guided=guided,
            system=system,
            limit=args.limit,
            auto_play=getattr(args, 'play', False),
            reviewer=args.by,
        )
        return

    if args.defer is not None:
        msg = guided.defer_review(hours=args.defer)
        print(msg)
        return

    if args.cleanup:
        stats = guided.cleanup_processed_clips()
        print("Cleanup complete:")
        print(f"  Checked: {stats['checked']} clips")
        print(f"  Removed: {stats['removed']} clips")
        print(f"  Kept: {stats['kept']} clips")
        if stats['bytes_freed'] > 0:
            mb_freed = stats['bytes_freed'] / (1024 * 1024)
            print(f"  Freed: {mb_freed:.2f} MB")
        return

    if args.check_prompt:
        should_prompt, message = guided.should_prompt_review()
        if should_prompt:
            print(f"PROMPT: {message}")
            print("  Run: axi signal corrections --guided")
        else:
            print(f"NO_PROMPT: {message}")
        return

    if args.stats:
        print_training_stats()
        return

    if args.errors:
        print_recent_errors()
        return

    if args.resynthesis:
        jobs = system.get_pending_resynthesis()
        if not jobs:
            print("No pending re-synthesis jobs.")
        else:
            print(f"\n{len(jobs)} re-synthesis job(s) pending:\n")
            for job in jobs:
                print(f"  [{job.id}] Signal: {job.signal_id}")
                print(f"    Correction: {job.correction_id}")
                print(f"    Endpoints: {', '.join(job.published_endpoints)}")
                print()
        return

    if args.flag:
        if not args.actual:
            # Look up the correction to show what it was
            corr = system.get_applied(args.flag)
            if not corr:
                print(f"Correction not found: {args.flag}")
                return
            print(f"Correction: {corr.original!r} → {corr.corrected!r}")
            print("Use --actual to specify what it should have been")
            print(f"Example: --flag {args.flag} --actual 'correct text' --reason 'why'")
            return

        corr = system.flag_error(
            correction_id=args.flag,
            flagged_by=args.by,
            actual_correct=args.actual,
            reason=args.reason,
        )
        if corr:
            print(f"✗ Flagged error: {corr.original!r} → {corr.corrected!r}")
            print(f"  Should have been: {args.actual!r}")
            print("  Added to negative examples (will be avoided in future)")
            if corr.signal_ids:
                print("  Re-synthesis queued for affected signals")
        else:
            print(f"Correction not found: {args.flag}")
        return

    if args.confirm:
        corr = system.confirm_correct(
            correction_id=args.confirm,
            confirmed_by=args.by,
            note=args.reason,
        )
        if corr:
            print(f"✓ Confirmed: {corr.original!r} → {corr.corrected!r}")
            print("  Added to training set (positive example)")
        else:
            print(f"Correction not found: {args.confirm}")
        return

    # Default: list corrections awaiting feedback
    print_unfeedback_corrections()


def cmd_review(args: argparse.Namespace) -> None:
    """Unified review command — merges correct + corrections.

    Dispatches based on flags:
      --transcripts  → transcript correction (cmd_correct)
      --stats        → training stats
      --errors       → recent errors
      (default)      → guided correction review
    """
    if getattr(args, "transcripts", False):
        cmd_correct(args)
    elif getattr(args, "stats", False):
        # Wire up for cmd_corrections
        args.guided = False
        args.list_awaiting = False
        args.errors = False
        args.flag = None
        args.confirm = None
        args.resynthesis = False
        args.check_prompt = False
        args.defer = None
        args.cleanup = False
        cmd_corrections(args)
    elif getattr(args, "errors", False):
        args.guided = False
        args.list_awaiting = False
        args.stats = False
        args.flag = None
        args.confirm = None
        args.resynthesis = False
        args.check_prompt = False
        args.defer = None
        args.cleanup = False
        cmd_corrections(args)
    else:
        # Default: guided review (the most useful mode)
        args.guided = True
        args.list_awaiting = False
        args.errors = False
        args.stats = False
        args.flag = None
        args.confirm = None
        args.resynthesis = False
        args.check_prompt = False
        args.defer = None
        args.cleanup = False
        if not hasattr(args, "play"):
            args.play = False
        if not hasattr(args, "by"):
            args.by = os.environ.get("USER", "reviewer")
        if not hasattr(args, "limit"):
            args.limit = None
        cmd_corrections(args)


def cmd_sources(args):
    """Manage signal sources (extractors)."""
    from .registry import (
        get_registry,
        print_inbox_status,
        print_sources_table,
    )

    registry = get_registry()

    if args.init:
        paths = registry.ensure_all_inboxes()
        print(f"Created/verified {len(paths)} inbox directories:")
        for p in paths:
            print(f"  {p}")
        return

    if args.inbox:
        print_inbox_status()
        return

    if args.check:
        sources = registry.sources
        print("\n=== Source Configuration Check ===\n")

        configured = []
        unconfigured = []

        for src in sources:
            ok, msg = src.check_configured()
            if ok:
                configured.append(src)
            else:
                unconfigured.append((src, msg))

        if configured:
            print("✓ Configured:")
            for src in configured:
                print(f"  {src.icon} {src.name}")

        if unconfigured:
            print("\n✗ Needs configuration:")
            for src, msg in unconfigured:
                print(f"  {src.icon} {src.name}: {msg}")

        print()
        return

    if args.fetch:
        source_name = args.fetch
        extractor = registry.get_extractor(source_name)
        if not extractor:
            print(f"Unknown source: {source_name}")
            print(f"Available: {', '.join(s.name for s in registry.sources)}")
            return

        source_meta = registry.get_source(source_name)
        if not source_meta:
            print(f"Source metadata not found: {source_name}")
            return

        meta, _ = source_meta
        if meta.source_type.value == "push":
            print(f"{source_name} is a PUSH source (data is sent to us, not fetched)")
            return

        print(f"Fetching from {source_name}...")
        # The actual fetch would depend on the extractor implementation
        print(f"  (fetch logic depends on extractor - use `axi signal ingest --source {source_name}`)")
        return

    # Default: list sources
    print_sources_table()


def cmd_subscribers(args):
    """Manage signal subscribers (sinks)."""
    from .registry import (
        get_registry,
        print_subscribers_table,
    )

    registry = get_registry()

    if args.status:
        subs = registry.subscribers
        print("\n=== Subscriber Status ===\n")

        for sub in sorted(subs, key=lambda s: s.name):
            print(f"{sub.icon} {sub.name}")
            print(f"    Type: {sub.subscriber_type.value}")
            if sub.last_publish:
                print(f"    Last publish: {sub.last_publish}")
            if sub.publish_count:
                print(f"    Total published: {sub.publish_count}")
            if sub.last_error:
                print(f"    Last error: {sub.last_error}")
            print()
        return

    # Default: list subscribers
    print_subscribers_table()


def cmd_providers(args):
    """List and manage publisher providers (Google Docs, Dropbox, Box, etc.)."""
    try:
        from .extractors.docflow_providers import (
            ProviderRegistry,
            sync_all_folders,
        )
    except ImportError as e:
        print(f"Error: Could not import provider registry: {e}")
        return

    print("\n=== Publisher Providers ===\n")

    # Test a specific provider
    if args.test:
        slug = args.test
        print(f"Testing provider: {slug}")

        # Try as doc provider first
        provider = ProviderRegistry.get_doc_provider(slug)
        if provider:
            print("  Type: Document Provider")
            print(f"  Display Name: {provider.display_name}")
            print(f"  Available: {'Yes' if provider.is_available else 'No (credentials not configured)'}")
            if provider.capabilities:
                caps = ", ".join(c.value for c in provider.capabilities)
                print(f"  Capabilities: {caps}")
            return

        # Try as folder provider
        provider = ProviderRegistry.get_folder_provider(slug)
        if provider:
            print("  Type: Folder Sync Provider")
            print(f"  Display Name: {provider.display_name}")
            print(f"  Available: {'Yes' if provider.is_available else 'No (credentials not configured)'}")
            print(f"  Local Root: {provider._local_root}")
            if provider.capabilities:
                caps = ", ".join(c.value for c in provider.capabilities)
                print(f"  Capabilities: {caps}")
            return

        print(f"  Unknown provider: {slug}")
        return

    # Sync all folder providers
    if args.sync:
        print("Syncing all configured folder providers...\n")
        try:
            changes = sync_all_folders()
            if changes:
                print(f"  {len(changes)} change(s) detected:\n")
                for change in changes[:10]:
                    print(f"    [{change.change_type.value}] {change.path}")
                if len(changes) > 10:
                    print(f"    ... and {len(changes) - 10} more")
            else:
                print("  No changes detected")
        except Exception as e:
            print(f"  Sync error: {e}")
        return

    # Default: list all providers
    doc_providers = ProviderRegistry.list_doc_providers()
    folder_providers = ProviderRegistry.list_folder_providers()

    print("Document Providers (single doc sync):")
    if doc_providers:
        for slug in sorted(doc_providers):
            provider = ProviderRegistry.get_doc_provider(slug)
            status = "✓" if provider and provider.is_available else "○"
            name = provider.display_name if provider else slug
            print(f"  {status} {slug:15} - {name}")
    else:
        print("  (none registered)")

    print("\nFolder Sync Providers (sync entire folders):")
    if folder_providers:
        for slug in sorted(folder_providers):
            provider = ProviderRegistry.get_folder_provider(slug)
            status = "✓" if provider and provider.is_available else "○"
            name = provider.display_name if provider else slug
            print(f"  {status} {slug:15} - {name}")
    else:
        print("  (none registered)")

    print("\n✓ = configured and ready")
    print("○ = registered but not configured (set env vars)")
    print("\nEnvironment variable pattern: DOCFLOW_{PROVIDER}_TOKEN")
    print("See: tools/pipelines/sense/extractors/DOCFLOW_PROVIDERS.md")


def cmd_db(args):
    """Manage vector database (PostgreSQL + pgvector via K3D)."""
    import os

    from .pgvector_store import (
        DEFAULT_LOCAL_URL,
        VectorDB,
        k3d_delete,
        k3d_down,
        k3d_status,
        k3d_up,
    )

    action = args.db_action

    if action == "stats":
        try:
            db = VectorDB()
            db.connect()
            stats = db.stats()

            print("\n📊 Vector Database Stats\n")
            print(f"Backend:        {stats.get('backend', 'pgvector')}")
            print(f"Signals:        {stats.get('signals', 0)} ({stats.get('signals_indexed', 0)} indexed)")
            print(f"Media:          {stats.get('media', 0)} ({stats.get('media_indexed', 0)} indexed)")
            print(f"Participants:   {stats.get('participants', 0)}")
            print(f"Unique People:  {stats.get('unique_people', 0)}")
            print(f"\nConnection:     {stats.get('connection', 'N/A')}")

            db.close()
        except Exception as e:
            print(f"Error connecting to database: {e}")
            print("\nIs the database running? Try: axi signal db up")

    elif action == "sync":
        print("Note: With K3D architecture, local and remote use the same PostgreSQL stack.")
        print("Data replication is handled at the infrastructure level (K8s/K3D).")
        print("\nFor staging/production sync, deploy to your Kubernetes cluster.")

    elif action == "import":
        print("📦 Importing from file-based index to PostgreSQL...\n")

        # Load existing file-based data
        from .signal_rag import EMBEDDINGS_PATH, INDEX_PATH, SignalRAG

        if not INDEX_PATH.exists():
            print("No existing signal index found.")
            return

        # Count records to import
        import json
        try:
            index_data = json.loads(INDEX_PATH.read_text())
            signal_count = len(index_data) if isinstance(index_data, list) else 0
        except Exception:
            signal_count = 0

        print(f"Found {signal_count} signals to import")

        if args.dry_run:
            print("\n(Dry run - no changes made)")
            return

        try:
            # Import using SignalRAG with pgvector
            rag = SignalRAG(use_pgvector=True)

            if rag.backend_name != "pgvector":
                print("Error: Could not connect to pgvector backend")
                print("Make sure database is running: axi signal db up")
                return

            # Load file-based data and re-index
            from .signal_rag import VectorStore
            file_store = VectorStore()
            if file_store.load(INDEX_PATH, EMBEDDINGS_PATH):
                imported = 0
                for chunk, embedding in zip(file_store.chunks, file_store.embeddings):
                    rag.store.add(chunk, embedding)
                    imported += 1

                print(f"✓ Imported {imported} signals to pgvector")
            else:
                print("Error loading existing index")
        except Exception as e:
            print(f"Error during import: {e}")
            print("\nMake sure database is running: axi signal db up")

    elif action == "migrate":
        # Alembic schema migrations
        from .migrations import (
            check_migrations,
            ensure_pgvector_extension,
            run_migrations,
            verify_schema,
        )

        cmd = getattr(args, 'migrate_command', 'check') or 'check'
        revision = getattr(args, 'revision', 'head') or 'head'
        message = getattr(args, 'message', '')
        autogenerate = getattr(args, 'autogenerate', False)

        if cmd == "check":
            print("\n🔍 Migration Status\n")

            status = check_migrations()

            if not status.get("connected"):
                print("❌ Cannot connect to database")
                print("   Is the database running? Try: axi signal db up")
                return

            print(f"Current revision: {status.get('current') or '(none)'}")
            print(f"Head revision:    {status.get('head') or '(none)'}")
            print(f"Pending:          {status.get('pending', 0)} migration(s)")

            if status.get("up_to_date"):
                print("\n✅ Database is up to date")
            else:
                print(f"\n⚠️  {status['pending']} pending migration(s):")
                for rev in status.get("pending_revisions", []):
                    print(f"   - {rev}")
                print("\nRun: axi signal db migrate upgrade head")

            # Also verify schema
            schema = verify_schema()
            if schema.get("valid"):
                print("\n✅ Schema verified")
            else:
                if schema.get("missing_tables"):
                    print(f"\n⚠️  Missing tables: {', '.join(schema['missing_tables'])}")
                if not schema.get("has_pgvector"):
                    print("⚠️  pgvector extension not installed")

        elif cmd == "upgrade":
            print(f"\n🚀 Upgrading database to revision: {revision}\n")

            # Ensure pgvector extension first
            ensure_pgvector_extension()

            if run_migrations("upgrade", revision):
                print("\n✅ Upgrade complete")

                # Show current status
                status = check_migrations()
                print(f"Current revision: {status.get('current')}")
            else:
                print("\n❌ Upgrade failed")

        elif cmd == "downgrade":
            print(f"\n⬇️  Downgrading database to revision: {revision}\n")

            if run_migrations("downgrade", revision):
                print("\n✅ Downgrade complete")

                status = check_migrations()
                print(f"Current revision: {status.get('current')}")
            else:
                print("\n❌ Downgrade failed")

        elif cmd == "current":
            run_migrations("current")

        elif cmd == "history":
            print("\n📜 Migration History\n")
            run_migrations("history")

        elif cmd == "revision":
            if not message:
                print("Error: --message/-m is required for 'revision' command")
                print("Example: axi signal db migrate revision -m 'add user table'")
                return

            print(f"\n📝 Creating new migration: {message}\n")

            if run_migrations("revision", message=message, autogenerate=autogenerate):
                print("\n✅ Migration created")
                if autogenerate:
                    print("   Review the generated migration before applying.")
            else:
                print("\n❌ Failed to create migration")

        else:
            print("Usage: axi signal db migrate <command> [revision]")
            print()
            print("Commands:")
            print("  check       Check migration status (default)")
            print("  upgrade     Apply pending migrations (default: head)")
            print("  downgrade   Revert migrations (specify revision)")
            print("  current     Show current database revision")
            print("  history     Show migration history")
            print("  revision    Create new migration (-m message required)")
            print()
            print("Examples:")
            print("  axi signal db migrate check")
            print("  axi signal db migrate upgrade head")
            print("  axi signal db migrate downgrade -1")
            print("  axi signal db migrate revision -m 'add user preferences' --autogenerate")

    elif action == "config":
        print("\n⚙️  Database Configuration\n")

        db_url = os.environ.get("AXIOM_DB_URL", DEFAULT_LOCAL_URL)

        # Mask password in URL
        import re
        masked_url = re.sub(r":[^:@]+@", ":****@", db_url)

        # Check K3D status
        status = k3d_status()

        print("--- K3D Cluster ---")
        if status.get("k3d_installed") is False:
            print("  K3D: Not installed")
            print("  Install: brew install k3d")
        elif status.get("exists"):
            from axiom.extensions.builtins.signals.pgvector_store import K3D_CLUSTER_NAME
            print(f"  Cluster: {K3D_CLUSTER_NAME}")
            print(f"  Running: {'Yes' if status.get('running') else 'No'}")
        else:
            print("  Cluster: Not created")
            print("  Create:  axi signal db up")

        print("\n--- Connection ---")
        print(f"  URL: {masked_url}")

        if os.environ.get("AXIOM_DB_URL"):
            print("  Source: AXIOM_DB_URL environment variable")
        else:
            print("  Source: Default (local K3D)")

        # Test connection if cluster is running
        if status.get("running"):
            try:
                db = VectorDB()
                db.connect()
                health = db.health_check()
                print("\n--- Health Check ---")
                print(f"  Status:     {health.get('status', 'unknown')}")
                print(f"  PostgreSQL: {health.get('postgresql', 'N/A')[:50]}")
                print(f"  pgvector:   {health.get('pgvector', 'N/A')}")
                db.close()
            except Exception as e:
                print(f"\n  Connection: ✗ Error: {e}")

        print("\n--- Environments ---")
        print("  Local:      axi signal db up  (K3D)")
        print("  Staging:    Set AXIOM_DB_URL to staging PostgreSQL")
        print("  Production: Set AXIOM_DB_URL to production PostgreSQL")

    elif action == "up":
        print("🚀 Starting local PostgreSQL + pgvector (K3D)...\n")
        success = k3d_up()
        if not success:
            print("\nFailed to start. Check prerequisites above.")

    elif action == "down":
        print("⏸️  Stopping local cluster...\n")
        k3d_down()

    elif action == "delete":
        if not args.confirm:
            print("⚠️  This will DELETE the local cluster and ALL data!")
            print("\nTo confirm, run:")
            print("  axi signal db delete --confirm")
            return

        print("🗑️  Deleting local cluster...\n")
        k3d_delete()

    else:
        print("Usage: axi signal db <command>")
        print()
        print("Cluster Management:")
        print("  up        Start local K3D cluster with PostgreSQL + pgvector")
        print("  down      Stop local K3D cluster (preserves data)")
        print("  delete    Delete local cluster and all data (--confirm required)")
        print()
        print("Database:")
        print("  stats     Show database statistics")
        print("  config    Show current database configuration")
        print()
        print("Schema Migrations (Alembic):")
        print("  migrate check      Check migration status")
        print("  migrate upgrade    Apply pending migrations")
        print("  migrate downgrade  Revert migrations")
        print("  migrate history    Show migration history")
        print("  migrate revision   Create new migration")
        print()
        print("Data Import:")
        print("  import    Import data from file-based index to PostgreSQL")
        print()
        print("Quick Start:")
        print("  axi signal db up              # Start local PostgreSQL")
        print("  axi signal db migrate upgrade # Apply migrations")
        print("  axi signal db stats           # Check it's working")
        print()
        print("Configuration:")
        print("  Local (default): postgresql://axiom:axiom@localhost:5432/axiom_db")
        print("  Remote: Set AXIOM_DB_URL environment variable")


def cmd_media(args):
    """Search, play, and discuss recordings with Neut."""
    from .media_library import (
        MediaLibrary,
        SearchMode,
        format_duration,
    )

    library = MediaLibrary()
    action = args.media_action

    if action == "search":
        query = " ".join(args.query)
        mode = SearchMode(args.mode)

        print(f"\n🔍 Searching: \"{query}\" (mode: {mode.value})\n")

        results = library.search(query, mode=mode, top_k=args.limit)

        if not results:
            print("No results found.")
            return

        for i, result in enumerate(results, 1):
            item = result.item
            score_pct = int(result.score * 100)
            duration = format_duration(item.duration_sec) if item.duration_sec else "?"

            print(f"{i}. [{score_pct}%] {item.title or item.media_id}")
            print(f"   ID: {item.media_id}")
            print(f"   Duration: {duration}  |  Type: {item.media_type.value}")
            if result.matched_text:
                preview = result.matched_text[:150].replace("\n", " ")
                print(f"   Match: \"{preview}...\"")
            print()

        # Play first result
        if args.play and results:
            first = results[0]
            print(f"🎵 Playing matched segment from: {first.item.title or first.item.media_id}")
            if first.start_time_sec is not None:
                library.play(first.item.media_id, start_sec=first.start_time_sec, duration_sec=15)
            else:
                # Find segment based on matched text
                segment = library.find_segment(query, first.item.media_id)
                if segment:
                    start, end, _ = segment
                    library.play(first.item.media_id, start_sec=start, duration_sec=end - start)
                else:
                    library.play(first.item.media_id)

        # Discuss first result
        if args.discuss and results:
            first = results[0]
            print(f"💬 Starting discussion about: {first.item.title or first.item.media_id}\n")
            neut = library.discuss(first)
            neut.interactive()

    elif action == "play":
        print(f"🎵 Playing: {args.media_id}")
        if not library.play(args.media_id, start_sec=args.start, duration_sec=args.duration):
            print("Playback failed. Check media ID or file existence.")

    elif action == "discuss":
        # Find the item
        item = library._items.get(args.media_id)
        if not item:
            print(f"Media not found: {args.media_id}")
            return

        # Create a synthetic SearchResult for the explainer
        from .media_library import SearchResult
        result = SearchResult(
            item=item,
            score=1.0,
            match_type=SearchMode.KEYWORD,
            matched_text="",
        )

        neut = library.discuss(result)

        if args.summary:
            print(f"\n📝 Summary of: {item.title or args.media_id}\n")
            print(neut.summarize())
        elif args.concepts:
            print(f"\n🔬 Technical concepts in: {item.title or args.media_id}\n")
            print(neut.concepts())
        elif args.actions:
            print(f"\n✅ Action items from: {item.title or args.media_id}\n")
            print(neut.action_items())
        elif args.question:
            print(f"\n🦎 Neut on: {item.title or args.media_id}\n")
            print(f"Q: {args.question}\n")
            print(f"A: {neut.ask(args.question)}")
        else:
            # Interactive mode
            neut.interactive()

    elif action == "stats":
        stats = library.stats()
        print("\n📊 Media Library Stats\n")
        print(f"  Total recordings: {stats['total_items']}")
        print(f"  Audio files:      {stats['audio_count']}")
        print(f"  Video files:      {stats['video_count']}")
        print(f"  With transcripts: {stats.get('with_transcript', 0)}")
        print(f"  With embeddings:  {stats.get('with_embedding', 0)}")
        print(f"  Unique people:    {stats.get('unique_participants', 0)}")
        hours = stats.get('total_duration_hours', 0)
        print(f"  Total duration:   {hours:.1f} hours")
        print()

    elif action == "index":
        print("🔄 Rebuilding media index...\n")
        count = library.rebuild_index()
        print(f"✓ Indexed {count} recording(s)")

    elif action == "list":
        # List recordings
        items = list(library._items.values())

        # Filter by participant if requested
        if args.with_person:
            items = [
                item for item in items
                if any(
                    args.with_person.lower() in p.name.lower() or
                    args.with_person.lower() == p.person_id.lower()
                    for p in item.participants
                )
            ]

        # Sort by recorded_at descending
        items.sort(key=lambda x: x.recorded_at or "", reverse=True)
        items = items[:args.limit]

        if not items:
            print("No recordings found.")
            return

        print(f"\n📼 Recordings ({len(items)} shown)\n")
        for item in items:
            duration = format_duration(item.duration_sec) if item.duration_sec else "?"
            date = item.recorded_at[:10] if item.recorded_at else "unknown"
            participants = ", ".join(p.name for p in item.participants[:3])
            if len(item.participants) > 3:
                participants += f" +{len(item.participants) - 3}"

            print(f"  {item.media_id[:12]}  {date}  {duration:>8}  {item.title or '(untitled)'}")
            if participants:
                print(f"  {'':12}  People: {participants}")

    else:
        print("Usage: axi signal media <search|play|discuss|stats|index|list>")
        print()
        print("Commands:")
        print("  search <query>     Search recordings by keyword or concept")
        print("  play <id>          Play a recording or segment")
        print("  discuss <id>       Discuss a recording with Neut")
        print("  stats              Show library statistics")
        print("  index              Rebuild the media index")
        print("  list               List all indexed recordings")
        print()
        print("Examples:")
        print("  axi signal media search heat exchangers --discuss")
        print("  axi signal media discuss abc123 --summary")
        print("  axi signal media discuss abc123 -q 'What was the decision?'")


def cmd_timestamps(args):
    """Regenerate word-level timestamps for transcripts.

    This enables precise audio clip extraction during correction review.
    """
    import json
    from pathlib import Path

    processed_dir = INBOX_PROCESSED
    raw_voice_dir = _RUNTIME_DIR / "inbox" / "raw" / "voice"

    # Find transcripts that need timestamps
    transcripts = list(processed_dir.glob("*_transcript.md"))

    if args.file:
        # Filter to specific file
        target = Path(args.file)
        if not target.exists():
            target = processed_dir / args.file
        transcripts = [t for t in transcripts if t == target or t.name == args.file]
        if not transcripts:
            print(f"Transcript not found: {args.file}")
            return

    # Identify which need timestamps
    to_process = []
    for transcript in transcripts:
        stem = transcript.stem.replace("_transcript", "")
        timestamps_path = transcript.parent / f"{stem}_timestamps.json"

        # Find audio file
        audio_path = None
        for ext in [".m4a", ".mp3", ".wav", ".webm"]:
            candidate = raw_voice_dir / f"{stem}{ext}"
            if candidate.exists():
                audio_path = candidate
                break

        if not audio_path:
            continue

        needs_regen = not timestamps_path.exists() or args.all
        if needs_regen:
            to_process.append((transcript, audio_path, timestamps_path))

    if not to_process:
        print("All transcripts already have timestamps.")
        return

    print(f"Found {len(to_process)} transcript(s) needing timestamps:")
    for transcript, audio, _ in to_process:
        print(f"  - {transcript.name} (audio: {audio.name})")

    if args.dry_run:
        print("\n(dry-run: no changes made)")
        return

    # Check for whisper
    try:
        import whisper
    except ImportError:
        print("\nError: openai-whisper not installed.")
        print("Install with: pip install openai-whisper")
        return

    print(f"\nLoading Whisper model '{args.model}'...")
    model = whisper.load_model(args.model)

    for i, (transcript, audio_path, timestamps_path) in enumerate(to_process, 1):
        print(f"\n[{i}/{len(to_process)}] Processing: {audio_path.name}")

        try:
            result = model.transcribe(str(audio_path), word_timestamps=True)  # type: ignore[union-attr]
            segments = result.get("segments", [])  # type: ignore[union-attr]

            word_timestamps = []
            for seg in segments:
                for word_info in seg.get("words", []):  # type: ignore[union-attr]
                    word_timestamps.append({
                        "word": word_info.get("word", ""),
                        "start": word_info.get("start", 0),
                        "end": word_info.get("end", 0),
                    })

            from datetime import datetime
            timestamps_data = {
                "source_audio": str(audio_path),
                "transcript_path": str(transcript),
                "created_at": datetime.now(UTC).isoformat(),
                "word_count": len(word_timestamps),
                "words": word_timestamps,
            }
            timestamps_path.write_text(json.dumps(timestamps_data, indent=2), encoding="utf-8")
            print(f"  Saved {len(word_timestamps)} word timestamps to {timestamps_path.name}")
        except Exception as e:
            print(f"  Error: {e}")

    print("\nDone!")


def cmd_voice(args):
    """Show voice identification status.

    Voice enrollment happens automatically during media review:
    - When reviewing corrections with audio, unknown speakers can be tagged
    - The system learns voices over time as users identify speakers
    - Eventually most team members are auto-identified
    """
    try:
        from .voice_id import VoiceProfileStore
    except ImportError:
        print("🎤 Voice Identification")
        print()
        print("Status: Not available (dependencies not installed)")
        print()
        print("To enable voice identification:")
        print("  pip install pyannote.audio torch torchaudio")
        print("  export HF_TOKEN=<your_huggingface_token>")
        return

    profiles = VoiceProfileStore(_RUNTIME_DIR)
    all_profiles = profiles.list_profiles()

    # Load team roster for comparison
    people_file = _RUNTIME_DIR / "config" / "people.md"
    team_size = 0
    content = ""
    if people_file.exists():
        content = people_file.read_text()
        # Count table rows (skip header row)
        team_size = len([line for line in content.split("\n") if line.startswith("|") and "---" not in line]) - 1

    print("🎤 Voice Identification Status")
    print()

    if not all_profiles:
        print("  Enrolled voices: 0")
        if team_size > 0:
            print(f"  Team roster:     {team_size} people")
        print()
        print("  Voices are learned during media review.")
        print("  Run: axi signal corrections --guided --play")
        print("  When prompted, identify who is speaking.")
    else:
        print(f"  Enrolled voices: {len(all_profiles)}")
        if team_size > 0:
            pct = int(len(all_profiles) / team_size * 100)
            print(f"  Team coverage:   {pct}% ({len(all_profiles)}/{team_size})")
        print()
        print("  Known voices:")
        for p in sorted(all_profiles, key=lambda x: x.person_name):
            samples = p.sample_count
            print(f"    • {p.person_name} ({samples} sample{'s' if samples != 1 else ''})")

        # Show who's missing
        if team_size > len(all_profiles) and people_file.exists():
            enrolled_names = {p.person_name.lower() for p in all_profiles}
            missing = []
            for line in content.split("\n"):
                if line.startswith("|") and "---" not in line and "Name" not in line:
                    parts = line.split("|")
                    if len(parts) > 1:
                        name = parts[1].strip()
                        if name.lower() not in enrolled_names:
                            missing.append(name)
            if missing:
                print()
                print(f"  Not yet enrolled ({len(missing)}):")
                for name in missing[:5]:
                    print(f"    ○ {name}")
                if len(missing) > 5:
                    print(f"    ... and {len(missing) - 5} more")


if __name__ == "__main__":
    main()
