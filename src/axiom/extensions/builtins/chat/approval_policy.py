# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Approval decisions for surfaces with nobody at a keyboard.

The chat agent asks an operator before it runs a write. A surface that has no
operator, such as an HTTP serving worker or a scheduled run, cannot answer that
question and must not be asked it: a prompt written to a pipe either blocks
forever or returns a refusal nobody chose.

An ``ApprovalPolicy`` is how such a surface answers instead. The surface
declares itself by installing a policy; the platform never guesses from
``isatty`` or any other property of the process. ``decide`` reaches its answer
from the policy's own configuration alone, reads no input stream and writes no
output.

``NonInteractiveApprovalPolicy`` is the shipped policy and it refuses. A site
widens it one capability at a time, by name, through the ``allow`` allowlist,
so headless authority is always something a site granted on purpose. Both
outcomes carry a reason, so a refusal arrives as an explanation the caller can
act on rather than a bare choice letter.

The four approval outcomes and their letters live in ``permissions``; a policy
speaks that same vocabulary, and adds none of its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .permissions import ApprovalChoice

if TYPE_CHECKING:
    from axiom.infra.orchestrator.actions import Action


class ApprovalDecision(str):
    """One approval choice with the reason a policy reached it.

    A ``str`` subclass so it compares equal to the plain choice letter every
    approval surface already returns, while ``reason`` carries the explanation
    on to whoever asked. A caller that only switches on the letter keeps
    working; a caller that wants the why reads ``reason``.
    """

    reason: str

    def __new__(cls, choice: ApprovalChoice, reason: str = "") -> ApprovalDecision:
        decision = super().__new__(cls, choice)
        decision.reason = reason
        return decision

    @property
    def choice(self) -> ApprovalChoice:
        """The bare approval letter, without the reason."""
        return str(self)  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"ApprovalDecision({str.__repr__(self)}, reason={self.reason!r})"


class ApprovalPolicy(ABC):
    """Answers the approval gate for a surface that cannot ask a human."""

    @abstractmethod
    def decide(self, action: Action) -> ApprovalDecision:
        """Return the decision for ``action``.

        Implementations reach an answer from their own configuration. They read
        no input stream and write nothing to stdout or stderr.
        """
        ...


class NonInteractiveApprovalPolicy(ApprovalPolicy):
    """Refuse every action except the capabilities a site named in advance.

    Deny by default: a surface with no operator must never approve a write on
    its own. ``allow`` is the set of tool names a site has pre-approved for
    headless operation, fixed at construction so nothing can widen it later
    from inside a turn.

    An allowed action returns ``a`` and a refusal returns ``r``. Neither
    persists: the lasting choices (``A``, ``D``) are an operator's to make.
    """

    def __init__(self, allow: Iterable[str] = ()) -> None:
        self._allow = frozenset(allow)

    @property
    def allowed(self) -> frozenset[str]:
        """The capability names this policy approves, by name."""
        return self._allow

    def decide(self, action: Action) -> ApprovalDecision:
        name = action.name
        if name in self._allow:
            return ApprovalDecision(
                "a",
                f"{name!r} is on this surface's headless approval allowlist",
            )
        return ApprovalDecision(
            "r",
            f"{name!r} is not on this surface's headless approval allowlist "
            f"and there is no operator present to approve it",
        )


__all__ = [
    "ApprovalDecision",
    "ApprovalPolicy",
    "NonInteractiveApprovalPolicy",
]
