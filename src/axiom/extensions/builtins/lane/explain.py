# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reasoning over what `doctor` found — locally, and never as a prerequisite.

`doctor` detects. This interprets. The split is the platform's existing
pattern (a deterministic floor under LLM judgement), and the distinction that
matters is **not** "no model is purer". It is that detection has to work in CI
and on a machine with nothing loaded, so the floor may not DEPEND on a model.
Everything above the floor is better with one.

What a model adds here is not decoration. A real case, on the day this was
written: `doctor` can report

    editable install points at .../appkit-wt-cause/..., which does not exist

which is accurate and still leaves a person to work out that a landed,
merged, correctly-removed worktree was what a running server had been
importing from for two days. A model holding that finding next to
`git worktree list` and the venv's own records says so in a sentence. The
deterministic layer cannot, because the conclusion is an inference across
three sources rather than a fact in any one of them.

## Local by default, and honest when absent

Interpretation runs against a LOCAL provider. A developer's half-broken
environment is not something to post to a third party, and the latency of a
round trip is wrong for a command people run between other commands.

When no local model answers, this returns the deterministic findings with a
note saying why there is no commentary. It never fails the command, never
blocks, and never silently substitutes a remote model — a tool that quietly
sends your machine's state somewhere else the one time Ollama is down has
broken a promise that mattered more than the feature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .doctor import BROKEN, Finding

#: Why there is no commentary. The router already names these failures; using
#: its vocabulary means an operator sees one story, not two.
UNREACHABLE = "ollama_unreachable"
NOT_LOADED = "model_not_loaded"
NOT_REQUESTED = "not_requested"

#: The house local endpoint. The same daemon the router's sensitivity
#: classifier and the chat SLM advisor use — `lane` must not become a second
#: way to reach it with different settings.
OLLAMA_BASE_SETTING = "routing.ollama_base"
DEFAULT_BASE = "http://localhost:11434"

#: TWO local roles, because they are different jobs and one model is wrong
#: for one of them. Both defaults are Apache-2.0 and both are Qwen 2.5 — one
#: family, so there is one set of prompt quirks and one licence to track.
#:
#: The choice is measured, not assumed. Asked to explain a real finding (an
#: editable install pointing at a removed worktree), on 2026-09-28:
#:
#:   gemma2:2b     4.3s   missed the cause, invented a venv command
#:   phi3.5:3.8b   6.0s   missed the cause, blamed a missing .venv
#:   qwen2.5:7b   13.0s   named the finding AND inferred the deletion
#:
#: Only the 7B made the join across findings, worktree list and lane state.
#: The 2B models were not a cheaper version of the right answer; they were a
#: fast wrong one, which is worse, because fluent and wrong reads as certain.
#: 13 seconds is acceptable for a command run when something is already broken.
#:
#: NOTE for anyone tempted by a smaller default: all three invented the final
#: command. That is a property of the tier, not of a model, and it is why the
#: prompt now offers fixes to CHOOSE from rather than asking for one.
#:
#: QUICK — classification, a one-line next step, terminal affordances. Sized
#: for something a person should not perceive. This is what the router's
#: classifier and the chat advisor already use, and it stays as it is.
#:
#: REASONING — an inference drawn across sources that no single source
#: states. Explaining that a dangling editable install means a removed
#: worktree requires holding three facts together; a 1B model will produce a
#: fluent sentence about the one fact it was handed and miss the join.
#:
#: The settings keys are separate so a deployment can move one without the
#: other, and so "which model was that" has an answer per role.
QUICK_MODEL_SETTING = "routing.ollama_model"
DEFAULT_QUICK_MODEL = "qwen2.5:1.5b"

REASONING_MODEL_SETTING = "routing.ollama_reasoning_model"
DEFAULT_REASONING_MODEL = "qwen2.5:7b"

#: `lane explain` reasons. It does not classify.
OLLAMA_MODEL_SETTING = REASONING_MODEL_SETTING
DEFAULT_MODEL = DEFAULT_REASONING_MODEL

#: Longer than the chat advisor's 120 characters, and deliberately so: that
#: caller wants one short next step after a turn, this one is explaining an
#: inference drawn across three sources. Same discipline, different budget.
MAX_CHARS = 600

