# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Names the harness already owns, and what happens when we publish one too.

A person types ``/renew`` in a harness that also has Axiom's MCP server
configured, and Axiom publishes something called ``renew``. Which one answers?
The measured answer (see ``harness_profiles.toml``, every claim marked by how
it was established) is that it depends on the harness, and that the harness
usually settles it before the model is involved:

- A harness-owned slash command is executed by the harness (Claude Code in
  ``-p`` mode answers ``/resume`` itself, with zero model turns).
- An MCP *tool* is never a slash command. It reaches the model under a
  harness-chosen name (``mcp__axiom__axiom_x__y`` in Claude Code,
  ``axiom_axiom_x__y`` in OpenCode).
- An MCP *prompt* becomes a slash command only in harnesses that surface
  prompts, and Claude Code prefixes it (``/mcp__axiom__<prompt>``).
- A slash line a harness does not own is passed to the model as text, and the
  model may then reach for any tool whose name or description resembles it.

So collisions are not decided by luck; they are decided by names we control.
This module is the detector, and it is a pure function of two inputs: the names
Axiom exposes and a harness profile. It does not start a harness.

Rules (each finding names its rule, harness, and the offending name):

``reserved-exact``
    the harness-visible name of a tool, or the slash form of a prompt, equals a
    command the harness reserves.
``bare-verb``
    an exposed name is not namespaced (``axiom_<ext>_<verb>``; prompts
    ``<ext>.<name>``). A bare verb is the shape that collides in every harness
    that does not prefix, including the ones nobody has measured yet.
``transform-collision``
    two exposed names are the same string after the harness's naming transform
    (sanitisation, case folding, truncation).
``verb-shadow``
    the verb of a name matches (or is one edit from) a reserved command, and the
    description lacks a clause saying it is not that command.
``session-owning``
    the verb is one of Axiom's own chat session-owning commands. Those belong to
    the surface that owns the conversation and are never exposed over MCP.

Severity follows provenance. A reserved name or naming rule marked
``unverified`` produces an *advisory*, not an *error*: we do not fail a build
on a behaviour nobody has observed, and we do not stay silent about it either.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Exposed",
    "Finding",
    "HarnessProfile",
    "apply_client_coercion",
    "exposed_surface",
    "find_collisions",
    "harness_for_client",
    "load_profiles",
    "resolve_client_name",
    "restore_tool_name",
    "visible_name",
]

PROFILES_PATH = Path(__file__).with_name("harness_profiles.toml")

#: Provenance marks that may fail a build. ``unverified`` may not.
TRUSTED = frozenset({"verified-by-run", "from-binary", "from-docs"})

_COMPOSED_SERVER = "axiom"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exposed:
    """One name Axiom publishes to a client."""

    name: str
    kind: str  # "tool" | "prompt"
    server: str
    description: str = ""


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # "error" | "advisory"
    harness: str
    name: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return (
            f"[{self.rule}] {self.severity} harness={self.harness} "
            f"{self.kind}={self.name}: {self.detail}"
        )


@dataclass(frozen=True)
class HarnessProfile:
    name: str
    client_names: tuple[str, ...]
    tool_template: str
    tool_status: str
    sanitize: str
    max_len: int
    max_len_status: str
    prompts_surface: bool
    prompt_template: str
    prompt_status: str
    #: reserved command -> provenance mark
    reserved: dict[str, str] = field(default_factory=dict)
    hide: frozenset[str] = frozenset()
    rename: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def reserved_trusted(self) -> set[str]:
        return {n for n, m in self.reserved.items() if m in TRUSTED}


