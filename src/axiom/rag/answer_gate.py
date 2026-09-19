# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Answer provenance: does this answer's evidence exist?

The verification this builds on is not new. :func:`postprocess_citations`
already extracts every ``[C<n>]`` marker, checks it against the chunks the
retriever fed the model, and returns an envelope naming the ones that resolve,
the ones that do not, and the ones nobody used. It is deterministic and it is
well tested.

What was missing is a *decision*. That verification had exactly one production
caller, an audit-logging method that passed its result to a log row, swallowed
every exception, and ran after the answer had already been appended to the
session. So the platform could establish that an answer cited a source which
does not exist, write that fact to a row, and return the answer anyway. This
module is the standing that a caller can act on.

It is deliberately only the finding. What a surface *does* about an ungrounded
answer differs by surface and is not this module's to assume: a terminal
session exploring a corpus and a served answer that will be acted on want
different things from the same finding.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from axiom.rag.citation import CitationEnvelope, postprocess_citations

__all__ = [
    "ProvenanceFinding",
    "ProvenanceStanding",
    "check_answer_provenance",
]


class ProvenanceStanding(str, Enum):
    """What the answer's own citations turned out to be worth.

    ``GROUNDED`` — the answer cites at least one source and every marker
    resolves to a chunk the retriever actually supplied.

    ``UNGROUNDED`` — at least one marker names a source that was not
    retrieved. The answer is claiming evidence that does not exist, which is
    worse than claiming none: a reader who spot-checks one citation and finds
    it sound has no reason to doubt the others.

    ``UNCITED`` — the answer carries no citation markers at all. Not a
    failure on its own; a greeting is uncited and fine. It becomes one when a
    surface expects an answer to be grounded, which is why this is its own
    standing rather than being folded into either of the others.
    """

    GROUNDED = "grounded"
    UNGROUNDED = "ungrounded"
    UNCITED = "uncited"


@dataclass(frozen=True)
class ProvenanceFinding:
    """What the check found, and enough to explain it to a person.

    ``__bool__`` raises. Three standings do not collapse into two, and every
    truthiness convention gets one of them wrong: ``UNCITED`` is not a pass
    and it is not a failure, and ``UNGROUNDED`` carries a reason a caller owes
    the reader rather than being an empty nothing.
    """

    standing: ProvenanceStanding
    cited: tuple[str, ...]
    unresolved: tuple[str, ...]
    unused: tuple[str, ...]
    retrieved_count: int
    reason: str

    def __bool__(self) -> bool:
        raise TypeError(
            "A ProvenanceFinding has three standings and cannot be used as a "
            f"boolean. This one is {self.standing.value}. Compare "
            "`finding.standing is ProvenanceStanding.GROUNDED` explicitly, and "
            "decide what an uncited answer should do rather than letting it "
            "pass as a grounded one."
        )


def check_answer_provenance(
    answer: str,
    retrieved: Iterable[object] | None,
) -> ProvenanceFinding:
    """Decide whether ``answer``'s citations name sources that were retrieved.

    ``retrieved`` is the chunk sequence the retriever fed the model this turn.
    An empty one does **not** short-circuit the check, and that is the point:
    an answer carrying ``[C1]`` when nothing at all was retrieved has invented
    its evidence outright, and skipping the check because there is nothing to
    compare against is how that case stays invisible.
    """
    chunks: Sequence[object] = list(retrieved or [])
    envelope: CitationEnvelope = postprocess_citations(answer, chunks)

    cited = tuple(reference.citation_key for reference in envelope.cited)
    unresolved = tuple(envelope.unresolved)
    unused = tuple(envelope.unused)

    if unresolved:
        if not chunks:
            reason = (
                f"the answer cites {', '.join(unresolved)} but nothing was "
                "retrieved this turn, so every citation in it names a source "
                "that does not exist"
            )
        else:
            reason = (
                f"the answer cites {', '.join(unresolved)}, which "
                f"{'was' if len(unresolved) == 1 else 'were'} not among the "
                f"{len(chunks)} chunk(s) the retriever supplied"
            )
        return ProvenanceFinding(
            standing=ProvenanceStanding.UNGROUNDED,
            cited=cited,
            unresolved=unresolved,
            unused=unused,
            retrieved_count=len(chunks),
            reason=reason,
        )

    if not cited:
        return ProvenanceFinding(
            standing=ProvenanceStanding.UNCITED,
            cited=(),
            unresolved=(),
            unused=unused,
            retrieved_count=len(chunks),
            reason=(
                "the answer cites nothing"
                + (
                    f"; {len(chunks)} chunk(s) were retrieved and none was used"
                    if chunks
                    else " and nothing was retrieved"
                )
            ),
        )

    return ProvenanceFinding(
        standing=ProvenanceStanding.GROUNDED,
        cited=cited,
        unresolved=(),
        unused=unused,
        retrieved_count=len(chunks),
        reason=(f"every citation resolves: {', '.join(cited)} of {len(chunks)} retrieved chunk(s)"),
    )
