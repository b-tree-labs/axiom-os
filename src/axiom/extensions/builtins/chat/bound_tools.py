# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A laptop bound to a site node answers data questions with the node's data tools.

A bound install already reads its data from the node (``read_target``) and answers with the node's
model (``bound_model``). Chat's tool list did not follow: the data skills were offered only where a
local database answered, and a promoted kit verb was not a tool at all, so a question about one got
"I don't have that tool" and a question about a current value got a number from the model's memory.

When bound, chat offers:

* the node's served data reads (:data:`read_target.FOLLOWS_THE_BINDING` and the everyday reads
  composed from them, ``latest``, ``channels`` and ``show``) and the kit reads
  (:data:`read_target.KIT_READS`), each projected from its local skill, which follows the binding;
* each promoted kit verb of the bound site as a tool of its own name, discovered from the node once,
  when chat starts, through the binding.

Every call goes to the node with the binding's key and is held there to the key's site; nothing here
opens a database or names a site to read.

A data question (:func:`is_data_question`) goes through these tools. One that names a promoted verb
runs it before the model answers. If the turn ends with no data tool called, here or on the node
(``x_verbs_called``), the reply says so plainly: an answer without a verb call is not a measurement.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Where a bound tool came from (``ToolDef.source``).
TOOL_SOURCE_BOUND = "bound-node"

#: The everyday reads, composed from served reads, so they work against any node that serves those.
EVERYDAY_READS: tuple[str, ...] = ("data.latest", "data.channels", "data.show")

#: The line a reply leads with when a data question was answered without any data tool.
NOT_A_MEASUREMENT = "not a measurement: no data tool was called"

#: The rule the model is given on a bound install.
BOUND_RULE = (
    "\n\n--- DATA ON {site} ---\n"
    "This install is bound to the site {site}. Its measurements are read only by calling a data "
    "tool: {tools}. For any question about a value, a reading, a trend or a recent state, call "
    "one of these tools first and answer from what it returned, quoting its rows and their times. "
    "Never answer such a question from memory or from documents: a number that did not come from "
    "a tool call is not a measurement. If no tool can answer it, say so."
)

_DATA_WORDS = re.compile(
    r"\b(latest|newest|now|right now|current(?:ly)?|at the moment|today|yesterday|tonight|"
    r"last (?:\d+ )?(?:minute|hour|day|week|month|night|reading|value)s?|past (?:\d+ )?"
    r"(?:minute|hour|day|week)s?|since|reading|readings|value|values|measure(?:d|ment|ments)?|"
    r"level|average|mean|max(?:imum)?|min(?:imum)?|peak|trend|series|over time|by (?:the )?hour|"
    r"by (?:the )?day|hourly|daily|how (?:much|many|high|low|hot|cold|fast)|what (?:is|was|are|were) "
    r"the|status|state)\b",
    re.IGNORECASE,
)


@dataclass
class BoundTools:
    """The tools a bound chat offers, and what each one runs."""

    site: str
    face_url: str
    tools: dict[str, Any] = field(default_factory=dict)
    #: tool name -> the promoted verb's own name
    kit_verbs: dict[str, str] = field(default_factory=dict)
    #: tool name -> capability name, for the projected data skills
    skills: dict[str, str] = field(default_factory=dict)
    #: Why the kit verbs could not be listed, or empty when they were.
    discovery_error: str = ""

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.tools)


_CACHE: dict[tuple[str, str], BoundTools] = {}


def reset_cache() -> None:
    """Forget what was discovered (a test, or a rebind in this process)."""
    _CACHE.clear()


def _target():
    from axiom.extensions.builtins.data_platform.read_target import resolve_read_target

    target = resolve_read_target()
    if target.where != "binding" or not target.face_url:
        return None
    return target


def bound_tools() -> BoundTools | None:
    """The bound chat's data tools, discovered once per binding per process. None when not bound.

    Never raises: a node that cannot list its kit verbs still leaves the served reads, and the
    reason is kept (:attr:`BoundTools.discovery_error`) for the log and ``/tools``.
    """
    try:
        target = _target()
    except Exception as exc:  # noqa: BLE001 - an unreadable binding is no binding
        log.debug("chat: binding not readable: %s", exc)
        return None
    if target is None:
        return None
    key = (target.site, target.face_url)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    found = BoundTools(site=target.site, face_url=target.face_url)
    _add_served_reads(found)
    _add_kit_verbs(found, target)
    _CACHE[key] = found
    log.info(
        "chat: bound to %s; offering %d data tool(s), %d of them promoted kit verbs%s",
        target.site, len(found.tools), len(found.kit_verbs),
        f" ({found.discovery_error})" if found.discovery_error else "",
    )
    return found