def load_profiles(path: Path | str | None = None) -> dict[str, HarnessProfile]:
    """Read the profile data. The file, not code, is the source of truth."""
    data = tomllib.loads(Path(path or PROFILES_PATH).read_text(encoding="utf-8"))
    out: dict[str, HarnessProfile] = {}
    for name, h in (data.get("harness") or {}).items():
        tool = h["tool_name"]
        prompt = h["prompt_slash"]
        reserved: dict[str, str] = {}
        for mark, names in (h.get("slash") or {}).items():
            for n in names:
                # the strongest claim wins if a name appears under two marks
                if reserved.get(n) in TRUSTED and mark not in TRUSTED:
                    continue
                reserved[str(n).lower()] = mark.replace("_", "-")
        coerce = h.get("coerce") or {}
        out[name] = HarnessProfile(
            name=name,
            client_names=tuple(h.get("client_names") or (name,)),
            tool_template=tool["template"],
            tool_status=tool["status"],
            sanitize=tool.get("sanitize", ""),
            max_len=int(tool.get("max_len") or 0),
            max_len_status=tool.get("max_len_status", "unverified"),
            prompts_surface=bool(prompt.get("surfaces")),
            prompt_template=prompt["template"],
            prompt_status=prompt["status"],
            reserved=reserved,
            hide=frozenset(coerce.get("hide") or ()),
            rename=dict(coerce.get("rename") or {}),
            raw=h,
        )
    return out


def session_owning_commands(path: Path | str | None = None) -> tuple[str, ...]:
    data = tomllib.loads(Path(path or PROFILES_PATH).read_text(encoding="utf-8"))
    return tuple(str(c).lower() for c in (data.get("axiom_chat") or {}).get("session_owning", ()))


# ---------------------------------------------------------------------------
# The real surface
# ---------------------------------------------------------------------------


def exposed_surface(surface: Any = None) -> list[Exposed]:
    """Every tool and prompt Axiom publishes, from the servers themselves.

    The composed server's surface (tools + prompts) and the memory server's
    tool list, read the way ``tools/list`` and ``prompts/list`` would return
    them. Pass ``surface`` to examine a synthetic one.
    """
    if surface is None:
        from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

        surface = AggregationRegistry.from_node().build()
    out = [
        Exposed(t.name, "tool", _COMPOSED_SERVER, t.description or "")
        for t in surface.tools
    ]
    out += [
        Exposed(p.name, "prompt", _COMPOSED_SERVER, getattr(p, "description", "") or "")
        for p in surface.prompts
    ]
    from axiom.extensions.builtins.memory import mcp_server as memory_server

    out += [
        Exposed(t.name, "tool", memory_server.SERVER_NAME, t.description or "")
        for t in memory_server._TOOLS
    ]
    return out


# ---------------------------------------------------------------------------
# The transform
# ---------------------------------------------------------------------------


def _sanitize(text: str, rule: str) -> str:
    if rule == "[^A-Za-z0-9_-]->_":
        return re.sub(r"[^A-Za-z0-9_-]", "_", text)
    return text


def visible_name(item: Exposed, profile: HarnessProfile) -> str | None:
    """What the harness calls this, or ``None`` when it does not surface it."""
    if item.kind == "tool":
        template = profile.tool_template
    elif profile.prompts_surface:
        template = profile.prompt_template
    else:
        return None
    return template.format(
        server=_sanitize(item.server, profile.sanitize),
        tool=_sanitize(item.name, profile.sanitize),
        prompt=_sanitize(item.name, profile.sanitize),
    )


def _truncate(name: str, profile: HarnessProfile) -> str:
    return name[: profile.max_len] if profile.max_len and len(name) > profile.max_len else name


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

# axiom_<ext>_<verb> (memory server) or axiom_<ext>__<verb> (composed server):
# the part after "axiom_" must hold at least an extension token and a verb token.
_NAMESPACED_TOOL = re.compile(r"^axiom_(?=[a-z0-9_]*[a-z0-9]_+[a-z0-9])[a-z0-9_]+$")
_NAMESPACED_PROMPT = re.compile(r"^(?:axiom_[a-z0-9_]+|[a-z0-9][a-z0-9_-]*\.[A-Za-z0-9_.-]+)$")


