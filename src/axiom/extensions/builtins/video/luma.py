# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The reduction both built-in triggers take a frame through: a block mean, then luma.

``block_luma(array, k)`` averages ``k`` by ``k`` blocks of an H×W×3 ``uint8`` RGB
array per channel and returns their BT.601 luma as ``float32``, one value per block
(a remainder of fewer than ``k`` rows or columns is left out). It sums in integers,
rows by a reshape and columns by adding ``k`` strided slices, which is exact and
about 5 ms for a 4K frame at ``k = 12`` and 13 ms at ``k = 4``; summing the columns by
a reshape too took three times as long at every ``k``, and a float mean over the
whole frame about 120 ms. ``motion`` picks ``k`` so the longer side is at most 320 px;
``reference-match`` takes the factor its reference set's working size gives.
numpy is imported when a frame is reduced, not at import.
"""

from __future__ import annotations

from typing import Any

from .frame import require_numpy

#: ITU-R BT.601 luma, the grayscale every camera's "Y" is.
LUMA = (0.299, 0.587, 0.114)


def block_luma(array: Any, k: int) -> Any:
    """The luma of the ``k`` by ``k`` block means of an RGB ``uint8`` array, as ``float32``."""
    np = require_numpy()
    weights = np.asarray(LUMA, dtype=np.float32)
    if k <= 1:
        return array.astype(np.float32) @ weights
    height, width = int(array.shape[0]), int(array.shape[1])
    rows, cols = height // k, width // k
    block = array[: rows * k, : cols * k]
    widest = np.iinfo(np.uint16).max
    first = np.uint16 if k * 255 <= widest else np.uint32
    second = np.uint16 if k * k * 255 <= widest else np.uint32
    summed = block.reshape(rows, k, cols * k, 3).sum(axis=1, dtype=first)
    total = summed[:, 0::k].astype(second)
    for j in range(1, k):
        total += summed[:, j::k]
    return (total.astype(np.float32) @ weights) / float(k * k)


__all__ = ["LUMA", "block_luma"]
