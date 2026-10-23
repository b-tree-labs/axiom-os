# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A precomputed tab-completion tree, so a TAB press imports nothing.

Completion needs the argparse tree of every verb, and the only way to learn a
verb's flags is to import its module -- which, through an extension package's
``__init__``, drags in numpy, pandas, scikit-learn, a web framework and an ORM.
Measured on a developer Mac that was ~2.3 s of CPU and 3,600 modules per
keypress; on a ten-year-old collector host it is the difference between a
usable shell and one that appears to hang.

The tree changes only when a verb's module, an extension manifest or the
installed set changes, so it is built once (by the same ``get_parser`` the
eager path always used) and serialised to JSON in the startup cache
(:mod:`axiom.extensions.index_cache`). Later presses rebuild a stub argparse
tree from the JSON -- names, help, flags, choices -- with no extension imports.
The fingerprint includes the source file of every verb module, so editing a
parser invalidates the entry; anything that goes wrong is a miss and the eager
tree is built exactly as before.

Capability gating (ADR-047: hide a verb whose requirements are unmet) is a
property of the machine *now*, not of the tree, so it is applied when the
cached tree is loaded, never baked into it.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

KIND = "completion-tree"

_STORE_ACTIONS = {
    argparse._StoreTrueAction: "store_true",
    argparse._StoreFalseAction: "store_false",
    argparse._CountAction: "count",
}


def _serialize_arg(action: argparse.Action) -> dict[str, Any]:
    out: dict[str, Any] = {"names": list(action.option_strings) or [action.dest]}
    if action.help:
        out["help"] = str(action.help)
    if action.choices:
        out["choices"] = [c if isinstance(c, str) else str(c) for c in action.choices]
    if action.metavar and isinstance(action.metavar, str):
        out["metavar"] = action.metavar
    kind = _STORE_ACTIONS.get(type(action))
    if kind:
        out["action"] = kind
    elif action.nargs is not None:
        out["nargs"] = action.nargs
    if action.option_strings and action.dest:
        out["dest"] = action.dest
    return out


def serialize_parser(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """The parts of *parser* completion can use: flags, and sub-verbs recursively."""
    doc: dict[str, Any] = {"args": [], "subs": {}}
    if parser.description:
        doc["description"] = parser.description
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            doc["sub_dest"] = action.dest
            for name, sub in action.choices.items():
                child = serialize_parser(sub)
                if helps.get(name):
                    child["help"] = str(helps[name])
                doc["subs"][name] = child
            continue
        doc["args"].append(_serialize_arg(action))
    return doc


def _add_args(parser: argparse.ArgumentParser, args: list[dict[str, Any]]) -> None:
    for spec in args:
        kwargs: dict[str, Any] = {}
        for key in ("help", "choices", "metavar", "nargs"):
            if key in spec:
                kwargs[key] = spec[key]
        if spec.get("action"):
            kwargs["action"] = spec["action"]
            kwargs.pop("nargs", None)
        if spec["names"][0].startswith("-") and spec.get("dest"):
            kwargs["dest"] = spec["dest"]
        try:
            parser.add_argument(*spec["names"], **kwargs)
        except Exception:  # noqa: BLE001 - one odd flag must not lose the rest
            pass


def _fill(parser: argparse.ArgumentParser, doc: dict[str, Any]) -> None:
    _add_args(parser, doc.get("args", []))
    subs = doc.get("subs") or {}
    if subs:
        sub_action = parser.add_subparsers(dest=doc.get("sub_dest") or None)
        for name, child in subs.items():
            sp = sub_action.add_parser(name, help=child.get("help", ""), description=child.get("description"))
            _fill(sp, child)


def build_parser(doc: dict[str, Any], *, prog: str, available) -> argparse.ArgumentParser:
    """Rebuild the top-level parser from a stored tree.

    ``available(requires) -> bool`` is the live capability gate; verbs it
    rejects are left out unless ``AXI_SHOW_UNAVAILABLE`` is set.
    """
    parser = argparse.ArgumentParser(prog=prog, description=f"{prog} CLI")
    subparsers = parser.add_subparsers(dest="subcommand")
    show_all = bool(os.environ.get("AXI_SHOW_UNAVAILABLE"))
    for entry in doc["commands"]:
        if not show_all and not available(entry.get("requires", [])):
            continue
        tree = entry["tree"]
        sp = subparsers.add_parser(entry["name"], help=entry.get("help", ""), description=tree.get("description"))
        _fill(sp, tree)
    return parser


def build_doc(full_parser: argparse.ArgumentParser, requires: dict[str, list[str]]) -> dict[str, Any]:
    """Serialise the eager (ungated) parser into the stored form."""
    commands = []
    for action in full_parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {a.dest: a.help for a in action._choices_actions}
        for name, sub in action.choices.items():
            commands.append({
                "name": name,
                "help": str(helps.get(name) or ""),
                "requires": list(requires.get(name, [])),
                "tree": serialize_parser(sub),
            })
    return {"commands": commands}


def _module_file(module: str, root: str | None) -> str | None:
    """Source file of a verb module, resolved WITHOUT importing it."""
    parts = module.split(".")
    base: str | None = None
    if parts[0] == "axiom":
        import axiom

        base = os.path.dirname(os.path.abspath(axiom.__file__))
        rel = parts[1:]
    elif root and _basename(root) in parts:
        i = len(parts) - 1 - parts[::-1].index(_basename(root))
        base, rel = root, parts[i + 1:]
    else:
        return None
    path = os.path.join(base, *rel)
    return path + ".py" if os.path.isfile(path + ".py") else os.path.join(path, "__init__.py")


def _basename(p: str) -> str:
    return os.path.basename(os.path.normpath(p))


def fingerprint_parts(core: dict[str, str | None], ext_cmds: dict[str, dict], cli_file: str) -> list[str]:
    """Everything the tree is derived from: extension tree + each verb's source."""
    from axiom.extensions import discovery, index_cache

    parts = discovery._extension_tree_fingerprint_parts(discovery.get_extension_dirs())
    parts.append(index_cache.stat_token(os.path.abspath(cli_file)))
    seen: set[str] = set()
    modules = [(m, None) for m in core.values() if m] + [
        (i["module"], i.get("root")) for i in ext_cmds.values()
    ]
    for module, root in modules:
        f = _module_file(module, root)
        if f and f not in seen:
            seen.add(f)
            parts.append(index_cache.stat_token(f))
    return parts
