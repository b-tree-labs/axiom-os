# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verbs an extension adds under a noun another extension owns (#1969).

Built-in areas already gather their providers under the area's noun
(``auth providers``, ``publish providers``, ``data connector add``), each verb
a thin wrapper over a skill named ``<noun>.<verb>`` (ADR-056). This gives any
extension the same reach without editing the owner's parser: a manifest block

    [[extension.provides]]
    kind = "cmd"
    noun = "<an existing noun>"
    verb = "<the verb it adds>"
    entry = "package.module:skill_function"

contributes ``<noun> <verb>``. Discovery records it under the owner's noun
(``verbs``); the top-level dispatcher hands that verb here, which binds the
entry as the capability ``<noun>.<verb>`` and runs it through the capability
chokepoint, exactly as a built-in verb's wrapper does. The flags become the
skill's params; the result prints as the value, then what was done, then any
errors on stderr.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import sys
from typing import Any


def capability_name(noun: str, verb: str) -> str:
    """``rag parsers`` → ``rag.parsers``: the name the built-in verbs use (ADR-056)."""
    return f"{noun}.{verb}".replace("-", "_")


def load_entry(info: dict[str, Any], alias: str) -> Any:
    """The callable an extension's ``entry`` names, loaded as its kind of extension needs."""
    from axiom.extensions.entry_loader import load_entry_module

    module_path = str(info["module"])
    function = str(info.get("function") or "main")
    mod = load_entry_module(
        module_path, root=info.get("root") or "", builtin=bool(info.get("builtin")), alias=alias
    )
    fn = getattr(mod, function, None)
    if fn is None:
        raise ImportError(f"entry {module_path}:{function} names a symbol the module lacks")
    return fn


def params_from_argv(argv: list[str]) -> dict[str, Any]:
    """Flags to skill params: ``--a-b v`` / ``--a-b=v`` → ``a_b``, a bare ``--flag`` → True,
    words that are not flag values → ``args``."""
    params: dict[str, Any] = {}
    args: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--") and len(tok) > 2:
            key, eq, value = tok[2:].partition("=")
            key = key.replace("-", "_")
            if eq:
                params[key] = value
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                params[key] = argv[i + 1]
                i += 1
            else:
                params[key] = True
        else:
            args.append(tok)
        i += 1
    if args:
        params["args"] = args
    return params


def dispatch(noun: str, verb: str, info: dict[str, Any], argv: list[str], *, cli: str = "axi") -> int:
    """Run the contributed ``noun verb`` with *argv*; return its exit code."""
    if any(a in ("-h", "--help") for a in argv):
        print(f"usage: {cli} {noun} {verb} [--NAME VALUE ...] [WORD ...]\n")
        print(info.get("description") or "")
        print(f"\nprovided by extension {info.get('extension')!r} "
              f"(capability {capability_name(noun, verb)})")
        return 0

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    name = capability_name(noun, verb)
    registry = SkillRegistry()
    registry.register(name, load_entry(info, f"{noun}.{verb}"))
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger(f"axi.{name}"),
        user_prompt=None,
    )
    result = invoke_capability(registry, name, params_from_argv(argv), ctx, surface=CLI_SURFACE)
    if result.value is not None:
        value = result.value
        print(value if isinstance(value, str) else json.dumps(value, indent=2, default=str))
    for line in result.actions_taken:
        print(line)
    for err in result.errors:
        print(err, file=sys.stderr)
    return result.exit_code


_CHOICES = re.compile(r"\{([A-Za-z0-9_,.-]+)\}")


def owner_verbs(noun: str, owner: dict[str, Any]) -> set[str]:
    """The verbs the noun's owner builds itself, from its parser (or its ``--help``).

    A contributed verb never shadows one of these. An owner that exposes a
    parser builder is read directly; otherwise its own ``--help`` is captured
    and the verb list read off the usage line. Empty when neither says.
    """
    try:
        handler = load_entry({**owner, "function": owner.get("function") or "main"}, noun)
    except Exception:  # noqa: BLE001 - an owner that will not load is reported by its own dispatch
        return set()
    mod = sys.modules.get(getattr(handler, "__module__", "") or "")
    for builder in ("get_parser", "build_parser", "_build_parser", "_parser"):
        fn = getattr(mod, builder, None) if mod is not None else None
        if callable(fn):
            try:
                parser = fn()
                for action in parser._actions:  # noqa: SLF001 - argparse has no public walk
                    if action.__class__.__name__ == "_SubParsersAction":
                        return set(action.choices)
            except Exception:  # noqa: BLE001 - fall back to --help
                break
    buf = io.StringIO()
    saved = sys.argv
    sys.argv = [f"{noun}", "--help"]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            handler()
    except BaseException:  # noqa: BLE001 - argparse exits after help; anything else says nothing
        pass
    finally:
        sys.argv = saved
    found = _CHOICES.search(buf.getvalue())
    return set(found.group(1).split(",")) if found else set()


def owner_collision(noun: str, verb: str, owner: dict[str, Any]) -> str:
    return (
        f"verb {noun} {verb}: extension {owner['verbs'][verb]['extension']!r} contributes it, "
        f"and the noun's owner {owner['extension']!r} already has it; "
        f"{owner['extension']!r} keeps it. Fix: rename the verb in "
        f"{owner['verbs'][verb]['extension']!r}."
    )


def help_section(noun: str, verbs: dict[str, dict[str, Any]]) -> str:
    """The lines ``<noun> --help`` adds: each contributed verb and the extension that provides it."""
    if not verbs:
        return ""
    width = max(len(v) for v in verbs)
    lines = [f"\nverbs other extensions add under {noun}:"]
    for verb in sorted(verbs):
        info = verbs[verb]
        lines.append(f"  {verb.ljust(width)}  {info.get('description') or ''}  "
                     f"[from {info.get('extension')}]")
    return "\n".join(lines)