def verbs(name: str) -> list[str]:
    """The tokens of a name that a person would call "the verb".

    ``axiom_data__kit_init`` -> ``kit_init`` and ``init``. Both, because a
    person (and a model) reads the last word.
    """
    core = name.split("__")[-1] if "__" in name else name.rsplit("_", 1)[-1]
    core = core.split(".")[-1]
    toks = {core, core.rsplit("_", 1)[-1]}
    return sorted(t.lower() for t in toks if t)


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def disambiguates(description: str, reserved: str) -> bool:
    """Does the description say what this is *not*?

    A clause with "not" that names the reserved command, or names the harness,
    the client, or "slash command".
    """
    text = description.lower()
    for clause in re.split(r"[.;\n]", text):
        if re.search(r"\b(not|isn't|unlike|rather than|instead of)\b", clause) and (
            re.search(rf"(?<![a-z0-9_]){re.escape(reserved)}(?![a-z0-9_])", clause)
            or re.search(r"\b(harness|client|slash)", clause)
        ):
            return True
    return False


def _sev(mark: str) -> str:
    return "error" if mark in TRUSTED else "advisory"


def find_collisions(
    exposed: list[Exposed] | list[str],
    profile: HarnessProfile,
    *,
    session_owning: tuple[str, ...] | None = None,
) -> list[Finding]:
    """Everything wrong with publishing ``exposed`` to ``profile``'s harness.

    ``exposed`` may be bare names (treated as tools with no description) so a
    caller can ask the one-line question "would this name collide?".
    """
    items = [
        e if isinstance(e, Exposed) else Exposed(str(e), "tool", _COMPOSED_SERVER)
        for e in exposed
    ]
    owning = session_owning_commands() if session_owning is None else session_owning
    found: list[Finding] = []

    seen: dict[str, Exposed] = {}
    for item in items:
        vis = visible_name(item, profile)
        slash = None
        if vis is not None:
            slash = vis.lstrip("/").lower()
        # (i) exact collision with a reserved slash command. A tool's visible
        # name is compared as-is; a prompt's slash form likewise.
        if slash is not None and slash in profile.reserved:
            mark = profile.reserved[slash]
            found.append(
                Finding(
                    "reserved-exact", _sev(mark), profile.name, item.name, item.kind,
                    f"harness shows it as {vis!r}, which is the harness's own "
                    f"/{slash} ({mark})",
                )
            )
        # the bare form a person types, regardless of prefixing
        bare = item.name.lower().lstrip("/")
        if bare in profile.reserved and bare != slash:
            mark = profile.reserved[bare]
            found.append(
                Finding(
                    "reserved-exact", _sev(mark), profile.name, item.name, item.kind,
                    f"the name itself is the harness's own /{bare} ({mark}); the "
                    f"harness prefixes it as {vis!r} here, but a client that does "
                    "not would shadow it",
                )
            )

        # (ii) bare verb
        namespaced = bool(
            (_NAMESPACED_TOOL if item.kind == "tool" else _NAMESPACED_PROMPT).match(item.name)
        )
        if not namespaced:
            found.append(
                Finding(
                    "bare-verb", "error", profile.name, item.name, item.kind,
                    "not namespaced (tools are axiom_<ext>_<verb>, prompts are "
                    "<ext>.<name>); a bare verb collides with whatever a harness "
                    "calls the same word",
                )
            )

        # session-owning chat commands are never exposed
        for v in verbs(item.name):
            if v in owning:
                found.append(
                    Finding(
                        "session-owning", "error", profile.name, item.name, item.kind,
                        f"verb {v!r} is an Axiom chat session-owning command; a "
                        "session is changed by the surface that owns it, never "
                        "through an MCP tool or prompt",
                    )
                )

        # (iv) verb shadow / near-collision without a disambiguating clause
        for v in verbs(item.name):
            for r, mark in profile.reserved.items():
                near = v == r or (len(v) >= 4 and len(r) >= 4 and edit_distance(v, r) <= 1)
                if near and not disambiguates(item.description, r):
                    found.append(
                        Finding(
                            "verb-shadow", _sev(mark), profile.name, item.name, item.kind,
                            f"verb {v!r} {'equals' if v == r else 'is one edit from'} the "
                            f"harness's /{r} ({mark}) and the description has no clause "
                            f"saying what it is not (e.g. \"not the harness's own /{r}\")",
                        )
                    )

        # (iii) collisions after transform
        if vis is not None:
            if profile.max_len and len(vis) > profile.max_len:
                found.append(
                    Finding(
                        "transform-collision", _sev(profile.max_len_status), profile.name,
                        item.name, item.kind,
                        f"harness name {vis!r} is {len(vis)} chars, over its "
                        f"{profile.max_len} limit ({profile.max_len_status}); truncation "
                        "can merge it with a sibling",
                    )
                )
            key = (item.kind, _truncate(vis, profile).lower())
            other = seen.get(key)  # type: ignore[arg-type]
            if other is not None and (other.server, other.name) != (item.server, item.name):
                other_vis = visible_name(other, profile) or ""
                # Merged only because of the length cut: the claim rests on the
                # limit's provenance, not on the naming template's.
                truncated = other_vis.lower() != vis.lower()
                mark = profile.max_len_status if truncated else profile.tool_status
                found.append(
                    Finding(
                        "transform-collision", _sev(mark), profile.name,
                        item.name, item.kind,
                        f"becomes {key[1]!r} in this harness, the same as "
                        f"{other.name!r} on server {other.server!r}"
                        + (" (after truncation)" if truncated else ""),
                    )
                )
            else:
                seen[key] = item  # type: ignore[index]
    return found


