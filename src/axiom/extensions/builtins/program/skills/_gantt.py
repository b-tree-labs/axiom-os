# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program schedule as a Gantt PNG — a committed image, never Mermaid.

One row per schedule item, grouped by lane in the file's lane order: a span
item is a bar from ``start`` to ``end`` with its ``pct`` filled, a point item
is a diamond on its ``date``. A vertical rule marks the program's ``as_of``.

The output is **byte-stable**: the same data file renders the same bytes (no
"today" line, no timestamp or software tag in the PNG metadata), so a
publisher can compare and skip a commit when nothing changed.

matplotlib is optional (it ships with the ``math`` extra); without it
:func:`render_gantt_png` returns ``None`` and the caller says so.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any

from ..model import ProgramData

_PALETTE = ("#3b6ea5", "#c2702e", "#4a8f5c", "#8a5ca8", "#a8505c", "#5c8f9a")


def _span(item: dict[str, Any]) -> tuple[date, date] | None:
    try:
        if "start" in item and "end" in item:
            return date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if "date" in item:
            day = date.fromisoformat(item["date"])
            return day, day
    except (TypeError, ValueError):
        return None
    return None


def _rows(data: ProgramData) -> list[tuple[str, dict[str, Any]]]:
    """(lane id, item) in lane order, unlaned last, file order within a lane."""
    order = {lane_id: i for i, lane_id in enumerate(data.lane_ids())}
    indexed = [
        (order.get(item.get("lane"), len(order)), i, item)
        for i, item in enumerate(data.schedule)
        if isinstance(item, dict) and _span(item) is not None
    ]
    indexed.sort(key=lambda t: (t[0], t[1]))
    return [(str(item.get("lane") or ""), item) for _o, _i, item in indexed]


def render_gantt_png(data: ProgramData) -> bytes | None:
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — optional dependency
        return None

    rows = _rows(data)
    lane_colors = {lane: _PALETTE[i % len(_PALETTE)] for i, lane in enumerate(data.lane_ids())}
    height = max(2.0, 0.42 * len(rows) + 1.2)
    with plt.rc_context({"svg.hashsalt": "axiom-program", "font.size": 9}):
        fig, ax = plt.subplots(figsize=(10, height), dpi=100)
        try:
            for y, (lane, item) in enumerate(rows):
                start, end = _span(item)  # type: ignore[misc]
                color = lane_colors.get(lane, "#6b7680")
                if start == end and "date" in item:
                    ax.plot(mdates.date2num(start), y, marker="D", color=color, markersize=7)
                    continue
                left = mdates.date2num(start)
                width = max(1.0, mdates.date2num(end) - left + 1)
                ax.barh(y, width, left=left, height=0.6, color=color, alpha=0.3)
                pct = item.get("pct")
                if isinstance(pct, int) and pct > 0:
                    ax.barh(y, width * pct / 100.0, left=left, height=0.6, color=color)
            ax.set_yticks(range(len(rows)))
            ax.set_yticklabels([str(item.get("label", item.get("id"))) for _l, item in rows])
            ax.invert_yaxis()
            ax.xaxis_date()
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
            as_of = data.program.get("as_of")
            if isinstance(as_of, str):
                try:
                    ax.axvline(mdates.date2num(date.fromisoformat(as_of)), color="#888888", linestyle="--", linewidth=1)
                except ValueError:
                    pass
            ax.set_title(str(data.program.get("name", data.program.get("id", "Program"))))
            ax.grid(axis="x", color="#dddddd", linewidth=0.6)
            fig.autofmt_xdate()
            fig.tight_layout()
            buffer = io.BytesIO()
            # metadata={"Software": None} drops the version tag, so the bytes
            # depend only on the data.
            fig.savefig(buffer, format="png", metadata={"Software": None})
            return buffer.getvalue()
        finally:
            plt.close(fig)


__all__ = ["render_gantt_png"]