SYSTEM = """You are reading the output of a developer-environment checker on one machine.

Several checkouts of the same project run side by side, each with its own
database, ports and editable installs. Findings describe drift between what
was declared and what the machine is doing.

Answer three things, briefly, in plain prose:
  1. Which finding explains the problem the developer is most likely hitting now.
  2. What most plausibly caused it, if the findings together imply a cause that
     none of them states alone.
  3. Which of the offered fixes to run. Quote it exactly as given. If none
     of them fits, say that no offered fix applies — do NOT compose a
     command of your own.

When the caller states a goal, answer for THAT goal: lead with whatever
stands between them and it, and leave the rest unmentioned. A caller trying
to start one app does not need findings about another.

Do not restate findings that need no explanation. Never invent a path, port,
package name or command. Every command you name must appear verbatim in the
input; small models reliably confabulate plausible-looking commands here, and
a wrong command runs. If the findings do
not support a conclusion, say that instead of producing one."""


@dataclass(frozen=True)
class Commentary:
    """What the model said, or why it said nothing."""

    text: str = ""
    unavailable: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.unavailable


def as_prompt(
    findings: list[Finding], *, context: dict[str, str] | None = None, caller_goal: str = ""
) -> str:
    """The findings, the surrounding facts, and what the caller wanted.

    `context` carries what the checker knows but a finding does not — the
    worktrees that exist, which lanes are claimed. A model asked to explain a
    dangling path with no view of the checkouts will guess at one.

    `caller_goal` is the caller's own sentence about what they are trying to
    do (ADR-139). It is what turns "explain these findings" into "explain
    these findings to somebody trying to start the chat app", which is the
    difference between a summary and an answer. It is untrusted prose: it
    shapes emphasis and nothing else.
    """
    lines = []
    if caller_goal.strip():
        lines += [f"THE CALLER IS TRYING TO: {caller_goal.strip()}", ""]
    lines += ["FINDINGS:"]
    # The fix travels WITH the finding so the model picks from real commands
    # instead of writing one. Measured: every local model tried tested — 2B
    # through 7B — invented a final command when not given any.
    lines += [
        f"- [{f.level}] {f.subject}: {f.detail}" + (f"\n    OFFERED FIX: {f.fix}" if f.fix else "")
        for f in findings
    ] or ["- none"]
    for key, value in sorted((context or {}).items()):
        lines += [f"\n{key.upper()}:", value]
    return "\n".join(lines)


def explain(
    findings: list[Finding],
    *,
    ask: Callable[[str, str], str] | None,
    context: dict[str, str] | None = None,
    caller_goal: str = "",
) -> Commentary:
    """Interpret `findings` with a local model, or say why not.

    `ask(system, user) -> str` is injected so the caller owns provider choice
    and so this is testable without a model running. Passing None means the
    caller did not want commentary, which is different from a model being
    unavailable and is reported differently.
    """
    if ask is None:
        return Commentary(unavailable=NOT_REQUESTED)
    if not findings:
        return Commentary(text="Nothing to explain: no findings.")

    try:
        said = (
            ask(SYSTEM, as_prompt(findings, context=context, caller_goal=caller_goal)) or ""
        ).strip()
    except FileNotFoundError:
        return Commentary(unavailable=UNREACHABLE)
    except Exception as exc:  # a local daemon has many ways to be absent
        reason = NOT_LOADED if "model" in str(exc).lower() else UNREACHABLE
        return Commentary(unavailable=reason)

    return Commentary(text=said) if said else Commentary(unavailable=NOT_LOADED)


def render(findings: list[Finding], commentary: Commentary) -> str:
    """Findings first, always. Commentary is an addition, never a substitute."""
    out = [f.render() for f in findings] or ["no findings"]
    if commentary.ok:
        out += ["", "— reading, from a local model —", commentary.text]
    elif commentary.unavailable and commentary.unavailable != NOT_REQUESTED:
        why = {
            UNREACHABLE: "no local model answered (is ollama running?)",
            NOT_LOADED: "the local model is not loaded (ollama pull ...)",
        }.get(commentary.unavailable, commentary.unavailable)
        out += ["", f"(no commentary: {why} — the findings above stand on their own)"]
    if any(f.level == BROKEN for f in findings):
        out += ["", "at least one finding is BROKEN: something is wrong now, not later."]
    return "\n".join(out)


__all__ = [
    "NOT_LOADED",
    "NOT_REQUESTED",
    "SYSTEM",
    "UNREACHABLE",
    "Commentary",
    "as_prompt",
    "explain",
    "render",
]
