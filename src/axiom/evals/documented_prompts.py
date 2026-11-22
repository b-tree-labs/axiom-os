# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Documented chat prompts as tests: a guide says "or ask chat", and the claim is checked.

A guide step may end with a line that tells the reader what to type into chat instead::

    > **Or ask `neut chat`:** "am I connected to the site?" or "which site am I bound to?"

Each quoted prompt is a promise that chat can do the step. Left alone, the promise rots: a tool is
renamed, a classifier changes, a reply stops naming where it came from, and the guide still says
it works. This module turns the guide into the test.

* :func:`extract_prompts` reads the prompts out of a guide (the marker line, its quoted prompts and the
  step heading each sits under).
* A **catalog** (TOML, kept beside the guide) says for each prompt which tool it should reach and which
  source the answer should come from. :func:`coverage` fails both ways: a documented prompt with no
  catalog entry, and a catalog entry no guide documents.
* :func:`check_routing` is the cheap check that needs no model. The prompt must be classified as a data
  question or not, as the catalog says, the tool it should reach must be among the tools offered, and a
  model with a small context window, which is offered only the tools the question ranks highest, must
  still be offered it.
* :func:`evaluate` is the on-demand check against a real, bound node: it asks chat each prompt through
  ``chat --ask --json`` and scores whether the right tool ran, an answer came back, and the footer names
  the source the catalog expects. It uses :class:`axiom.evals.EvalHarness`, so it reports the way every
  other evaluation here does. A model is not available in CI, which is why this one is run by hand.

Domain-agnostic: it reads a guide and a catalog it is handed, and names no product.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.evals.harness import EvalCase, EvalHarness, EvalReport

