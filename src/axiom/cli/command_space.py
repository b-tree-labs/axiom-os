# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One grammar for the command line: what can be typed, and what each piece holds.

Tab completion, ``explore``, the ``next:`` lines under ``--help`` and a result,
and the way ``chat`` is told to phrase a question all read this, so they cannot
drift apart. It is two registries joined once:

- the **argparse tree** of every noun, verb and flag, which the completion
  index (:mod:`axiom.cli.completion_index`) already builds and caches so a TAB
  press imports nothing; and
- the **skill registry** (ADR-056), where each verb's skill declares its
  parameters. A parameter named ``site`` holds a site; ``target`` on
  ``data.show`` holds a role or a channel because its skill says so.

The join runs when the tree is built (once per change to the installed code)
and is stored in the same document, so a TAB press, ``explore`` and ``--help``
read a value kind without importing a single skill.

Examples are built only from values that are known (Ben's rule: no placeholder
without a step that produces it). A verb whose required argument has no known
value gets no example rather than one with ``<role>`` in it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: A parameter whose name alone says what it holds. A skill may say more
#: (``SkillSpec.values``), and that wins.
PARAM_KINDS: dict[str, tuple[str, ...]] = {
    "site": ("site",),
    "sites": ("site",),
    "role": ("role",),
    "roles": ("role",),
    "channel": ("channel",),
    "channels": ("channel",),
    "feed": ("feed",),
    "day": ("day",),
    "shadow": ("shadow",),
}

#: The order flags are filled in an example: where, then what, then when.
_FLAG_ORDER = ("site", "role", "channel", "feed", "day", "shadow", "kit_verb")
#: Kinds that only disambiguate, so an example names them only when required.
_ONLY_WHEN_REQUIRED = frozenset({"feed"})

#: How a value reads in a plain-English question.
_SPOKEN = {"site": "at {}", "day": "on {}", "feed": "on feed {}"}


# --------------------------------------------------------------------------- annotation (at build time)


def _kinds_from_spec(spec: Any) -> dict[str, tuple[str, ...]]:
    """``{param: kinds}`` a skill declares: its ``values`` map, then its input names."""
    out: dict[str, tuple[str, ...]] = {}
    inputs = getattr(spec, "inputs", None) or {}
    for name in inputs:
        if name in PARAM_KINDS:
            out[name] = PARAM_KINDS[name]
    for name, kinds in (getattr(spec, "values", None) or {}).items():
        if isinstance(kinds, str):
            kinds = tuple(k for k in re.split(r"[|,\s]+", kinds) if k)
        out[name] = tuple(kinds)
    return out


def skill_candidates(path: tuple[str, ...]) -> list[str]:
    """Skill names a CLI path may wrap, most specific first (``data show`` → ``data.show``)."""
    if len(path) < 2:
        return []
    noun, rest = path[0], [p.replace("-", "_") for p in path[1:]]
    out = [f"{noun}.{'_'.join(rest)}"]
    if len(rest) > 1:
        out.append(f"{noun}.{rest[0]}")
    if rest[0].startswith("gold_"):
        out.append(f"{noun}.{rest[0][len('gold_'):]}")
    return out