def _served_capabilities() -> tuple[str, ...]:
    from axiom.extensions.builtins.data_platform.read_target import FOLLOWS_THE_BINDING, KIT_READS

    seen: list[str] = []
    for name in (*EVERYDAY_READS, *FOLLOWS_THE_BINDING, *KIT_READS):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _add_served_reads(found: BoundTools) -> None:
    from axiom.infra.capability_projection import (
        approval_category,
        capability_to_surface_name,
        exposed_on,
        inputs_to_json_schema,
    )
    from axiom.infra.orchestrator.actions import ActionCategory

    from .skill_tools import AGENT_SURFACE
    from .tools import ToolDef

    try:
        from axiom.extensions.builtins.data_platform.skills import bind_default

        registry = bind_default()
    except Exception as exc:  # noqa: BLE001 - no data skills here means none to offer
        log.debug("chat: data skills unavailable: %s", exc)
        return
    for capability in _served_capabilities():
        spec = registry.spec(capability)
        if spec is None or not exposed_on(spec, AGENT_SURFACE):
            continue
        if approval_category(spec) != ActionCategory.READ:
            continue
        name = capability_to_surface_name(capability)
        description = (spec.description or f"Invoke {capability}.").strip()
        found.tools[name] = ToolDef(
            name=name,
            description=f"{description} Asked of {found.site} through this install's binding.",
            category=ActionCategory.READ,
            parameters=inputs_to_json_schema(getattr(spec, "inputs", {}) or {}),
            source=TOOL_SOURCE_BOUND,
        )
        found.skills[name] = capability


_JSON_TYPES = {"int": "integer", "integer": "integer", "float": "number", "number": "number",
               "bool": "boolean", "boolean": "boolean"}


def _verb_schema(declared: Any) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for pname, pspec in (declared or {}).items() if isinstance(declared, dict) else ():
        pspec = pspec if isinstance(pspec, dict) else {}
        prop: dict[str, Any] = {"type": _JSON_TYPES.get(str(pspec.get("type") or "").lower(), "string")}
        about = str(pspec.get("description") or "").strip()
        if pspec.get("default") is not None:
            about = f"{about} Default {pspec['default']}.".strip()
        if about:
            prop["description"] = about
        properties[str(pname)] = prop
    return {"type": "object", "properties": properties}


def tool_name_for_verb(verb: str) -> str:
    """A promoted verb's tool name: its own name, made safe for a function-calling API."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(verb))[:64]


def _add_kit_verbs(found: BoundTools, target: Any) -> None:
    from axiom.extensions.builtins.data_platform.read_target import ask
    from axiom.infra.orchestrator.actions import ActionCategory

    from .tools import ToolDef

    result = ask(target, verb="data.kit_verbs", params={})
    if not result.ok:
        found.discovery_error = "the node did not list its kit verbs: " + (
            "; ".join(result.errors) or "no reason given")
        return
    rows = (result.value or {}).get("verbs") if isinstance(result.value, dict) else None
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        name = tool_name_for_verb(row["name"])
        if name in found.tools:
            continue
        about = str(row.get("description") or "").strip() or f"The promoted verb {row['name']}."
        found.tools[name] = ToolDef(
            name=name,
            description=(f"{about} A promoted kit verb of {row.get('site') or found.site}; it runs on "
                         f"the node and returns rows."),
            category=ActionCategory.READ,
            parameters=_verb_schema(row.get("params")),
            source=TOOL_SOURCE_BOUND,
        )
        found.kit_verbs[name] = str(row["name"])


def execute(name: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Run a bound tool, or None when ``name`` is not one. The answer comes from the node."""
    found = bound_tools()
    if found is None or name not in found.tools:
        return None
    if name in found.kit_verbs:
        from axiom.extensions.builtins.data_platform.read_target import ask

        target = _target()
        if target is None:
            return {"ok": False, "value": None, "errors": ["this install is no longer bound"]}
        result = ask(target, verb="data.kit_verb",
                     params={"name": found.kit_verbs[name], "params": dict(params or {})})
        return {"ok": result.ok, "value": result.value, "errors": list(result.errors),
                "actions_taken": [f"ran {found.kit_verbs[name]} on {found.site} at {found.face_url}"]}
    from .tools import _execute_skill_tool

    return _execute_skill_tool(name, params)


