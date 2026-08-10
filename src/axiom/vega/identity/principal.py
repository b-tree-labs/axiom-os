# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Principal: a named, public-keyed entity (human, agent, node, org).

Handle grammar (ADR-020 §"Principal Naming Convention"):
    @name            — principal in home context
    @name:context    — principal qualified to a context
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HANDLE_RE = re.compile(r"^@([A-Za-z0-9_\-\.]+)(?::([A-Za-z0-9_\-\.]+))?$")


@dataclass
class Principal:
    handle: str
    public_bytes: bytes

    def __post_init__(self) -> None:
        if not self.handle.startswith("@"):
            raise ValueError(f"handle must start with '@': {self.handle!r}")
        # Reject fediverse double-@ form explicitly with a clear error.
        if self.handle.count("@") > 1:
            raise ValueError(
                f"handle uses fediverse double-@ form; use matrix-style "
                f"@name:context instead: {self.handle!r}"
            )
        if not _HANDLE_RE.match(self.handle):
            raise ValueError(f"invalid handle shape: {self.handle!r}")

    @property
    def name(self) -> str:
        m = _HANDLE_RE.match(self.handle)
        assert m is not None  # guaranteed by __post_init__
        return m.group(1)

    @property
    def context(self) -> str | None:
        m = _HANDLE_RE.match(self.handle)
        assert m is not None
        return m.group(2)
