# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axiom.infra.cli_vocabulary`` — the source-of-truth surface every
documenter and runbook validates against.

Walks every installed AEOS extension's manifest, introspects each
``kind = "cmd"`` entry-point's argparse, and produces a flat
``Vocabulary`` of:

    { noun -> { "shorts": [str], "verbs": { verb -> { "flags": [str],
                                                       "positionals": [str] } } } }

Markdown linters consume this to validate every ``axi <noun> <verb>
<flags...>`` snippet in docs against what the CLI actually accepts.
"""

from __future__ import annotations

import argparse
import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerbSpec:
    name: str
    flags: tuple[str, ...] = ()         # e.g. ("--since", "--actor")
    positionals: tuple[str, ...] = ()   # e.g. ("path",)
    short_flags: tuple[str, ...] = ()   # e.g. ("-h",)


@dataclass(frozen=True)
class NounSpec:
    name: str
    shorts: tuple[str, ...] = ()
    verbs: dict[str, VerbSpec] = field(default_factory=dict)


@dataclass
class Vocabulary:
    nouns: dict[str, NounSpec] = field(default_factory=dict)

    def has_noun(self, name: str) -> bool:
        if name in self.nouns:
            return True
        for n in self.nouns.values():
            if name in n.shorts:
                return True
        return False

    def resolve_noun(self, name: str) -> NounSpec | None:
        if name in self.nouns:
            return self.nouns[name]
        for n in self.nouns.values():
            if name in n.shorts:
                return n
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            n.name: {
                "shorts": list(n.shorts),
                "verbs": {
                    v.name: {
                        "flags": list(v.flags),
                        "positionals": list(v.positionals),
                        "short_flags": list(v.short_flags),
                    }
                    for v in n.verbs.values()
                },
            }
            for n in self.nouns.values()
        }


# ---------------------------------------------------------------------------
# Build vocabulary from installed extensions
# ---------------------------------------------------------------------------


def _flag_names(action: argparse.Action) -> tuple[list[str], list[str]]:
    """Split argparse action's option_strings into ``--long`` + ``-short``."""
    longs: list[str] = []
    shorts: list[str] = []
    for opt in action.option_strings:
        if opt.startswith("--"):
            longs.append(opt)
        elif opt.startswith("-"):
            shorts.append(opt)
    return longs, shorts


def _walk_subparsers(parser: argparse.ArgumentParser) -> dict[str, VerbSpec]:
    """Find every subparser registered under ``parser`` and emit VerbSpec."""
    verbs: dict[str, VerbSpec] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for verb_name, sub in action.choices.items():
            flags: set[str] = set()
            shorts: set[str] = set()
            positionals: list[str] = []
            for a in sub._actions:
                if a.option_strings:
                    longs, sh = _flag_names(a)
                    flags.update(longs)
                    shorts.update(sh)
                else:
                    if a.dest != "help":
                        positionals.append(a.dest)
            verbs[verb_name] = VerbSpec(
                name=verb_name,
                flags=tuple(sorted(flags)),
                short_flags=tuple(sorted(shorts)),
                positionals=tuple(positionals),
            )
    return verbs


def _load_parser_from_entry(entry: str) -> argparse.ArgumentParser | None:
    """Import a ``module:function`` entry and try to extract its parser."""
    if ":" not in entry:
        return None
    mod_path, _, attr = entry.partition(":")
    try:
        mod = importlib.import_module(mod_path)
    except Exception as exc:  # noqa: BLE001
        _log.debug("could not import %s: %s", mod_path, exc)
        return None

    # Common patterns:
    # 1. The entry point IS ``main``; the module exposes ``_build_parser``
    #    or ``_parser``.
    for parser_attr in ("_build_parser", "_parser", "build_parser", "parser"):
        if hasattr(mod, parser_attr):
            try:
                obj = getattr(mod, parser_attr)
                if callable(obj):
                    obj = obj()
                if isinstance(obj, argparse.ArgumentParser):
                    return obj
            except Exception as exc:  # noqa: BLE001
                _log.debug("calling %s.%s failed: %s", mod_path, parser_attr, exc)

    # 2. Fall back to calling ``main([--help])`` in a way that surfaces the parser
    # — not reliable enough; skip.
    return None


