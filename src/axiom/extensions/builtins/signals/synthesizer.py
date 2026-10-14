# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Synthesizer — merges extracted signals into changelog drafts.

Takes all Signal objects from all extractors, groups by initiative and
signal_type, and produces:
1. drafts/changelog_YYYY-MM-DD.md — tab-separated paste-ready format
2. drafts/weekly_summary_YYYY-MM-DD.md — prose summary

Signal staleness tracking:
- Signals are hashed and tracked in signal_manifest.json
- By default, only unreported signals are included in changelogs
- Use include_all=True to see all signals (e.g., for full audit)

Deduplication:
- Exact: signals with the same signal_id (content hash) are merged
- Fuzzy: within the same initiative+type group, signals with very similar
  detail text are merged (keeps highest-confidence version)
- Temporal: within the same initiative+type, keep only the most recent
  signal when two describe the same workflow stage at different times
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import DATE_FORMAT_ISO, Changelog, ChangelogEntry, Signal, SignalManifest

if TYPE_CHECKING:
    from .correlator import Correlator

# Drafts directory: tools/agents/drafts/
from axiom import REPO_ROOT as _REPO_ROOT

_RUNTIME_DIR = _REPO_ROOT / "runtime"
DRAFTS_DIR = _RUNTIME_DIR / "drafts"

# Structural-only sources — signals from these alone mean we need human context
_STRUCTURAL_SOURCES = frozenset({"gitlab_diff", "github_commit", "github"})

# Tone filter patterns: (compiled regex, replacement template)
# Applied to detail text to rewrite blame framing as constructive language.
_TONE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "{Person} is blocking X" → "X is pending — next step: follow up with {Person}"
    (re.compile(r"(\S+)\s+(?:is\s+)?blocking\s+(.+)", re.IGNORECASE),
     r"\2 is pending — next step: follow up with \1"),
    # "{Person} hasn't responded" → "Awaiting response from {Person}"
    (re.compile(r"(\S+)\s+hasn'?t\s+responded?", re.IGNORECASE),
     r"Awaiting response from \1"),
    # "{Person} has not responded" → "Awaiting response from {Person}"
    (re.compile(r"(\S+)\s+has\s+not\s+responded?", re.IGNORECASE),
     r"Awaiting response from \1"),
    # "except for {Person}" → "one response outstanding"
    (re.compile(r"(?:all\s+.+?\s+)?except\s+(?:for\s+)?(\S+)", re.IGNORECASE),
     r"nearing completion — one response outstanding"),
    # "{Person} is late" → "Timeline adjustment needed"
    (re.compile(r"(\S+)\s+is\s+late", re.IGNORECASE),
     r"Timeline adjustment needed"),
    # "{Person} failed to" → "{Person}'s task is pending"
    (re.compile(r"(\S+)\s+failed\s+to\s+(.+)", re.IGNORECASE),
     r"\1's \2 is pending"),
]


