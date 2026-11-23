# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom-specific specialization of the generic AskPipeline.

The generic ``axiom.memory.ask.AskPipeline`` defines the canonical
shape of "user/agent asks a question": retrieve context, compose a
prompt via PromptComposer, invoke the LLM, log the interaction. This
module plugs the classroom extension into that shape via two seams:

- :class:`ClassroomRetriever` — adapts the per-classroom local index
  (``ClassroomLocalIndex``) to the generic ``Retriever`` protocol.
  Results map ``SearchHit`` → ``Citation`` so the pipeline's prompt
  composition + citation reporting work without classroom-specific
  glue.

- :class:`ClassroomAskHooks` — implements ``AskHooks`` to layer in
  classroom-specific specializations:

    * **Mode-aware system prompt overlays.** Tutor mode contributes
      the Socratic overlay; review mode contributes the summary
      overlay; default ``ask`` mode contributes nothing.

    * **Closed-book modes short-circuit the LLM.** Quiz and reflect
      modes have ``llm_constraint == "none"`` — the hook returns an
      empty :class:`AskResult` from ``pre_llm`` so the pipeline never
      calls the LLM.

    * **Closed-book modes hide retrieved citations.** Quiz mode also
      drops citations from the result (filter_citations).

The classroom CLI's ``_cmd_ask`` builds this hook + retriever pair,
hands them to an ``AskPipeline``, and renders the result. No
prompt-string concatenation lives in the CLI anymore.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from axiom.memory.ask import (
    AskHooks,
    AskRequest,
    AskResult,
    Citation,
)

# ---------------------------------------------------------------------------
# ClassroomRetriever — adapts ClassroomLocalIndex to the Retriever protocol
# ---------------------------------------------------------------------------


@dataclass
class ClassroomRetriever:
    """Wrap ``ClassroomLocalIndex`` so the generic pipeline can call it.

    The classroom index is opened lazily on first ``retrieve()`` and
    closed by the caller via :meth:`close` once the pipeline finishes
    its run. Each retrieve call surfaces top-k matches as generic
    ``Citation`` instances; ``source_id`` is the classroom file_id so
    provenance round-trips cleanly.
    """

    classroom_dir: Path

    _index: object | None = None  # ClassroomLocalIndex; lazy

    def _ensure_open(self):
        # Lazy import keeps the pipeline-side hook layer free of
        # classroom-internal modules at import time.
        from .classroom_local_index import ClassroomLocalIndex

        if self._index is None:
            self._index = ClassroomLocalIndex(base_dir=self.classroom_dir)
            self._index.open()
        return self._index

    def retrieve(self, query: str, *, k: int) -> list[Citation]:
        index = self._ensure_open()
        hits = index.search(query, k=k)
        return [
            Citation(
                title=h.title,
                text=h.text,
                source_id=h.file_id,
                score=h.score,
            )
            for h in hits
        ]

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None


# ---------------------------------------------------------------------------
# ClassroomAskHooks — mode-aware specialization
# ---------------------------------------------------------------------------


@dataclass
class ClassroomAskHooks(AskHooks):
    """Layer classroom-specific behavior onto the generic pipeline.

    Reads the resolved learning mode from ``request.mode`` and:

    - ``contribute_layers`` adds the mode's system prompt overlay (if
      any) to the ``domain_context`` layer of the composer.

    - ``filter_citations`` strips citations for closed-book modes so
      the result the pipeline returns matches the mode's contract
      (quiz mode returns nothing, even when retrieval would have
      matched something).

    - ``pre_llm`` short-circuits when the mode's ``llm_constraint``
      is ``"none"`` — quiz + reflect modes never invoke the LLM.

    - ``post_llm`` is a no-op; mode-shaping is handled via the system
      prompt overlay rather than response post-processing, so the
      raw LLM answer is what the pipeline returns. Hook is provided
      so future tutor-shape-checking logic has an obvious place.
    """

    classroom_id: str

    def _resolve_mode(self, request: AskRequest):
        """Return the LearningMode for ``request.mode`` or None when
        the name is unknown / missing. Lazy import keeps the import
        graph shallow."""
        from .learning_modes import get_mode

        if not request.mode:
            return None
        try:
            return get_mode(request.mode)
        except KeyError:
            return None

    # --- Hook 1: prompt layer contributions --------------------------------

    def contribute_layers(self, request: AskRequest, composer) -> None:
        mode = self._resolve_mode(request)
        if mode is None:
            return
        if not mode.system_prompt_overlay:
            return
        composer.add(
            "domain_context",
            name=f"classroom_mode_{mode.name}",
            content=mode.system_prompt_overlay,
            source=f"classroom:{self.classroom_id}",
            required=True,
        )

    # --- Hook 2: filter retrieved citations --------------------------------

    def filter_citations(
        self, request: AskRequest, citations: list[Citation],
    ) -> list[Citation]:
        mode = self._resolve_mode(request)
        if mode is None:
            return citations
        if mode.retrieval_policy == "none":
            # Quiz mode is closed-book — citations are hidden even if
            # the retriever happened to fire (defense in depth; the
            # CLI also gates retrieval upstream).
            return []
        return citations

    # --- Hook 3: pre-LLM short-circuit -------------------------------------

    def pre_llm(
        self, request: AskRequest, composer, citations: list[Citation],
    ) -> AskResult | None:
        mode = self._resolve_mode(request)
        if mode is None:
            return None
        if mode.llm_constraint != "none":
            return None
        # Closed-book mode (quiz, reflect) — return an empty result so
        # the pipeline skips the LLM call and the CLI renders the
        # mode-appropriate "you write first" surface.
        return AskResult(
            answer="",
            citations=citations,
            fragments_used=[],
            mode_used=mode.name,
        )

    # --- Hook 4: post-LLM transform ----------------------------------------

    def post_llm(
        self,
        request: AskRequest,
        raw_response: str | None,
        citations: list[Citation],
    ) -> str | None:
        # No transformation — overlays do the shaping at prompt time.
        return None


__all__ = [
    "ClassroomAskHooks",
    "ClassroomRetriever",
]