def annotate(
    doc: dict[str, Any],
    specs: Mapping[str, Any] | None,
    *,
    maps: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Write each argument's value kinds, and each verb's skill and risk, into ``doc``.

    ``specs`` is ``{skill name: SkillSpec}``. A path with no skill still gets kinds
    for parameters whose names say what they are, so a verb not yet migrated to a
    skill completes ``--site`` like every other.

    ``maps`` is ``{noun: SKILL_FOR_VERB}`` for nouns that export one (ADR-243, ADR-256
    D5): a noun whose second word is a registered name (``instrument video add``)
    cannot be joined into a skill name, so its map is read first.
    """
    specs = specs or {}
    maps = maps or {}

    def candidates(path: tuple[str, ...]) -> list[str]:
        mapped = (maps.get(path[0]) or {}).get(" ".join(path[1:])) if len(path) > 1 else None
        return ([mapped] if mapped else []) + skill_candidates(path)

    def walk(tree: dict[str, Any], path: tuple[str, ...]) -> None:
        spec = None
        for name in candidates(path):
            if name in specs:
                spec = specs[name]
                tree["skill"] = name
                break
        declared = _kinds_from_spec(spec) if spec is not None else {}
        if spec is not None:
            risk = getattr(spec, "effective_risk", None) or getattr(spec, "risk", None)
            if risk:
                tree["risk"] = risk
        for arg in tree.get("args", []):
            key = arg.get("dest") or arg["names"][0].lstrip("-").replace("-", "_")
            kinds = declared.get(key) or PARAM_KINDS.get(key)
            if kinds and not arg.get("choices"):
                arg["values"] = list(kinds)
        for name, child in (tree.get("subs") or {}).items():
            walk(child, (*path, name))

    for entry in doc.get("commands", []):
        walk(entry["tree"], (entry["name"],))
    doc["annotated"] = True
    return doc


def skill_paths(space: Node) -> dict[str, tuple[str, ...]]:
    """``{skill: command path}`` for every verb the annotated space knows the skill of.

    The shortest path wins when two verbs wrap one skill. Used to print the command a
    person types for a skill, rather than guessing ``<namespace> <verb>``.
    """
    out: dict[str, tuple[str, ...]] = {}
    stack = list(space.children.values())
    while stack:
        node = stack.pop()
        if node.skill:
            known = out.get(node.skill)
            if known is None or (len(node.path), node.path) < (len(known), known):
                out[node.skill] = node.path
        stack.extend(node.children.values())
    return out


def load_specs() -> dict[str, Any]:
    """Every registered skill's spec. Imports every extension's skills: build time only."""
    from axiom.infra.skills_emit import bind_all_skills

    return bind_all_skills().specs()


# --------------------------------------------------------------------------- the model


@dataclass(frozen=True)
class Arg:
    names: tuple[str, ...]
    help: str = ""
    choices: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    required: bool = False
    flag: bool = True
    takes_value: bool = True

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def key(self) -> str:
        return self.names[0].lstrip("-").replace("-", "_")


@dataclass
class Node:
    """A noun, a verb or a sub-verb, with its flags and children."""

    path: tuple[str, ...]
    help: str = ""
    args: list[Arg] = field(default_factory=list)
    children: dict[str, Node] = field(default_factory=dict)
    skill: str = ""
    risk: str = ""

    @property
    def name(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def summary(self) -> str:
        return first_sentence(self.help)

    def find(self, path: Iterable[str]) -> Node | None:
        node: Node | None = self
        for part in path:
            if node is None:
                return None
            node = node.children.get(part)
        return node


def first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    # A sentence ends at . ? or ! before a space, but not inside "e.g." or "i.e.".
    m = re.match(r"(.+?(?<!\be\.g)(?<!\bi\.e)[.?!])(\s|$)", text)
    out = (m.group(1) if m else text).rstrip(".")
    return out[:117] + "..." if len(out) > 120 else out


def _arg(spec: dict[str, Any]) -> Arg:
    names = tuple(spec.get("names") or ())
    flag = bool(names and names[0].startswith("-"))
    takes = spec.get("action") not in ("store_true", "store_false", "count")
    nargs = spec.get("nargs")
    required = bool(spec.get("required")) if flag else nargs not in ("?", "*")
    return Arg(
        names=names,
        help=str(spec.get("help") or ""),
        choices=tuple(str(c) for c in spec.get("choices") or ()),
        values=tuple(spec.get("values") or ()),
        required=required,
        flag=flag,
        takes_value=takes,
    )


def _node(tree: dict[str, Any], path: tuple[str, ...], help_: str = "") -> Node:
    node = Node(
        path=path,
        help=help_ or str(tree.get("help") or tree.get("description") or ""),
        args=[_arg(a) for a in tree.get("args", []) if a.get("names")],
        skill=str(tree.get("skill") or ""),
        risk=str(tree.get("risk") or ""),
    )
    for name, child in (tree.get("subs") or {}).items():
        node.children[name] = _node(child, (*path, name))
    return node


def from_doc(doc: dict[str, Any] | None, *, available: Callable[[list[str]], bool] | None = None) -> Node:
    """The whole command space as a tree, rooted at the CLI itself (path ``()``)."""
    root = Node(path=())
    for entry in (doc or {}).get("commands", []):
        if available is not None and not available(entry.get("requires", [])):
            continue
        root.children[entry["name"]] = _node(entry["tree"], (entry["name"],), str(entry.get("help") or ""))
    return root


def load(*, build: bool = False) -> Node:
    """The command space from the completion index; ``build=False`` never imports a verb module.

    ``build=True`` builds the index when there is none (what ``explore`` does once).
    Help and result footers pass ``build=False`` and work from whatever is cached.
    """
    try:
        from axiom.axiom_cli import completion_doc
        from axiom.infra import cli_gating

        return from_doc(completion_doc(build=build), available=cli_gating.is_available)
    except Exception:  # noqa: BLE001 - a missing index is an empty space, never an error
        return Node(path=())


# --------------------------------------------------------------------------- examples


ValueLookup = Callable[[tuple[str, ...], "str | None"], list[str]]


def _quote(value: str) -> str:
    return value if re.fullmatch(r"[\w.:/@%+=,-]+", value) else "'" + value.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class Example:
    """A ready-to-run command line, and what filled it."""

    argv: tuple[str, ...]
    filled: tuple[tuple[str, str], ...] = ()
    #: How many of ``filled`` are positional (they come first, and read bare).
    positional: int = 0

    def line(self, cli: str) -> str:
        return " ".join([cli, *(_quote(a) for a in self.argv)])


def example_for(
    node: Node, lookup: ValueLookup, *, site: str | None = None, optional: int = 2
) -> Example | None:
    """A runnable line for ``node`` using only known values, or ``None``.

    Required arguments must all be fillable (from a choice or a known value).
    Optional flags that hold a known value are added in a fixed order, at most
    ``optional`` of them, so the example says *where* (``--site``) and *what*
    (``--role``). ``optional=0`` gives the shortest line that runs.
    """
    if not node.path:
        return None
    argv: list[str] = list(node.path)
    filled: list[tuple[str, str]] = []
    chosen_site = site

    def pick(arg: Arg) -> str | None:
        if arg.choices:
            return arg.choices[0]
        if not arg.values:
            return None
        found = lookup(arg.values, chosen_site)
        return found[0] if found else None

    # A site first, so per-site values below are the site's own.
    site_arg = next((a for a in node.args if a.flag and "site" in a.values), None)
    if site_arg is not None:
        found = lookup(("site",), None)
        chosen_site = chosen_site or (found[0] if found else None)

    positionals = [a for a in node.args if not a.flag]
    for arg in positionals:
        if not arg.required:
            continue
        value = pick(arg)
        if value is None:
            return None
        argv.append(value)
        filled.append((arg.values[0] if arg.values else arg.key, value))
    positional = len(filled)

    optional_used = 0
    flags = sorted(
        (a for a in node.args if a.flag and a.takes_value),
        key=lambda a: (not a.required, _FLAG_ORDER.index(a.values[0]) if a.values and a.values[0] in _FLAG_ORDER else 99),
    )
    for arg in flags:
        if arg.required:
            value = chosen_site if (arg is site_arg and chosen_site) else pick(arg)
            if value is None:
                return None
        else:
            if optional_used >= optional or not arg.values or arg.values[0] in _ONLY_WHEN_REQUIRED:
                continue
            if arg.values[0] == "day" and node.children:
                continue
            value = chosen_site if (arg is site_arg and chosen_site) else pick(arg)
            if value is None:
                continue
            optional_used += 1
        argv += [arg.name, value]
        filled.append((arg.values[0] if arg.values else arg.key, value))
    return Example(tuple(argv), tuple(filled), positional)


def chat_phrase(node: Node, example: Example | None = None) -> str:
    """The plain-English question that asks ``chat`` for what ``node`` does."""
    if not node.path:
        return "what can I do here?"
    if len(node.path) == 1:
        return f"what can I do with {node.path[0]}?" + (f" ({node.summary.lower()})" if node.summary else "")
    base = node.summary or " ".join(node.path[1:])
    base = base[0].lower() + base[1:] if base[:1].isupper() and not base[:2].isupper() else base
    base = base.rstrip("?.")
    words: list[str] = []
    for i, (kind, value) in enumerate(example.filled if example else ()):
        bare = example is not None and i < example.positional
        words.append(value if bare else _SPOKEN.get(kind, kind.replace("_", " ") + " {}").format(value))
    return f"{base}: {' '.join(words)}" if words else base


__all__ = [
    "Arg",
    "Example",
    "Node",
    "PARAM_KINDS",
    "annotate",
    "chat_phrase",
    "example_for",
    "first_sentence",
    "from_doc",
    "load",
    "load_specs",
    "skill_candidates",
    "skill_paths",
]
