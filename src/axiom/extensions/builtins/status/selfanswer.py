# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deterministic answers to questions about myself.

Carrying the facts in the system prompt is necessary but NOT sufficient:
on a node whose model is small and whose tool list is long, the model
can still wander (observed live — asked about federation, a 7B model
reached for an unrelated tool and took two minutes). Ben's requirement
was "wicked fast … not a lengthy reasoning call".

So self-questions take the SAME road the oversight courier takes
(deterministic to the words): a narrow, high-precision matcher answers
from the live facts with NO model round trip at all — milliseconds,
byte-stable, impossible to hallucinate. Anything the matcher is not
certain about falls through to the model, which still has the facts in
its prompt.

Precision over recall, deliberately: a miss costs a slower (still
correct) answer; a false positive would answer the wrong question
confidently. Every pattern here names one fact the node actually holds.
"""

from __future__ import annotations

import re
from typing import Any

from .selfknowledge import node_profile, node_summary

# A question is about ME only when it ASKS ABOUT MY STATE. Bare "I"/"my"
# is far too loose — "how do I federate two reactors in the report?" is a
# domain question that must reach the model, so the subject forms here are
# explicit state-asking ones. Precision over recall, on purpose.
_SELF = (
    r"(?:am i|are you|are we|is this (?:node|agent|system)|do you|"
    r"does this (?:node|agent|system)|your|yourself|this node|this agent)"
)

# "Am I connected to the site?", "which site am I bound to", "can I reach the site node from here":
# questions about THIS install's site binding, which are not questions about federation peers. They
# used to fall into the federation rule below (its "connected to" matched), and a person asking
# whether their laptop reached the site was told which peers a node registry held. Answered from
# the same report ``binding.status`` returns, so chat, the CLI and MCP say one thing.
_BINDING = re.compile(
    r"\b(?:am i|are we|is this (?:machine|laptop|install|computer))\b[^?]*"
    r"\b(?:connected|bound|binding|linked|attached)\b[^?]*\b(?:site|node|server)\b"
    r"|\bwhich (?:site|node)\b[^?]*\b(?:bound|connected|using|reading)\b"
    r"|\b(?:can|could) (?:i|we) (?:reach|connect to|talk to|get to|see)\b[^?]*\b(?:site|node)\b",
    re.I,
)
# "Check my setup", "is there a newer version of neut?": questions about this install's own health.
# A bound laptop's model is the NODE's, and the node's model runs its own tools and ignores the ones
# a laptop offers, so a model-chosen tool is not how these reach the right machine. They are answered
# here, from the same skills (``doctor.run``, ``update.check``) a local model would call. Changing the
# install is never done here: see ``wants_update`` and the agent's confirmation.
_DOCTOR = re.compile(
    r"\b(?:check|diagnose|verify|test|fix|run)\b[^?]*\b(?:my|this|the)\s+(?:setup|install(?:ation)?|environment)\b"
    r"|\b(?:run|do) (?:a |the )?(?:doctor|health ?check)\b"
    r"|\bis (?:my|this) (?:setup|install(?:ation)?) (?:ok|okay|working|healthy|broken)\b",
    re.I,
)
_UPDATE_CHECK = re.compile(
    r"\b(?:is there|are there|any) (?:a |an )?(?:newer|new|latest|updated)\b[^?]*\b(?:version|release|update)s?\b"
    r"|\b(?:am i|is (?:my|this) (?:install(?:ation)?|\w+)) (?:up to date|out of date|on the latest)\b"
    r"|\bcheck for updates?\b",
    re.I,
)
# Asking chat to CHANGE the install. Narrow on purpose: it must name the product or the install, so
# "update the procedure to the latest version" is never taken for it.
_UPDATE_APPLY = re.compile(
    r"\b(?:update|upgrade)\b[^?]*\b(?:neut|axi|axiom|neutron ?os|(?:my|this) install(?:ation)?)\b",
    re.I,
)
_FEDERATION = re.compile(rf"\b{_SELF}\b[^?]*\b(federat\w*|peer\w*|connected to)\b", re.I)
_VERSION = re.compile(rf"\b(what|which)\b[^?]*\bversion\b|{_SELF}\s+version\b", re.I)
_EXTENSIONS = re.compile(rf"\b{_SELF}\b[^?]*\b(extensions?|installed|capabilit\w+)\b", re.I)
_TOOLS = re.compile(
    rf"\b{_SELF}\b[^?]*\b(tools?|skills?)\b[^?]*\b(have|got|can)\b|\bwhat tools\b", re.I
)
_SITE = re.compile(rf"\b(site|tenant)\b[^?]*\b{_SELF}\b|\b{_SELF}\b[^?]*\b(site|tenant)\b", re.I)
_SIGNIN = re.compile(
    rf"\b(sign[- ]?in|login|log in|auth\w*|sso|oidc)\b[^?]*\b(configur\w+|set up|enabled|work\w*)\b|{_SELF}[^?]*\b(sso|oidc|sign[- ]?in)\b",
    re.I,
)


def _binding_answer() -> str:
    from axiom.extensions.builtins.install_health.skills.binding import report

    return report(probe=True)["text"]


def _doctor_answer() -> str:
    from axiom.extensions.builtins.install_health.skills.doctor import run

    result = run({})
    return str((result.value or {}).get("text") or "") if result.ok else "; ".join(result.errors)


def _update_check_answer() -> str:
    from axiom.extensions.builtins.install_health.skills.update_check import run

    result = run({})
    return str((result.value or {}).get("text") or "") if result.ok else "; ".join(result.errors)


def wants_update(user_input: str) -> bool:
    """True when the person asks chat to update this install (a change, so it is confirmed first)."""
    text = (user_input or "").strip()
    return (
        bool(text)
        and len(text) <= 400
        and bool(_UPDATE_APPLY.search(text))
        and not _UPDATE_CHECK.search(text)
    )


def _fed_answer() -> str:
    fed = node_summary()["federation"]
    if not fed["configured"]:
        return (
            "I'm not federated with any peer right now — my node registry is "
            "empty, so there's no site or peer I can reach or be reached by. "
            "If you expected a federation link here, it hasn't been "
            "established on this node."
        )
    lines = [f"- {p['name']}" + (f" ({p['url']})" if p.get("url") else "") for p in fed["peers"]]
    n = len(fed["peers"])
    return (
        f"I'm federated with {n} peer{'s' if n != 1 else ''}:\n"
        + "\n".join(lines)
        + "\n\nThat's my whole registry — anything not listed, I'm not federated with."
    )


def _version_answer() -> str:
    v = node_summary()["version"]
    return (
        f"I'm running Axiom {v['distribution']}, from {v['source_path']}. "
        "(The path matters on a dev box: that's the code actually serving "
        "this conversation, which can differ from the installed wheel.)"
    )


def _site_answer() -> str:
    site = node_summary()["site"]
    return (
        f"My site scope is '{site}' — reads and writes here are bounded to it."
        if site
        else "I have no site scope set, so I'm running unscoped (a local/dev posture)."
    )


def _extensions_answer() -> str:
    exts = node_summary()["extensions"]
    return f"I have {len(exts)} extensions installed: " + ", ".join(exts) + "."


def _tools_answer() -> str:
    tools = node_profile("chat_tools")["sections"]["chat_tools"]
    if not isinstance(tools, list):
        return ""
    head = ", ".join(tools[:25])
    more = f" …and {len(tools) - 25} more" if len(tools) > 25 else ""
    return (
        f"I have {len(tools)} tools wired into this conversation: {head}{more}. "
        "Ask me to describe any one of them and I'll read it from the registry."
    )


def _signin_answer() -> str:
    gate = node_profile("gate")["sections"]["gate"]
    if not isinstance(gate, dict) or "unavailable" in gate:
        return ""
    providers = gate.get("oidc_providers") or []
    if providers:
        names = ", ".join(p["label"] or p["name"] for p in providers)
        line = f"Single sign-on is configured here: {names}."
    else:
        line = (
            "No single sign-on provider is configured here — sign-in is by password or magic link."
        )
    if gate.get("self_signup"):
        line += " Self-signup is open on this node."
    return line


#: (matcher, answer builder). Order matters: the first confident match wins.
#: The middle item names what answered, so a surface can say so (``binding.status`` is also a tool).
_RULES: list[tuple[re.Pattern[str], str, Any]] = [
    (_BINDING, "binding.status", _binding_answer),
    (_DOCTOR, "doctor.run", _doctor_answer),
    (_UPDATE_CHECK, "update.check", _update_check_answer),
    (_FEDERATION, "status.federation", _fed_answer),
    (_SIGNIN, "status.signin", _signin_answer),
    (_SITE, "status.site", _site_answer),
    (_VERSION, "status.version", _version_answer),
    (_TOOLS, "status.tools", _tools_answer),
    (_EXTENSIONS, "status.extensions", _extensions_answer),
]


def answer_with_source(user_input: str) -> tuple[str, str] | None:
    """``(answer, what answered)`` when the question is certainly about this node, else ``None``."""
    text = (user_input or "").strip()
    if not text or len(text) > 400:
        return None
    for pattern, source, build in _RULES:
        if pattern.search(text):
            try:
                answer = build()
            except Exception:  # noqa: BLE001 — never break a turn to be clever
                return None
            if answer:
                return answer, source
    return None


def answer_about_self(user_input: str) -> str | None:
    """A deterministic answer when the question is certainly about this
    node, else ``None`` (the model handles it, facts already in prompt)."""
    found = answer_with_source(user_input)
    return found[0] if found else None


__all__ = ["answer_about_self", "answer_with_source", "wants_update"]
