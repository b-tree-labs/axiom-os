# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What an answer carries so its chart draws at once: one bounded block.

The block is :func:`scidisplay.suggest.to_block` plus two things that are
about transport, not about charts:

- **The first chart, already drawn.** The top suggestion's SVG rides in the
  block, so the browser shows a chart the moment the answer arrives, without
  a round trip. Every other kind is drawn from the same data by the same
  renderer when somebody picks it.
- **A bound.** The data is decimated to the width it will be drawn at. If the
  block is still over :data:`DATA_CAP_BYTES` it is decimated harder, and if it
  cannot fit at all its datasets are held here and the block carries a
  ``data_ref`` the browser fetches once (``GET /api/v1/charts/data/<ref>``).
  This cap is separate from the 16 KiB view-context cap: that one bounds what
  the page tells the model; this one bounds what an answer hands the page.

The data in a block is what the chart draws: the series the answer read,
decimated. It is the same reading the answer's prose quotes, for the person
who asked, never the raw tool result.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from axiom.extensions.builtins.scidisplay import suggest as sg

#: Bytes of data an answer carries inline (see the module doc).
DATA_CAP_BYTES = sg.DATA_CAP_BYTES
#: Widths tried, widest first, before the data goes by reference.
_WIDTHS = (720, 480, 320, 200)
#: Held datasets: how many, and for how long.
REF_CAPACITY = 256
REF_TTL_S = 3600.0

_lock = threading.Lock()
_held: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def hold(ref: str, datasets: Mapping[str, Any]) -> None:
    with _lock:
        _held[ref] = (time.monotonic(), dict(datasets))
        _held.move_to_end(ref)
        while len(_held) > REF_CAPACITY:
            _held.popitem(last=False)


def fetch(ref: str) -> dict[str, Any] | None:
    with _lock:
        got = _held.get(ref)
        if got is None:
            return None
        if time.monotonic() - got[0] > REF_TTL_S:
            del _held[ref]
            return None
        return got[1]


def _prerender(block: dict[str, Any], width: int) -> None:
    from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

    if not block["suggestions"]:
        return
    first = block["suggestions"][0]
    spec = sg.spec_of(block, first["id"])
    if spec is None:
        return
    try:
        drawn = render_spec(spec, width=width)
    except Exception:  # noqa: BLE001 - the browser draws it instead; never lose the answer
        return
    first["svg"] = drawn.svg
    first["width"] = width
    if drawn.readout:
        first["readout"] = drawn.readout
    if drawn.notes:
        first["notes"] = list(drawn.notes)


def build(
    result: Any,
    question: str = "",
    *,
    title: str = "",
    prefer: Sequence[str] = (),
    reference: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    width: int = sg.DEFAULT_WIDTH,
    cap: int = DATA_CAP_BYTES,
    prerender: bool = True,
) -> dict[str, Any] | None:
    """The bounded block for *result*, or None when nothing draws honestly."""
    widths = [w for w in _WIDTHS if w <= width] or [width]
    if widths[0] != width:
        widths.insert(0, width)
    block: dict[str, Any] | None = None
    for w in widths:
        s = sg.suggest(result, question, title=title, prefer=prefer, reference=reference, width=w)
        if not s:
            return None
        block = sg.to_block(s, source=source, question=question)
        if sg.block_bytes({"datasets": block["datasets"]}) <= cap:
            break
    assert block is not None
    if sg.block_bytes({"datasets": block["datasets"]}) > cap:
        ref = sg._digest(block["datasets"])  # noqa: SLF001 - one digest for one block
        hold(ref, block["datasets"])
        block["data_ref"] = ref
        block["data_bytes"] = sg.block_bytes({"datasets": block["datasets"]})
        if prerender:
            _prerender(block, width)
        block["datasets"] = {}
        return block
    if prerender:
        _prerender(block, width)
    return block


__all__ = ["DATA_CAP_BYTES", "build", "fetch", "hold"]
