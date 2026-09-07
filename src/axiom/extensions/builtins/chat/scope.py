# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""What belongs to one conversation, held apart from the process that runs it.

A chat agent at a terminal serves one person, so keeping the conversation on
the agent instance costs nothing. A serving worker holds one agent and answers
many people's requests with it, and there the same arrangement is a leak with
consequences: person A answers "always allow" for a write tool, the answer
lands on the agent, and person B's next request runs that tool with no
approval at all. Conversation history is shared the same way, and so is a
persisted deny, the interaction mode, and any image queued for the next turn.

A ``ChatScope`` is where those values live instead. One criterion decides what
belongs here:

    **would one person's value be wrong to use for another person's request?**

If yes it is request state and it goes in the scope. If no it stays on the
agent: the gateway, the event bus, the tool registry, the render provider and
the approval policy a surface installed for all of its requests. Note what
that criterion does with a store handle. A retrieval store or a memory store
is shared by every request and stays on the agent, because who is allowed to
see what is decided from the principal on the scoped session, not from
holding the handle.

The agent keeps one default scope, built exactly as it always built its own
attributes, and every public attribute still reads and writes through it. A
terminal passes no scope and behaves as it always has. A serving worker builds
one scope per request and passes it to ``turn``, and nothing that request does
can reach the default or any other request's scope.

A scope built with no arguments is cheap and touches no disk: its permission
map starts empty and in memory, so a request begins with no approval anybody
else granted. The agent's default scope is the one that loads the operator's
persisted allow and deny choices, because that is the conversation an operator
is sitting in front of.

The same reasoning puts a turn's budgets here. How many tool rounds a turn may
run, and how long it may take, are not properties of the process: a serving
worker answering an HTTP request wants a tighter bound than an operator who is
watching the output and can interrupt it. Both default to what the terminal has
always done, so a caller that sets neither sees no change.

And it puts the stream consumer here for the same reason. Who is watching this
stream arrive is a property of the request, not of the process: one request is
a browser waiting for server-sent events, the next is a voice, the next is a
terminal that wants none of it.

The same criterion decides the operator-local prompt sources. Part of the
system prompt is read off the machine the process runs on rather than off the
request: the repository's project file, the personal context file beside it,
and the operator's prompt library. At a terminal the operator and the person
asking are one person and those notes belong in the prompt. On a serving
worker they are two people, and one person's private notes going into another
person's prompt is exactly what the criterion above rejects. So the surface
declares it, and it declares it here rather than on the agent: a surface
stamps one answer on every scope it mints, and because the value rides the
request, one agent can still answer two requests differently. Nothing infers
it from the environment, from ``isatty``, or from whether a file happens to
exist.

The retrieval corpus is the same question asked twice. A turn indexes its own
transcript into the node's internal corpus when it ends, and retrieves from
that corpus into the system prompt when the next one begins. At a terminal
that round trip is the feature: the operator gets last week's conversation
back because the operator on both ends is the same person. On a surface
answering other people it is a disclosure, in both directions at once, so both
directions are declared here: ``retrieval_corpora`` says what this
conversation may read, and ``index_transcript`` says whether it is written
down where the next conversation can read it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.session import Session

from .permissions import ToolPermissions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from axiom.infra.gateway import StreamChunk

#: Tool rounds one turn may run when nobody asks for a different number. The
#: name predates the scope and is kept as the default's single source, so an
#: existing caller that imports it still reads the value the loop uses.
MAX_TOOL_ROUNDS = 10

#: The corpus a node indexes its own material into: this machine's chat
#: transcripts, its processed signals, its local documents. It is the operator's
#: corpus in the same sense the project file and the prompt library are the
#: operator's: whatever is in it got there from this machine.
#:
#: Named here rather than imported from ``axiom.rag.store`` because importing
#: that module pulls a database driver into every chat import. The equality is
#: asserted in the tests instead of trusted.
OPERATOR_LOCAL_CORPUS = "rag-internal"

#: What a conversation that is not the operator's own may draw from: every
#: corpus except that one. Shared and community material is published to be
#: read by whoever asks; the internal corpus is not.
SHARED_CORPORA: tuple[str, ...] = ("rag-community", "rag-org")


