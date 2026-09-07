# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Embedding-space registry (ADR-087 D6).

Every embedding space is a disposable projection keyed ``(model, dim)``. The
**canonical** space is always maintained; **secondary** spaces embed lazily,
only on demand. Where the embedder supports it, a smaller space is a Matryoshka
truncation of the canonical vector (prefix + renormalize) rather than a second
model. A **query-time embed endpoint** (:meth:`embed_query`) means a consumer
never has to know which space to match — it asks for text embedded and gets the
canonical space by default.

Dedup (P2) is pinned to the canonical space via :meth:`pinned_embedder`, so
near-dup / conflict tier decisions are reproducible instead of silently
model-dependent (resolves P2 open question 2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Embedder = Callable[[list[str]], "list[list[float]] | None"]


@dataclass(frozen=True)
class EmbeddingSpace:
    """A projection space keyed ``(model, dim)``.

    ``matryoshka`` marks that vectors from this model's native space may be
    truncated to ``dim`` (prefix + renormalize). A canonical space has
    ``dim == native dim`` so no truncation occurs.
    """

    model: str
    dim: int
    matryoshka: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return (self.model, self.dim)


def _renormalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


@dataclass
class EmbeddingSpaceRegistry:
    """Registry of embedding spaces with a pinned canonical space."""

    canonical: EmbeddingSpace
    embedder: Embedder
    _spaces: dict[tuple[str, int], tuple[EmbeddingSpace, Embedder]] = field(
        default_factory=dict, repr=False,
    )

    def __post_init__(self) -> None:
        self._spaces[self.canonical.key] = (self.canonical, self.embedder)

    # ---- registration ------------------------------------------------------

    def register_secondary(self, space: EmbeddingSpace, *, embedder: Embedder) -> None:
        """Register a secondary space. It embeds lazily — only when requested."""
        self._spaces[space.key] = (space, embedder)

    def resolve(self, model: str, dim: int) -> EmbeddingSpace:
        """Look up a registered space by ``(model, dim)``."""
        entry = self._spaces.get((model, dim))
        if entry is None:
            raise KeyError(
                f"no registered embedding space ({model!r}, {dim}); "
                f"known: {sorted(self._spaces)}"
            )
        return entry[0]

    # ---- embedding ---------------------------------------------------------

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[list[float]] | None:
        """Embed ``texts`` in a registered space (default: canonical).

        Applies Matryoshka truncation when the target space is smaller than the
        embedder's native output and the space supports it. Returns ``None``
        when the underlying embedder is unavailable (degrades, never breaks).
        """
        space, embedder = self._select(model, dim)
        vectors = embedder(texts)
        if vectors is None:
            return None
        return [self._project(v, space) for v in vectors]

    def embed_query(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dim: int | None = None,
    ) -> list[list[float]] | None:
        """Query-time embed endpoint — consumers never match spaces themselves.

        Identical to :meth:`embed`; named for the transport surface (a consumer
        asks "embed this query" and gets the canonical space by default).
        """
        return self.embed(texts, model=model, dim=dim)

    def pinned_embedder(self) -> Embedder:
        """A plain ``list[str] -> list[list[float]] | None`` pinned to canonical.

        Handed to ``DedupEngine`` and ``RecallIndex`` so matching + recall
        always happen in the one canonical space (OQ2).
        """
        return lambda texts: self.embed(texts)

    # ---- internals ---------------------------------------------------------

    def _select(
        self, model: str | None, dim: int | None
    ) -> tuple[EmbeddingSpace, Embedder]:
        if model is None and dim is None:
            return (self.canonical, self.embedder)
        resolved_model = model if model is not None else self.canonical.model
        resolved_dim = dim if dim is not None else self.canonical.dim
        entry = self._spaces.get((resolved_model, resolved_dim))
        if entry is None:
            raise KeyError(
                f"no registered embedding space ({resolved_model!r}, "
                f"{resolved_dim}); known: {sorted(self._spaces)}"
            )
        return entry

    @staticmethod
    def _project(vec: list[float], space: EmbeddingSpace) -> list[float]:
        if len(vec) == space.dim:
            return vec
        if len(vec) > space.dim:
            if not space.matryoshka:
                raise ValueError(
                    f"space ({space.model!r}, {space.dim}) is not matryoshka but "
                    f"the embedder produced {len(vec)} dims — cannot truncate"
                )
            return _renormalize(vec[: space.dim])
        raise ValueError(
            f"embedder produced {len(vec)} dims < space dim {space.dim}; "
            "cannot expand a vector (Matryoshka truncates, never pads)"
        )


__all__ = [
    "Embedder",
    "EmbeddingSpace",
    "EmbeddingSpaceRegistry",
]