# ---------------------------------------------------------------------------
# Server-side coercion hook (defaults to no change)
# ---------------------------------------------------------------------------


def resolve_client_name(env: dict[str, str] | None = None, client_info_name: str | None = None) -> str:
    """Which harness is talking, by the two signals the server has today.

    The installer stamps ``AXIOM_MCP_CLIENT=<spec.name>`` into the entry it
    writes, which is the harness's own TOOL_SPECS name. A harness started from
    a hand-written config has no stamp, so the ``clientInfo.name`` from the
    ``initialize`` handshake is the fallback. Neither present: ``""``.
    """
    import os

    environ = os.environ if env is None else env
    stamped = (environ.get("AXIOM_MCP_CLIENT") or "").strip()
    if stamped and stamped != "unknown":
        return stamped
    return (client_info_name or "").strip()


def harness_for_client(
    client_name: str, profiles: dict[str, HarnessProfile] | None = None
) -> HarnessProfile | None:
    profiles = load_profiles() if profiles is None else profiles
    key = client_name.strip().lower()
    if not key:
        return None
    if key in profiles:
        return profiles[key]
    for p in profiles.values():
        if key in {c.lower() for c in p.client_names}:
            return p
    return None


def apply_client_coercion(tools: list[Any], profile: HarnessProfile | None) -> list[Any]:
    """The tool list this harness should see.

    ``hide`` removes a tool, ``rename`` lists it under another name (the call is
    routed back with :func:`restore_tool_name`). An unknown harness or an empty
    ``coerce`` table returns ``tools`` unchanged, the same list object.
    """
    if profile is None or not (profile.hide or profile.rename):
        return tools
    out: list[Any] = []
    for t in tools:
        if t.name in profile.hide:
            continue
        new = profile.rename.get(t.name)
        out.append(t.model_copy(update={"name": new}) if new else t)
    return out


def restore_tool_name(name: str, profile: HarnessProfile | None) -> str:
    """Map a harness-facing renamed tool back to the name the server dispatches."""
    if profile is None or not profile.rename:
        return name
    for original, shown in profile.rename.items():
        if shown == name:
            return original
    return name
