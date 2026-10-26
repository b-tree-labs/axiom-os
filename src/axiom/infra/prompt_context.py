# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a system-prompt contributor is allowed to know about a request.

An installed extension contributes fragments to the composed system prompt.
Until this object existed the loader called every contributor with no
arguments, so a fragment could only say things that were true of the process:
nothing could vary with who was asking, which conversation this was, or what
mode the surface was in.

A ``PromptContext`` is what a contributor is handed instead. Two rules shape
it, and both come from the same place.

**It is a declared field set, not a state object.** The obvious shortcut is to
pass the request's own state object and let each contributor take what it
wants. That shortcut has already cost this program once: a payload assembled
by handing over a whole internal object put credentials into a log, because
nobody could enumerate what was in it. A contributor is arbitrary installed
extension code, so what reaches it is a short list somebody chose on purpose
and a reviewer can read in one screen. Nothing credential-shaped is ever a
field here: no token, no key, no signing material, no store or connection
handle that stands in for one. Widening the set is a reviewed decision, and
``tests/infra/test_prompt_context.py`` pins the set so that widening it
silently is not possible.

**It is read-only.** A contributor runs in the middle of composing somebody's
prompt. Assigning to a field raises, so a contributor cannot change the
request it is contributing to, and cannot leave anything behind for the next
contributor in the same composition to pick up. Every field is a string, so
there is no mutable container to edit in place behind the frozen surface.
This is a guard against accident and a boundary a reviewer can check, not a
sandbox: in-process code that is determined to misbehave has other routes.

On the fields, and on what is deliberately missing:

``principal_id``
    Who is asking, as a principal identifier such as ``@axi:someone``. This
    is the field the whole object exists for: a fragment that says who the
    agent is acting for cannot be written without it. An identifier is not a
    credential; it names a principal, it does not authenticate as one.

``session_id``
    Which conversation this is, as an opaque identifier. It lets a
    contributor key its own per-conversation state and lets a fragment be
    correlated with the observability record for the same turn. It confers
    nothing: a contributor already runs in-process and could reach the
    session store directly, so the identifier grants no reach it lacked.

``interaction_mode``
    ``ask`` | ``plan`` | ``agent``: how much of the loop this conversation
    runs. A fragment about how to use tools is wrong in a mode where no
    tools will be offered, and this is the only way for a contributor to
    know which mode it is in.

``workspace_context``
    The workspace brief for this conversation. Contributors specialize on
    what the request is about, and this brief is already being composed into
    the same prompt, so a contributor reading it learns nothing the prompt
    pipeline was not about to say anyway.

Deliberately excluded, so a later reader knows these were decided rather than
forgotten: approval state (the permission map, the allowlist, the pending
approval gate), because a fragment tailored to what is already approved is a
way to steer the model toward an approved tool; conversation content
(messages, the current query), because a role fragment does not need the
user's words and shipping them to every installed extension is a disclosure
nobody asked for; turn control (the cancellation event, the stream consumer,
the budgets), because those are handles that act rather than facts that
inform; the routing tier hint, which belongs to a routing decision that has
not been made yet; and the accountable human behind the acting principal,
which no contributor needs today and which can be added when one does.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PromptContext"]


@dataclass(frozen=True)
class PromptContext:
    """The request a system-prompt contributor is contributing to.

    Read-only: assigning to any field raises ``FrozenInstanceError``. Every
    field is a string, and every field defaults to the empty string, so a
    caller with nothing to say about a request can build one and a
    contributor never has to guard against ``None``.
    """

    #: The acting principal, e.g. ``@axi:someone``. Empty when unbound.
    principal_id: str = ""

    #: Opaque identifier for this conversation. Empty when there is none.
    session_id: str = ""

    #: ``ask`` | ``plan`` | ``agent``: how much of the loop runs.
    interaction_mode: str = ""

    #: The workspace brief composed into this conversation's prompt.
    workspace_context: str = ""
