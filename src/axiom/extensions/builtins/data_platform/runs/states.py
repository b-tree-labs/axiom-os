# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A state channel is a segment stream that has not been collapsed yet.

Plants report what they are doing. A reactor console has a mode annunciator, a
loop has a controller state, a rig has a test phase — and all of them arrive
the same way: one discrete value per sample, repeated until it changes.

Stored that way it is unusable and enormous. Half a billion rows carrying the
same handful of strings answers no question anyone asks; "when was it at steady
state" means run-length encoding it first, every single time, in whatever
notebook happens to be open. Collapsed once into segments it is a few thousand
rows and the question is a lookup.

This is deliberately domain-blind. It knows nothing about reactors or modes: it
takes timestamped states and a mapping to the segment vocabulary, and returns
segments. What a given plant's states *mean* belongs to that plant's package,
the same way a channel's role does.

**A state nobody has mapped is kept, not dropped.** It becomes an `unknown`
segment carrying the raw value in its notes, so the window still exists on a
chart and the gap is visible to whoever can close it. Discarding it would make
an unmapped state indistinguishable from a period where the plant said nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import SEGMENT_LABELS, RunSegment


@dataclass
class StateCollapse:
    """Segments, plus what the mapping did not cover."""

    segments: list[RunSegment] = field(default_factory=list)
    #: raw state -> how many samples carried it, for states with no mapping.
    #: Reported so an unmapped vocabulary is a visible gap rather than a silent
    #: bucket of `unknown`.
    unmapped: dict[str, int] = field(default_factory=dict)
    #: Samples ignored because the state was one of ``absent``.
    absent_samples: int = 0

    @property
    def report(self) -> list[str]:
        out = [f"{len(self.segments)} segment(s) from state changes"]
        if self.unmapped:
            listed = ", ".join(f"{k!r}x{v}" for k, v in sorted(self.unmapped.items()))
            out.append(
                f"{len(self.unmapped)} state(s) have no label mapping and became "
                f"'unknown' with the raw value kept: {listed}"
            )
        if self.absent_samples:
            out.append(f"{self.absent_samples} sample(s) reported no state")
        return out


def collapse_states(
    samples: Iterable[tuple[datetime, object]],
    label_map: Mapping[str, str],
    *,
    absent: Sequence[object] = (None, "", "None", "none"),
    max_gap: timedelta | None = None,
    segment_prefix: str = "seg",
) -> StateCollapse:
    """Run-length encode ``(timestamp, state)`` samples into segments.

    ``absent`` lists states that mean "nothing reported". The default includes
    the **string** ``"None"`` as well as the real ``None``, because a feed that
    writes the word rather than a null is common and the two are impossible to
    tell apart downstream — a column where 99.998% of rows say ``'None'`` is
    not a column where every row has a state.

    ``max_gap`` closes a segment when consecutive samples are further apart
    than this. Without it a feed that stops for a week and resumes in the same
    state produces one segment spanning the outage, which asserts the plant
    held that state throughout — a claim the data does not make.

    Segments end at the first sample of the *next* state, so a segment's span
    is the interval the state actually covered rather than the timestamp of its
    last identical sample.
    """
    absent_set = {a for a in absent}
    ordered = sorted(samples, key=lambda pair: pair[0])

    out = StateCollapse()
    open_label: str | None = None
    open_raw: str | None = None
    open_start: datetime | None = None
    previous_ts: datetime | None = None
    index = 0

    def close(end: datetime | None) -> None:
        nonlocal index, open_label, open_raw, open_start
        if open_label is None or open_start is None:
            return
        index += 1
        out.segments.append(RunSegment(
            segment_id=f"{segment_prefix}-{index:05d}",
            label=open_label,
            started_at=open_start,
            ended_at=end,
            label_source="recorded",
            notes=None if open_label != "unknown" else f"unmapped state: {open_raw!r}",
        ))
        open_label = open_raw = open_start = None

    for ts, raw in ordered:
        if raw in absent_set:
            out.absent_samples += 1
            close(ts)
            previous_ts = ts
            continue

        key = str(raw)
        label = label_map.get(key)
        if label is None:
            out.unmapped[key] = out.unmapped.get(key, 0) + 1
            label = "unknown"
        elif label not in SEGMENT_LABELS:
            raise ValueError(
                f"label_map sends {key!r} to {label!r}, which is not one of "
                f"{', '.join(SEGMENT_LABELS)}"
            )

        gapped = (
            max_gap is not None
            and previous_ts is not None
            and ts - previous_ts > max_gap
        )
        if open_label is not None and (label != open_label or open_raw != key or gapped):
            close(previous_ts if gapped else ts)

        if open_label is None:
            open_label, open_raw, open_start = label, key, ts
        previous_ts = ts

    close(previous_ts)
    return out


__all__ = ["StateCollapse", "collapse_states"]