#: A guide line that starts with this (after the blockquote marker) documents chat prompts.
MARKER = re.compile(r"^\s*>\s*\*\*Or ask\b[^*]*\*\*:?\s*(?P<rest>.*)$", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“]([^\"“”]+)[\"”]")
_HEADING = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")

SOURCES = ("data", "documents", "setup")


@dataclass(frozen=True)
class DocPrompt:
    prompt: str
    step: str
    line: int


@dataclass(frozen=True)
class CatalogEntry:
    """What a documented prompt must do. ``route`` is the tool(s) it should reach, any one of them."""

    prompt: str
    source: str
    route: tuple[str, ...] = ()
    #: A reply to this prompt must ask the person to confirm first (it changes something).
    confirms: bool = False
    note: str = ""


@dataclass
class Coverage:
    undocumented: list[str] = field(default_factory=list)  # in the guide, not in the catalog
    stale: list[str] = field(default_factory=list)  # in the catalog, in no guide

    @property
    def ok(self) -> bool:
        return not self.undocumented and not self.stale


def normalise(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip()).lower()


# ---- reading the guide and the catalog -----------------------------------------------------


def extract_prompts(markdown: str) -> list[DocPrompt]:
    """Every quoted prompt on an ``> **Or ask ...:**`` line, with the step heading above it."""
    out: list[DocPrompt] = []
    step = ""
    for number, raw in enumerate(markdown.splitlines(), 1):
        heading = _HEADING.match(raw)
        if heading:
            step = heading.group(2)
            continue
        marker = MARKER.match(raw)
        if not marker:
            continue
        found = _QUOTED.findall(marker.group("rest"))
        out.extend(DocPrompt(prompt=p.strip(), step=step, line=number) for p in found if p.strip())
    return out


def load_catalog(path: str | Path) -> list[CatalogEntry]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    entries: list[CatalogEntry] = []
    for row in data.get("prompt", []):
        source = str(row.get("source", ""))
        if source not in SOURCES:
            raise ValueError(
                f"{row.get('text')!r}: source must be one of {SOURCES}, got {source!r}"
            )
        route = row.get("route", [])
        route = (route,) if isinstance(route, str) else tuple(route)
        if source in ("data", "setup") and not route:
            raise ValueError(
                f"{row.get('text')!r}: a {source} prompt must name the tool it reaches"
            )
        entries.append(
            CatalogEntry(
                prompt=str(row["text"]),
                source=source,
                route=route,
                confirms=bool(row.get("confirms", False)),
                note=str(row.get("note", "")),
            )
        )
    return entries


def coverage(documented: Iterable[DocPrompt], catalog: Iterable[CatalogEntry]) -> Coverage:
    docs = {normalise(d.prompt): d.prompt for d in documented}
    cat = {normalise(c.prompt): c.prompt for c in catalog}
    return Coverage(
        undocumented=sorted(docs[k] for k in docs.keys() - cat.keys()),
        stale=sorted(cat[k] for k in cat.keys() - docs.keys()),
    )


# ---- routing, with no model ----------------------------------------------------------------


@dataclass
class RoutingVerdict:
    entry: CatalogEntry
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_routing(
    entry: CatalogEntry,
    tools: dict[str, Any],
    *,
    small_window_tools: int = 8,
) -> RoutingVerdict:
    """Whether chat would put the right tool in front of a model for this prompt. No model runs.

    1. The classifier agrees with the catalog about whether this is a data question.
    2. At least one tool the prompt should reach is offered at all.
    3. A prompt marked ``confirms`` reaches a write tool, so the approval gate asks.
    4. A model that can hold only ``small_window_tools`` tools is still offered one of them,
       because tools are chosen by the question when the window is small.
    """
    from axiom.extensions.builtins.chat import bound_tools as bt

    verdict = RoutingVerdict(entry)
    is_data = bt.is_data_question(entry.prompt)
    if is_data != (entry.source == "data"):
        verdict.problems.append(
            f"classified as {'a data question' if is_data else 'not a data question'}, "
            f"but the catalog says source={entry.source}"
        )
    if not entry.route:
        return verdict
    offered = [name for name in entry.route if name in tools]
    if not offered:
        verdict.problems.append(f"none of {list(entry.route)} is an offered tool")
        return verdict
    if entry.confirms:
        from axiom.infra.orchestrator.actions import ActionCategory

        if not any(getattr(tools[n], "category", None) == ActionCategory.WRITE for n in offered):
            verdict.problems.append(
                "the catalog says this prompt changes something and chat confirms first, but none of "
                f"{offered} is a write tool, so the approval gate would not ask"
            )
    chosen = _small_window_choice(entry, tools, small_window_tools, is_data)
    if not set(offered) & set(chosen):
        verdict.problems.append(
            f"a model that holds {small_window_tools} tools is not offered {offered}; it gets {sorted(chosen)}"
        )
    return verdict


def _small_window_choice(
    entry: CatalogEntry, tools: dict[str, Any], keep: int, is_data: bool
) -> dict[str, Any]:
    """The tools ``fit_to_context`` leaves in when a window holds about ``keep`` average-sized tools."""
    from axiom.extensions.builtins.chat import bound_tools as bt

    sizes = sorted(bt._tool_tokens(t) for t in tools.values())
    room = keep * sizes[len(sizes) // 2] + bt.REPLY_RESERVE_TOKENS + 8
    prefer = frozenset(n for n in tools if n.startswith("data__")) if is_data else frozenset()
    chosen, _note = bt.fit_to_context(
        tools, entry.prompt, window=room, prompt_chars=0, prefer=prefer
    )
    return chosen


def offered_tools(*, bound: bool = True, site: str = "example-site") -> dict[str, Any]:
    """The tools a chat offers: the built-in and setup tools, plus (when ``bound``) the node's data reads.

    Built from the chat's own tables, with no binding read and no network: the data reads are the ones
    a bound install projects from its own skills, and a binding is not needed to know which they are.
    """
    from axiom.extensions.builtins.chat import bound_tools as bt
    from axiom.extensions.builtins.chat import tools as chat_tools

    table = dict(chat_tools.BUILTIN_TOOLS)
    table.update(chat_tools._setup_tools())
    if bound:
        found = bt.BoundTools(site=site, face_url="https://node.example.org")
        bt._add_served_reads(found)
        table.update(found.tools)
    return table


# ---- the on-demand check against a bound node ----------------------------------------------

#: What ``chat --ask --json`` returns, as far as scoring needs it.
Ask = Callable[[str], dict[str, Any]]  # prompt -> the --json reply


def _called(output: dict[str, Any]) -> set[str]:
    return {
        str(v).removeprefix("node:").replace(".", "__") for v in output.get("verbs_called") or []
    }


def score_tool(output: dict[str, Any], expected: CatalogEntry, **_: Any) -> float:
    """1.0 when a tool the prompt should reach ran.

    * documents: no data tool ran (the answer comes from the site's documents).
    * data: one of the expected tools ran, or *a* data read ran here or on the node (``measured``).
      A bound node's model may use its own equivalent of the expected read, and it is the read that
      makes the answer a measurement, not the spelling of its name.
    * setup: one of the expected tools ran. A reply the install answered from its own facts lists the
      verb that holds them (``binding.status``).
    """
    called = _called(output)
    if expected.source == "documents":
        return 0.0 if any(name.startswith("data__") for name in called) else 1.0
    if called & set(expected.route):
        return 1.0
    if expected.source == "data" and output.get("measured") is True:
        return 1.0
    return 0.0


def score_answered(output: dict[str, Any], expected: CatalogEntry, **_: Any) -> float:
    if output.get("error") or output.get("answered") is False:
        return 0.0
    return 1.0 if str(output.get("answer") or "").strip() else 0.0


def score_source(output: dict[str, Any], expected: CatalogEntry, **_: Any) -> float:
    """1.0 when the footer names the source this prompt should come from, plainly (no ``?``).

    data: a data tool ran and the reply is not marked "not a measurement"; its footer names ``data:``.
    documents: the footer names ``documents:`` as a place, not "none configured".
    setup: the footer is present; the answer comes from the verb, not from a document or a reading.
    """
    footer = str(output.get("footer") or "")
    if "?" in footer or "data:" not in footer or "documents:" not in footer:
        return 0.0
    if expected.source == "data":
        return 0.0 if "not a measurement" in footer or output.get("measured") is False else 1.0
    if expected.source == "documents":
        return 0.0 if "documents: none configured" in footer else 1.0
    return 1.0


def evaluate(
    entries: list[CatalogEntry], ask: Ask, *, thresholds: dict[str, float] | None = None
) -> EvalReport:
    """Ask chat every documented prompt and score tool, answer and source. ``ask`` runs one prompt."""
    cases = [
        EvalCase(name=e.prompt, input=e.prompt, expected=e, metadata={"source": e.source})
        for e in entries
    ]

    def runner(prompt: str) -> dict[str, Any]:
        try:
            return ask(prompt)
        except Exception as exc:  # noqa: BLE001 - one prompt failing is a finding, not a crash
            return {"error": f"{type(exc).__name__}: {exc}"}

    scorers = {"tool": score_tool, "answered": score_answered, "source": score_source}
    return EvalHarness(thresholds=thresholds).run(runner=runner, cases=cases, scorers=scorers)


__all__ = [
    "CatalogEntry",
    "Coverage",
    "DocPrompt",
    "RoutingVerdict",
    "check_routing",
    "coverage",
    "evaluate",
    "extract_prompts",
    "load_catalog",
    "normalise",
    "offered_tools",
    "score_answered",
    "score_source",
    "score_tool",
]