def build_vocabulary() -> Vocabulary:
    """Walk every installed extension's ``kind = "cmd"`` entries and build
    the canonical vocabulary."""
    from axiom.extensions.discovery import surfaced_extensions

    vocab = Vocabulary()
    try:
        extensions = surfaced_extensions()
    except Exception:
        return vocab

    for ext in extensions:
        for cli in getattr(ext, "cli_commands", []) or []:
            noun = getattr(cli, "noun", "")
            entry = getattr(cli, "entry", "")
            if not noun:
                continue
            shorts_raw = getattr(cli, "short", []) or []
            if isinstance(shorts_raw, str):
                shorts_raw = [shorts_raw]
            shorts = tuple(shorts_raw)

            parser = _load_parser_from_entry(entry)
            verbs = _walk_subparsers(parser) if parser is not None else {}

            if noun not in vocab.nouns:
                vocab.nouns[noun] = NounSpec(
                    name=noun, shorts=shorts, verbs=verbs,
                )

    return vocab


# ---------------------------------------------------------------------------
# Markdown linter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LintFinding:
    file: str
    line: int
    command: str
    issue: str   # "unknown_noun" | "unknown_verb" | "unknown_flag"
    detail: str


def _split_fenced_bash_blocks(text: str) -> Iterable[tuple[int, str]]:
    """Yield (lineno, line) for every line inside a ``` ```bash / ```sh / ```shell fence."""
    lines = text.splitlines()
    in_block = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if not in_block and stripped.startswith("```"):
            lang = stripped.strip("`").lower()
            if lang in {"bash", "sh", "shell", "console"} or lang == "":
                in_block = True
        elif in_block and stripped.startswith("```"):
            in_block = False
        elif in_block:
            yield (i + 1, ln)
        i += 1


def _lint_command_line(
    line: str, vocab: Vocabulary, file: str, lineno: int,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    stripped = line.strip()
    # Strip a leading shell prompt + comment leader
    for prefix in ("$ ", "# ", "// "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
    if not stripped.startswith(("axi ", "axi\t")):
        return findings
    # Tokenize naïvely (ignore shell quoting nuances; runbooks rarely have them on the noun/verb segment)
    tokens = stripped.split()
    if len(tokens) < 2:
        return findings
    noun = tokens[1]
    spec = vocab.resolve_noun(noun)
    if spec is None:
        findings.append(LintFinding(
            file=file, line=lineno, command=stripped,
            issue="unknown_noun",
            detail=f"noun {noun!r} not in vocabulary; "
                   f"known: {sorted(vocab.nouns.keys())}",
        ))
        return findings

    if len(tokens) < 3:
        return findings
    verb_tok = tokens[2]
    if verb_tok.startswith("-"):  # noun-level flag, not a verb
        return findings
    verb = spec.verbs.get(verb_tok)
    if verb is None:
        findings.append(LintFinding(
            file=file, line=lineno, command=stripped,
            issue="unknown_verb",
            detail=f"verb {verb_tok!r} not in noun {spec.name!r}'s verb set; "
                   f"known: {sorted(spec.verbs)}",
        ))
        return findings

    # Walk remaining tokens; report any --flag we don't recognize.
    for tok in tokens[3:]:
        if not tok.startswith("--"):
            continue
        # Strip =value if present (--foo=bar)
        flag = tok.split("=", 1)[0]
        if flag in verb.flags:
            continue
        findings.append(LintFinding(
            file=file, line=lineno, command=stripped,
            issue="unknown_flag",
            detail=f"flag {flag!r} not in `axi {spec.name} {verb.name}`; "
                   f"known: {sorted(verb.flags)}",
        ))
    return findings


def lint_markdown(text: str, *, vocab: Vocabulary, path: str = "<inline>") -> list[LintFinding]:
    findings: list[LintFinding] = []
    for lineno, line in _split_fenced_bash_blocks(text):
        if "\\" in line and line.rstrip().endswith("\\"):
            # Multi-line shell continuation — collapse with the next line.
            # For simplicity treat each physical line independently; the
            # noun/verb detection only needs the head of each command.
            pass
        findings.extend(_lint_command_line(line, vocab, path, lineno))
    return findings


__all__ = [
    "VerbSpec", "NounSpec", "Vocabulary",
    "build_vocabulary",
    "LintFinding", "lint_markdown",
]
