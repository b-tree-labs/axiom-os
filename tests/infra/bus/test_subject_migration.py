# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Subject migration enforcement.

Per the no-shim rule, the v2 bus only speaks NATS-shape subjects. This
test confirms two things:

1. The bus rejects `fnmatch`-shape multi-token patterns at subscribe time
   (the old `*` matched dots; under NATS it does not, and the bus refuses
   to silently change semantics by accepting both).
2. Every pattern actually used in the codebase passes `validate_pattern`
   under NATS rules — i.e., the migration succeeded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from axiom.infra.bus import EventBus, validate_pattern
from axiom.infra.bus.subjects import InvalidSubjectError

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestNoFnmatchSemantics:
    """Patterns that LOOK fnmatch-y must validate under strict NATS."""

    def test_star_only_matches_one_token_now(self):
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe("tool.*", lambda s, p: seen.append(s))

        bus.publish("tool.post_invoke", {})  # one token after — match.
        bus.publish("tool.post.invoke", {})  # two tokens after — no match.

        assert seen == ["tool.post_invoke"]

    def test_old_anything_pattern_replaced_by_gt(self):
        # Previously `"*"` meant "match anything" via fnmatch. Now it means
        # "match exactly one token". The cross-codebase replacement is `">"`.
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(">", lambda s, p: seen.append(s))
        bus.publish("a", {})
        bus.publish("a.b", {})
        bus.publish("a.b.c", {})
        assert seen == ["a", "a.b", "a.b.c"]

    def test_invalid_glob_chars_rejected(self):
        bus = EventBus()
        # `?` was a single-char fnmatch wildcard. Not legal in NATS.
        with pytest.raises(InvalidSubjectError):
            bus.subscribe("tool.?", lambda s, p: None)
        # Bracket classes were fnmatch-only.
        with pytest.raises(InvalidSubjectError):
            bus.subscribe("tool.[ab]", lambda s, p: None)


class TestCodebasePatternsValidate:
    """Every literal subscribe-pattern in src/ and tests/ must validate."""

    # Match: bus.subscribe("pattern", ...) or bus.subscribe_async("pattern", ...)
    SUBSCRIBE_PATTERN_RE = re.compile(
        r"""\bsubscribe(?:_async)?\s*\(\s*["']([^"']+)["']""",
    )

    def _extract_patterns(self, root: Path) -> list[tuple[Path, str]]:
        results: list[tuple[Path, str]] = []
        for py_file in root.rglob("*.py"):
            # Skip the bus package and its tests — those exercise edge cases
            # intentionally (some patterns there are deliberately invalid to
            # test rejection).
            parts = py_file.parts
            if "bus" in parts and any(p in {"infra", "infra/bus"} for p in parts):
                continue
            if "test_subjects.py" in py_file.name:
                continue
            if "test_subject_migration.py" in py_file.name:
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in self.SUBSCRIBE_PATTERN_RE.finditer(text):
                results.append((py_file, match.group(1)))
        return results

    def test_all_subscribe_patterns_validate(self):
        src = REPO_ROOT / "src"
        tests = REPO_ROOT / "tests"

        patterns = self._extract_patterns(src) + self._extract_patterns(tests)
        # Sanity: we should find at least a handful (hygiene + diagnostics
        # subscribers + signals CLI fallback).
        assert len(patterns) >= 5, (
            "expected to find subscribe() calls in the codebase; the regex"
            f" found only {len(patterns)} — has the API changed?"
        )

        failures: list[tuple[Path, str, str]] = []
        for path, pat in patterns:
            try:
                validate_pattern(pat)
            except InvalidSubjectError as exc:
                failures.append((path, pat, str(exc)))

        assert not failures, "invalid NATS-shape patterns in codebase:\n" + "\n".join(
            f"  {p.relative_to(REPO_ROOT)}: {pat!r} — {err}"
            for p, pat, err in failures
        )