# ---- the turn --------------------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(text).lower()) if len(w) > 2}


def named_kit_verb(question: str, found: BoundTools | None) -> str | None:
    """The tool of a promoted verb the question names (``my_view`` or ``my view``), or None."""
    if found is None:
        return None
    lowered = str(question).lower()
    for tool, verb in sorted(found.kit_verbs.items(), key=lambda kv: -len(kv[1])):
        spelled = verb.lower()
        if spelled in lowered or spelled.replace("_", " ") in lowered:
            return tool
    return None


def is_data_question(question: str, found: BoundTools | None = None) -> bool:
    """A question about a value, a reading or a recent state, which only a data tool can answer."""
    if named_kit_verb(question, found):
        return True
    return bool(_DATA_WORDS.search(str(question or "")))


@dataclass
class DataTurn:
    """What a bound turn knows before it starts."""

    site: str
    data_tools: frozenset[str]
    is_data: bool
    named_tool: str | None = None

    def rule(self) -> str:
        shown = ", ".join(sorted(self.data_tools)[:24])
        return BOUND_RULE.format(site=self.site, tools=shown or "none were found")

    def measured(self, called: list[str], served: list[str]) -> bool:
        """A data tool ran this turn, here or on the node."""
        return bool(set(called) & self.data_tools) or bool(served)


def data_turn(question: str, all_tools: dict[str, Any]) -> DataTurn | None:
    """The bound turn's view, or None on an install that is not bound."""
    found = bound_tools()
    if found is None:
        return None
    offered = frozenset(n for n in found.names if n in all_tools)
    named = named_kit_verb(question, found)
    return DataTurn(
        site=found.site,
        data_tools=offered,
        is_data=is_data_question(question, found),
        named_tool=named if named in offered else None,
    )


def precall_block(tool: str):
    """The call the client makes itself for a verb the question names."""
    from axiom.infra.gateway import ToolUseBlock

    return ToolUseBlock(tool_id=f"call_{uuid.uuid4().hex[:12]}", name=tool, input={})


# ---- the context budget ---------------------------------------------------------------------

_CHARS_PER_TOKEN = 4
#: Tokens kept for the reply and the tool results when choosing tools to fit a window.
REPLY_RESERVE_TOKENS = 1024


def _tool_tokens(tool: Any) -> int:
    import json

    return len(json.dumps({"name": tool.name, "description": tool.description,
                           "parameters": tool.parameters}, default=str)) // _CHARS_PER_TOKEN + 8


def fit_to_context(
    all_tools: dict[str, Any],
    question: str,
    *,
    window: int,
    prompt_chars: int,
    model: str = "",
    prefer: frozenset[str] = frozenset(),
    keep: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], str]:
    """The tools that fit ``window``, chosen by the question, and a sentence saying so.

    Returns ``(all_tools, "")`` unchanged when the window is unknown or every tool fits. Otherwise
    the tools the question names (``keep``) come first, then the data tools (``prefer``) and the
    rest, each ranked by the words it shares with the question, until the window is full.
    """
    if window <= 0 or not all_tools:
        return all_tools, ""
    sizes = {name: _tool_tokens(t) for name, t in all_tools.items()}
    room = window - prompt_chars // _CHARS_PER_TOKEN - REPLY_RESERVE_TOKENS
    if sum(sizes.values()) <= room:
        return all_tools, ""
    asked = _words(question)

    def rank(name: str) -> tuple[int, int, int, str]:
        tool = all_tools[name]
        shared = len(asked & (_words(name) | _words(tool.description)))
        return (0 if name in keep else 1, 0 if name in prefer else 1, -shared, name)

    chosen: dict[str, Any] = {}
    used = 0
    for name in sorted(all_tools, key=rank):
        if used + sizes[name] > max(room, 0) and chosen:
            continue
        chosen[name] = all_tools[name]
        used += sizes[name]
    who = model or "the model"
    note = (f"offered {len(chosen)} of {len(all_tools)} tools, chosen by the question: {who}'s "
            f"context window ({window:,} tokens) cannot hold them all")
    log.info("chat: %s", note)
    return chosen, note


__all__ = [
    "BOUND_RULE",
    "BoundTools",
    "DataTurn",
    "EVERYDAY_READS",
    "NOT_A_MEASUREMENT",
    "TOOL_SOURCE_BOUND",
    "bound_tools",
    "data_turn",
    "execute",
    "fit_to_context",
    "is_data_question",
    "named_kit_verb",
    "precall_block",
    "reset_cache",
    "tool_name_for_verb",
]