@dataclass
class ChatScope:
    """One conversation's state: everything a request owns and nothing more."""

    #: Message history for this conversation.
    session: Session = field(default_factory=Session)

    #: Per-tool allow/ask/deny choices. In memory by default so an answer
    #: given in one request is never read back in another, and never written
    #: to the operator's file. The agent's default scope overrides this with
    #: the persisted map.
    permissions: ToolPermissions = field(default_factory=ToolPermissions)

    #: Actions submitted this turn and their approval status. The gate holds
    #: pending actions mid-turn, which makes it request state.
    gate: ApprovalGate = field(default_factory=ApprovalGate)

    #: Tools this conversation approved for the rest of its life.
    allowlist: set[str] = field(default_factory=set)

    #: Routing tier hint (``auto`` unless a surface asked for another).
    session_mode: str = "auto"

    #: ``ask`` | ``plan`` | ``agent``: how much of the loop this
    #: conversation runs. See ``ChatAgent.set_interaction_mode``.
    interaction_mode: str = "agent"

    #: Images queued for the next turn's API call.
    pending_images: list[Any] = field(default_factory=list)

    #: The workspace brief injected into this conversation's system prompt.
    workspace_context: str = ""

    #: Whether this conversation's system prompt may include the sources read
    #: from the operator's own machine: the repository's project file, the
    #: personal context file under that root's dotted config directory, and
    #: the operator's prompt library. ``True`` is what a terminal has always
    #: had, where the operator is the person asking. A surface answering other
    #: people declares ``False``; ``HeadlessChat`` does so for every scope it
    #: mints, so a serving surface is safe without its author knowing these
    #: sources exist.
    include_operator_local_prompts: bool = True

    #: The corpora this conversation's retrieval may draw candidates from.
    #: ``None`` means every corpus, which is what a terminal has always had and
    #: what an administrative caller wants. A surface answering other people
    #: names the ones it may read, and ``HeadlessChat`` names
    #: :data:`SHARED_CORPORA` on every scope it mints, so the operator's own
    #: corpus is out of reach without the surface's author knowing it exists.
    #:
    #: This is a corpus filter and not an access context on purpose. The
    #: retriever's access filter reads a chunk's tier, classification and site
    #: through optional lookup callables; with none of them wired every chunk
    #: reads as public, unclassified and unattributed, which permits
    #: everything. An access context alone would look like a control and be
    #: none. The corpus filter runs in the store's own query, so it also keeps
    #: one conversation's material out of the candidate window rather than
    #: dropping it after fusion.
    retrieval_corpora: list[str] | None = None

    #: Whether this conversation's transcript is indexed into the node's
    #: internal corpus when a turn ends. ``True`` is what a terminal has always
    #: had, where the transcript being retrievable next week is the point. A
    #: surface answering other people declares ``False``: one person's
    #: conversation in a corpus everybody reads is the write half of the same
    #: disclosure ``retrieval_corpora`` closes on the read side.
    index_transcript: bool = True

    #: Names of the tools the most recent turn ran, in order.
    last_turn_tools: list[str] = field(default_factory=list)

    #: Chunks retrieved for the in-flight turn, checked against the model's
    #: citations by the retrieval audit.
    last_retrieved: list[Any] = field(default_factory=list)

    #: What the most recent answer's own citations turned out to be worth.
    #: Set on every completed turn, including turns where nothing was
    #: retrieved — an answer that cites a source when the retriever returned
    #: nothing has invented its evidence, and that is the case worth seeing.
    #: A ``ProvenanceFinding``; ``None`` before the first answer.
    last_provenance: Any | None = None

    #: The PromptComposer from the most recent system prompt, read by the
    #: prompt-composition observability record.
    last_composer: Any | None = None

    #: The query and start time of the in-flight turn, for that same audit
    #: and for the turn deadline below.
    turn_query: str = ""
    turn_start: float = 0.0

    #: How many tool rounds one turn of this conversation may run.
    max_tool_rounds: int = MAX_TOOL_ROUNDS

    #: Wall-clock seconds one turn of this conversation may take, measured
    #: from ``turn_start``. ``None`` means no time bound, which is what a
    #: terminal has always had. A serving worker sets one.
    turn_deadline: float | None = None

    #: Cancellation for the in-flight turn. Cancelling one conversation must
    #: not stop another, so the event is per scope. A turn that runs out of
    #: time never touches this: an interruption and a timeout are different
    #: events and a reader has to be able to tell which happened.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    #: Called with every streaming chunk the moment it arrives, or ``None``
    #: for the terminal's behaviour, which is what a terminal has always had.
    #:
    #: The render provider is a *pull* contract: it is handed an iterator, it
    #: owns the loop, and it returns a string when the stream is finished.
    #: That fits a terminal and fits nothing else, because a caller wanting to
    #: write one server-sent-event frame per delta never gets control back
    #: between chunks. This is the push half. A serving worker sets one per
    #: request; both contracts run together and neither displaces the other.
    #:
    #: Contract:
    #:
    #: * called once per chunk, in order, in whichever branch the streaming
    #:   helper takes, and before that helper's cancellation and deadline
    #:   checks, so a chunk that was produced is delivered rather than lost
    #:   to a race with the clock;
    #: * raising does not end the turn. The agent absorbs the failure, logs
    #:   it once per stream rather than once per chunk, and carries on, since
    #:   a consumer that went away is not a reason to abandon generation;
    #: * if the callable also has a ``close`` method, the agent calls it
    #:   exactly once when the stream ends, however it ends, including on a
    #:   cancellation and on a turn that ran out of time. A consumer holding
    #:   buffered state flushes there. A plain function needs none.
    #:
    #: See ``utterances.utterance_aggregator`` for a consumer built on this
    #: that reports whole clauses rather than tokens.
    on_chunk: Callable[[StreamChunk], None] | None = None

    def __post_init__(self) -> None:
        self.validate_budgets()

    def validate_budgets(self) -> None:
        """Reject a budget no turn could run under.

        Zero rounds means a turn that never calls the model; zero or negative
        seconds means one that is over before it starts. Both are caller
        mistakes worth a message rather than a silent empty answer. Checked
        at construction and again when a turn starts, because a worker that
        recycles a scope can set a field after building it.
        """
        if self.max_tool_rounds < 1:
            raise ValueError(f"max_tool_rounds must be at least 1, got {self.max_tool_rounds!r}")
        if self.turn_deadline is not None and self.turn_deadline <= 0:
            raise ValueError(
                "turn_deadline must be a positive number of seconds, or None for "
                f"no time bound, got {self.turn_deadline!r}"
            )

    def time_remaining(self, now: float) -> float | None:
        """Seconds left before this turn's deadline, or ``None`` when unbounded.

        ``now`` is passed in rather than read, so the caller owns the clock and
        this stays a pure function of the scope.
        """
        if self.turn_deadline is None:
            return None
        return self.turn_deadline - (now - self.turn_start)


__all__ = [
    "MAX_TOOL_ROUNDS",
    "OPERATOR_LOCAL_CORPUS",
    "SHARED_CORPORA",
    "ChatScope",
]
