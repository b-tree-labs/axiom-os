#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Generate ``tokens.json`` from ``tokens.css`` and verify the two agree.

``tokens.css`` is the single source of truth for the Axiom design tokens.
This script parses its three token blocks (``:root``, ``[data-theme='dark']``,
``[data-theme='light']``), checks the invariants the CSS relies on, and emits
the same tokens as data so that non-CSS consumers (docs, native shells, design
tools) can never drift from what the web ships.

Usage::

    python scripts/build_tokens.py          # (re)write tokens.json
    python scripts/build_tokens.py --check  # verify only; exit 1 on drift

Invariants enforced (a violation is a hard error in both modes):

* the file holds exactly the three token blocks, in that order, and nothing
  else -- the light block MUST be last so it wins at equal specificity;
* the blocks declare custom properties only (component rules live with the
  consuming product);
* ``dark`` and ``light`` declare the identical set of names, and every name
  they declare is also declared on ``:root`` (the unstamped fallback);
* every ``var(--x)`` referenced anywhere in the file resolves to a ``:root``
  declaration;
* nothing in the file can trigger a network request: no at-rules, no
  ``url(``, no ``http(s)://``.

Stdlib only: this runs in CI and on air-gapped sites with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
CSS_PATH = PACKAGE_DIR / "tokens.css"
JSON_PATH = PACKAGE_DIR / "tokens.json"

#: Selector -> theme name, in the order the blocks MUST appear in the CSS.
THEME_SELECTORS: dict[str, str] = {
    ":root": "base",
    "[data-theme='dark']": "dark",
    "[data-theme='light']": "light",
}
THEME_ORDER: tuple[str, ...] = tuple(THEME_SELECTORS.values())

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_DECL_RE = re.compile(r"([A-Za-z_-][\w-]*)\s*:\s*([^;]+?)\s*(?:;|\Z)", re.DOTALL)
_VAR_REF_RE = re.compile(r"var\(\s*(--[\w-]+)")
_NETWORK_RE = re.compile(r"url\(|https?://", re.IGNORECASE)


class TokenError(ValueError):
    """The CSS violates a token-system invariant."""


def strip_comments(css: str) -> str:
    return _COMMENT_RE.sub("", css)


def parse_blocks(css: str) -> list[tuple[str, dict[str, str]]]:
    """Return ``[(selector, {name: value}), ...]`` in source order.

    Deliberately tiny: the token file is flat (no nesting, no at-rules) and
    ``build`` rejects anything else, so a full CSS parser is not needed.
    """
    body = strip_comments(css)
    if "@" in body:
        raise TokenError("tokens.css must be flat: no at-rules (@import, @media, @font-face, ...)")
    blocks: list[tuple[str, dict[str, str]]] = []
    for match in _BLOCK_RE.finditer(body):
        selector = " ".join(match.group(1).split())
        decls: dict[str, str] = {}
        for decl in _DECL_RE.finditer(match.group(2)):
            name, value = decl.group(1), " ".join(decl.group(2).split())
            if name in decls:
                raise TokenError(f"{selector}: {name} is declared twice")
            decls[name] = value
        blocks.append((selector, decls))
    leftover = _BLOCK_RE.sub("", body).strip()
    if leftover:
        raise TokenError(f"unparsed CSS outside any block: {leftover[:60]!r}")
    return blocks


def build(css: str) -> dict:
    """Parse ``css`` and return the tokens.json document, validating as it goes."""
    hits = sorted({h.lower() for h in _NETWORK_RE.findall(css)})
    if hits:
        raise TokenError(f"tokens.css must never trigger a network request; found {hits}")

    blocks = parse_blocks(css)
    selectors = [selector for selector, _ in blocks]
    expected = list(THEME_SELECTORS)
    if selectors != expected:
        raise TokenError(
            "tokens.css must contain exactly these blocks, in this order "
            f"(light last so it wins at equal specificity): {expected}; found {selectors}"
        )
    themes = {THEME_SELECTORS[selector]: decls for selector, decls in blocks}

    for theme, decls in themes.items():
        bad = [name for name in decls if not name.startswith("--")]
        if bad:
            raise TokenError(f"{theme}: token blocks hold custom properties only, found {bad}")

    base, dark, light = (themes[theme] for theme in THEME_ORDER)
    if set(dark) != set(light):
        differ = sorted(set(dark) ^ set(light))
        raise TokenError(f"dark and light must declare the same names; they differ by {differ}")
    missing = sorted(set(dark) - set(base))
    if missing:
        raise TokenError(f"declared per-theme but not on :root (the unstamped fallback): {missing}")

    referenced = {
        ref
        for decls in themes.values()
        for value in decls.values()
        for ref in _VAR_REF_RE.findall(value)
    }
    undefined = sorted(referenced - set(base))
    if undefined:
        raise TokenError(f"var() references with no :root definition: {undefined}")

    themed = set(dark)
    return {
        "$comment": "GENERATED from tokens.css by scripts/build_tokens.py; do not edit by hand.",
        "source": "tokens.css",
        "selectors": {theme: selector for selector, theme in THEME_SELECTORS.items()},
        "order": list(THEME_ORDER),
        # Declared once on :root and never overridden per theme (font stack,
        # radii, focus treatment). resolved(theme) = shared + themes[theme].
        "shared": {name: value for name, value in base.items() if name not in themed},
        "themes": {
            "base": {name: value for name, value in base.items() if name in themed},
            "dark": dark,
            "light": light,
        },
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _dig(doc: object, dotted: str) -> dict:
    node = doc
    for key in dotted.split("."):
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _describe_drift(actual: dict, expected: dict) -> list[str]:
    out: list[str] = []
    for section in ("shared", *(f"themes.{theme}" for theme in THEME_ORDER)):
        have, want = _dig(actual, section), _dig(expected, section)
        for name in sorted(set(have) | set(want)):
            if name not in have:
                out.append(f"{section}: {name} missing from tokens.json (css: {want[name]!r})")
            elif name not in want:
                out.append(f"{section}: {name} in tokens.json but not in tokens.css")
            elif have[name] != want[name]:
                out.append(f"{section}: {name} json={have[name]!r} css={want[name]!r}")
    if not out:
        out.append("tokens.json metadata differs from what tokens.css generates")
    return out


def check(css_path: Path = CSS_PATH, json_path: Path = JSON_PATH) -> list[str]:
    """Return the problems found (empty when tokens.json is exactly up to date).

    Raises ``TokenError`` when the CSS itself is invalid.
    """
    doc = build(css_path.read_text(encoding="utf-8"))
    if not json_path.exists():
        return [f"{json_path.name} is missing; run scripts/build_tokens.py"]
    on_disk = json_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(on_disk)
    except json.JSONDecodeError as exc:
        return [f"{json_path.name} is not valid JSON: {exc}"]
    if parsed != doc:
        return _describe_drift(parsed, doc)
    if on_disk != render(doc):
        return [f"{json_path.name} content matches but its formatting drifted; regenerate"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate tokens.json from tokens.css, or verify they agree."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify tokens.json matches tokens.css and write nothing (exit 1 on drift)",
    )
    parser.add_argument("--css", type=Path, default=CSS_PATH, help=f"default: {CSS_PATH}")
    parser.add_argument("--json", type=Path, default=JSON_PATH, help=f"default: {JSON_PATH}")
    args = parser.parse_args(argv)

    try:
        if args.check:
            problems = check(args.css, args.json)
            if problems:
                print(f"{args.json.name} drifted from {args.css.name}:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                print("run scripts/build_tokens.py to regenerate", file=sys.stderr)
                return 1
            print(f"OK: {args.json.name} matches {args.css.name}")
            return 0

        doc = build(args.css.read_text(encoding="utf-8"))
        text = render(doc)
        changed = not args.json.exists() or args.json.read_text(encoding="utf-8") != text
        args.json.write_text(text, encoding="utf-8")
        count = len(doc["shared"]) + len(doc["themes"]["base"])
        verb = "wrote" if changed else "unchanged"
        print(f"{verb} {args.json.name}: {count} tokens ({len(doc['shared'])} shared)")
        return 0
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
