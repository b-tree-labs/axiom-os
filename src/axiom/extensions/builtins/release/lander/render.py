# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Text form of a backlog report. The JSON form is ``BacklogReport.to_dict``."""

from __future__ import annotations

from axiom.extensions.builtins.release.lander.report import BacklogReport


def _members(numbers: tuple[int, ...] | list[int]) -> str:
    return ", ".join(f"#{number}" for number in numbers)


def render_text(report: BacklogReport) -> str:
    lines = [
        f"repo: {report.repo}",
        f"mode: {report.mode} (propose and report only)",
        (
            f"suite: {report.suite_minutes} minutes ({report.suite_minutes_source}); "
            f"open: {report.open_count}; estimated minutes saved: {report.minutes_saved}"
        ),
        "",
        f"proposed batches ({len(report.batches)}):",
    ]
    if not report.batches:
        lines.append("  none inside the window")
    for batch in report.batches:
        why = ", ".join(batch.reasons) or batch.key
        lines.append(f"  {batch.key} [{why}] {_members(batch.members)}")
    lines.append("")
    lines.append(f"families outside a single window ({len(report.families)}):")
    if not report.families:
        lines.append("  none")
    for family in report.families:
        why = ", ".join(family.reasons) or family.key
        lines.append(
            f"  {family.key} [{why}] span {family.span_hours:.1f}h {_members(family.members)}"
        )
    lines.append("")
    lines.append(f"superseded or duplicate ({len(report.superseded)}):")
    if not report.superseded:
        lines.append("  none")
    for item in report.superseded:
        lines.append(f"  #{item.number} {item.reason}; keep #{item.keeper}")
    lines.append("")
    lines.append(f"ready to land ({len(report.ready)}):")
    if not report.ready:
        lines.append("  none")
    for item in report.ready:
        lines.append(
            f"  #{item.number} {item.head_ref} {item.checks} approval={item.approval} {item.title}"
        )
    lines.append("")
    lines.append(f"needs a person ({len(report.needs_human)}):")
    if not report.needs_human:
        lines.append("  none")
    for item in report.needs_human:
        why = ", ".join(item.reasons) or "unspecified"
        repair = f" repair={item.repair}" if item.repair else ""
        lines.append(f"  #{item.number} {why}{repair} {item.title}")
    if report.waiting_on_ci:
        lines.append("")
        lines.append(f"waiting on CI ({len(report.waiting_on_ci)}):")
        for item in report.waiting_on_ci:
            lines.append(f"  #{item.number} {item.head_ref}")
    if report.blocked_by_closer:
        lines.append("")
        lines.append(f"held by the auto-closer: {_members(report.blocked_by_closer)}")
    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"note: {note}")
    return "\n".join(lines) + "\n"