class Synthesizer:
    """Merge signals into human-reviewable changelog drafts."""

    def __init__(self, drafts_dir: Path | None = None):
        self.drafts_dir = drafts_dir or DRAFTS_DIR
        self.manifest = SignalManifest()
        self._last_synthesized_signals: list[Signal] = []  # Track for marking as reported

    @staticmethod
    def _normalize_detail(detail: str) -> str:
        """Normalize a detail string for fuzzy comparison."""
        text = detail.lower().strip()
        # Collapse whitespace and punctuation
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _apply_tone_filter(detail: str) -> str:
        """Rewrite negative/blame framing to constructive language.

        Applied universally — not per-person. All synthesized language must be
        direct, polite, constructive, and respectful of everyone.
        """
        result = detail
        for pattern, replacement in _TONE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    @staticmethod
    def _deduplicate_signals(signals: list[Signal]) -> list[Signal]:
        """Remove duplicate signals.

        Four-pass dedup:
        1. Exact: signals with the same signal_id (content hash) — keep highest confidence.
        2. Commit count merge: signals with metadata.event=="commit_summary" for the
           same resolved person are merged (counts summed).
        3. Fuzzy: signals with very similar normalized detail text — keep longest detail.
        4. Temporal: within same (initiative, type), if two signals describe the same
           workflow stage at different times, keep only the most recent.
        """
        if not signals:
            return signals

        # Pass 1: Deduplicate by signal_id
        by_id: dict[str, Signal] = {}
        for signal in signals:
            sid = signal.signal_id
            if sid in by_id:
                if signal.confidence > by_id[sid].confidence:
                    by_id[sid] = signal
            else:
                by_id[sid] = signal
        deduped = list(by_id.values())

        # Pass 2: Merge commit_summary signals by resolved person
        commit_signals: dict[str, Signal] = {}  # person → merged signal
        other_signals: list[Signal] = []
        for signal in deduped:
            if signal.metadata.get("event") == "commit_summary" and signal.people:
                person = signal.people[0]
                if person in commit_signals:
                    existing = commit_signals[person]
                    old_count = existing.metadata.get("count", 0)
                    new_count = signal.metadata.get("count", 0)
                    total = old_count + new_count
                    existing.metadata["count"] = total
                    existing.detail = f"{person}: {total} commits in the export period"
                    existing.raw_text = f"{person}: {total} commits"
                else:
                    commit_signals[person] = signal
            else:
                other_signals.append(signal)
        deduped = other_signals + list(commit_signals.values())

        # Pass 3: Fuzzy dedup within same (initiative_set, signal_type) groups
        # Group by (frozenset of initiatives, signal_type)
        groups: dict[tuple, list[Signal]] = defaultdict(list)
        for signal in deduped:
            key = (frozenset(signal.initiatives or ["Uncategorized"]), signal.signal_type)
            groups[key].append(signal)

        result = []
        for group_signals in groups.values():
            if len(group_signals) <= 1:
                result.extend(group_signals)
                continue

            # Within each group, merge signals with similar normalized detail
            merged: list[Signal] = []
            used = set()
            for i, sig_a in enumerate(group_signals):
                if i in used:
                    continue
                norm_a = Synthesizer._normalize_detail(sig_a.detail)
                # Find all similar signals
                cluster = [sig_a]
                for j, sig_b in enumerate(group_signals):
                    if j <= i or j in used:
                        continue
                    norm_b = Synthesizer._normalize_detail(sig_b.detail)
                    if Synthesizer._is_similar(norm_a, norm_b):
                        cluster.append(sig_b)
                        used.add(j)
                # Keep the version with the longest detail (most informative)
                best = max(cluster, key=lambda s: (s.confidence, len(s.detail)))
                # Merge people from all clustered signals
                all_people: list[str] = []
                seen_people: set[str] = set()
                for s in cluster:
                    for p in s.people:
                        if p not in seen_people:
                            all_people.append(p)
                            seen_people.add(p)
                best.people = all_people
                merged.append(best)
            result.extend(merged)

        # Pass 4: Temporal dedup — within same (initiative, type), if multiple signals
        # describe the same workflow stage at different timestamps, keep only the most recent.
        temporal_groups: dict[tuple, list[Signal]] = defaultdict(list)
        for signal in result:
            key = (frozenset(signal.initiatives or ["Uncategorized"]), signal.signal_type)
            temporal_groups[key].append(signal)

        final_result = []
        for group_signals in temporal_groups.values():
            if len(group_signals) <= 1:
                final_result.extend(group_signals)
                continue

            # Sort by timestamp descending, keep only the most recent for each
            # normalized detail prefix (first 4 words as a workflow stage proxy)
            seen_stages: dict[str, Signal] = {}
            group_sorted = sorted(group_signals, key=lambda s: s.timestamp, reverse=True)
            for sig in group_sorted:
                stage_key = " ".join(Synthesizer._normalize_detail(sig.detail).split()[:4])
                if stage_key not in seen_stages:
                    seen_stages[stage_key] = sig
            final_result.extend(seen_stages.values())

        return final_result

    @staticmethod
    def _is_similar(a: str, b: str, threshold: float = 0.80) -> bool:
        """Check if two normalized strings are similar enough to merge.

        Uses token overlap (Jaccard similarity on words) which is fast and
        works well for the near-duplicate case (same fact, slightly different wording).
        """
        if not a or not b:
            return a == b
        tokens_a = set(a.split())
        tokens_b = set(b.split())
        if not tokens_a or not tokens_b:
            return False
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union) >= threshold

    def synthesize(
        self,
        signals: list[Signal],
        date: str | None = None,
        include_all: bool = False,
        correlator: Correlator | None = None,
    ) -> Changelog:
        """Group signals by initiative/type and build a Changelog.

        Args:
            signals: All signals to consider.
            date: Changelog date (default: today).
            include_all: If True, include already-reported signals (marked with dagger).
                        If False (default), only include new/unreported signals.
            correlator: Optional Correlator for initiative metadata (strategic weight,
                       pause reasons, owners). Enables strategic ordering and
                       beneficiary tagging in the weekly summary.

        Returns:
            Changelog with filtered entries.
        """
        if date is None:
            date = datetime.now(UTC).strftime(DATE_FORMAT_ISO)

        # Filter out already-reported signals unless include_all is True
        total_signals = len(signals)
        if include_all:
            filtered_signals = signals
            skipped = 0
        else:
            filtered_signals = self.manifest.filter_unreported(signals)
            skipped = total_signals - len(filtered_signals)

        # Deduplicate signals before grouping
        filtered_signals = self._deduplicate_signals(filtered_signals)

        # Apply tone filter to all signal details
        for signal in filtered_signals:
            signal.detail = self._apply_tone_filter(signal.detail)

        # Track which signals we're including so we can mark them as reported
        self._last_synthesized_signals = filtered_signals.copy()
        self._last_changelog_date = date

        # Build initiative lookup from correlator if available
        init_lookup: dict[str, object] = {}
        if correlator:
            for init in correlator.initiatives:
                init_lookup[init.name] = init

        # Group signals by initiative
        by_initiative: dict[str, dict[str, list[Signal]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for signal in filtered_signals:
            initiatives = signal.initiatives if signal.initiatives else ["Uncategorized"]
            for init_name in initiatives:
                by_initiative[init_name][signal.signal_type].append(signal)

        # Sort initiatives by strategic weight (desc), then alphabetically
        def _init_sort_key(name: str) -> tuple:
            init = init_lookup.get(name)
            weight = getattr(init, "strategic_weight", 3) if init else 3
            return (-weight, name)

        sorted_initiatives = sorted(by_initiative.keys(), key=_init_sort_key)

        # Build changelog entries in strategic order
        entries = []
        for initiative in sorted_initiatives:
            type_groups = by_initiative[initiative]
            for signal_type in sorted(type_groups.keys()):
                type_signals = type_groups[signal_type]
                for signal in type_signals:
                    entries.append(ChangelogEntry(
                        initiative=initiative,
                        signal_type=signal_type,
                        detail=signal.detail,
                        people=signal.people,
                        sources=[signal.source],
                        confidence=signal.confidence,
                    ))

        # Build summary
        new_count = len(filtered_signals)
        init_count = len(by_initiative)
        people_set = set()
        for s in filtered_signals:
            people_set.update(s.people)

        if skipped > 0:
            summary = (
                f"Week of {date}: {new_count} new signals across {init_count} initiatives, "
                f"involving {len(people_set)} people. ({skipped} previously reported signals omitted)"
            )
        else:
            summary = (
                f"Week of {date}: {new_count} signals across {init_count} initiatives, "
                f"involving {len(people_set)} people."
            )

        return Changelog(date=date, entries=entries, summary=summary)

    def mark_as_reported(self) -> int:
        """Mark all signals from the last synthesize() as reported.

        Call this after successfully writing the changelog to prevent
        the same signals from appearing in future changelogs.

        Returns:
            Number of signals marked as reported.
        """
        if not self._last_synthesized_signals:
            return 0

        self.manifest.mark_batch_reported(
            self._last_synthesized_signals,
            self._last_changelog_date,
        )
        count = len(self._last_synthesized_signals)
        self._last_synthesized_signals = []  # Clear after marking
        return count

    def get_reported_count(self) -> int:
        """Return total number of signals that have been reported."""
        return self.manifest.get_reported_count()

    def write_changelog(self, changelog: Changelog) -> Path:
        """Write changelog to drafts/ in tab-separated format."""
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        path = self.drafts_dir / f"changelog_{changelog.date}.md"

        lines = [
            f"# Changelog — {changelog.date}",
            "",
            f"> {changelog.summary}",
            "",
            "| Initiative | Type | Detail | People | Source | Confidence |",
            "|-----------|------|--------|--------|--------|------------|",
        ]

        for entry in changelog.entries:
            people_str = ", ".join(entry.people) if entry.people else "—"
            sources_str = ", ".join(entry.sources) if entry.sources else "—"
            detail_clean = entry.detail.replace("|", "/").replace("\n", " ")
            lines.append(
                f"| {entry.initiative} | {entry.signal_type} | "
                f"{detail_clean} | {people_str} | {sources_str} | "
                f"{entry.confidence:.1f} |"
            )

        lines.append("")
        lines.append(f"*Generated: {changelog.generated_at}*")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def write_weekly_summary(
        self,
        changelog: Changelog,
        correlator: Correlator | None = None,
        blocker_tracker: Any | None = None,
    ) -> Path:
        """Write a prose weekly summary to drafts/.

        Args:
            changelog: Synthesized changelog.
            correlator: Optional Correlator for initiative metadata.
            blocker_tracker: Optional BlockerTracker for active blockers section.
        """
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        path = self.drafts_dir / f"weekly_summary_{changelog.date}.md"

        # Build initiative lookup from correlator if available
        init_lookup: dict[str, object] = {}
        if correlator:
            for init in correlator.initiatives:
                init_lookup[init.name] = init

        # Group entries by initiative for prose
        by_initiative: dict[str, list[ChangelogEntry]] = defaultdict(list)
        for entry in changelog.entries:
            by_initiative[entry.initiative].append(entry)

        # Sort initiatives by strategic weight (desc), then alphabetically
        def _init_sort_key(name: str) -> tuple:
            init = init_lookup.get(name)
            weight = getattr(init, "strategic_weight", 3) if init else 3
            return (-weight, name)

        sorted_initiatives = sorted(by_initiative.keys(), key=_init_sort_key)

        lines = [
            f"# Weekly Summary — {changelog.date}",
            "",
            changelog.summary,
            "",
        ]

        # Active Blockers section (before per-initiative sections)
        if blocker_tracker:
            active_blockers = blocker_tracker.get_active_blockers()
            if active_blockers:
                lines.append("## Active Blockers")
                lines.append("")
                # Cross-cutting blockers first (already sorted by tracker)
                for blocker in active_blockers:
                    scope = ""
                    if blocker.is_cross_cutting:
                        scope = f" (affects: {', '.join(blocker.initiatives)})"
                    age = ""
                    if blocker.times_reported > 1:
                        age = f" [reported {blocker.times_reported}x]"
                    action = ""
                    if blocker.proposed_action:
                        action = f" — Suggested: {blocker.proposed_action}"
                    lines.append(f"- {blocker.detail}{scope}{age}{action}")
                lines.append("")

        type_labels = {
            "progress": "Progress",
            "blocker": "Blockers",
            "decision": "Decisions",
            "action_item": "Action Items",
            "status_change": "Status Changes",
            "raw": "Notes",
        }

        for initiative in sorted_initiatives:
            entries = by_initiative[initiative]
            init_meta = init_lookup.get(initiative)

            # Build initiative header with annotations
            header = f"## {initiative}"
            annotations: list[str] = []

            if init_meta:
                pause_reason = getattr(init_meta, "pause_reason", "")
                status = getattr(init_meta, "status", "Active")

                if pause_reason:
                    annotations.append(f"Paused: {pause_reason}")
                elif status.lower() == "stale":
                    annotations.append("Needs attention")

                # Needs-color flag: only structural signals, no human context
                sources = {e.sources[0] for e in entries if e.sources}
                if sources and sources <= _STRUCTURAL_SOURCES:
                    annotations.append("Structural signals only — needs context")

            if annotations:
                header += f" ({'; '.join(annotations)})"

            lines.append(header)
            lines.append("")

            # Group by type within initiative
            by_type: dict[str, list[ChangelogEntry]] = defaultdict(list)
            for entry in entries:
                by_type[entry.signal_type].append(entry)

            for signal_type, label in type_labels.items():
                type_entries = by_type.get(signal_type, [])
                if not type_entries:
                    continue

                lines.append(f"### {label}")
                lines.append("")
                for entry in type_entries:
                    people = f" ({', '.join(entry.people)})" if entry.people else ""
                    detail = self._apply_tone_filter(entry.detail)

                    # Beneficiary tagging for action items
                    beneficiary_tag = ""
                    if signal_type == "action_item" and init_meta:
                        owners = getattr(init_meta, "owners", [])
                        if owners:
                            beneficiary_tag = f" — Benefits: {', '.join(owners)}"

                    lines.append(f"- {detail}{people}{beneficiary_tag}")
                lines.append("")

        lines.append(f"*Generated: {changelog.generated_at}*")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path
