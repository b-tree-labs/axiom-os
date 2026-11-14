# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The tier map: which tier each path is in (ADR-257 D2, LOOP-S2).

Tier 0 is documents, tests and design assets. Tier 1 is extension and feature code
whose effect a flag contains. Tier 2 is everything a flag cannot contain. A path's
tier is the highest tier of every rule it matches, and a path no rule matches takes
the map's ``default``. A change is the highest tier of any path it touches.

The map is one declared file, ``.axiom/tiers.toml``, read by the pre-push hook (through
``release.guards``), by the server check (``loop-guards.yml``, the same command) and by
the lander's report. Every path in the repository's ``CODEOWNERS`` is tier 2, so the
paths that already need their owner's review stay with a person.

The tier is computed and reported. Nothing is refused for it until commit signatures
exist (ADR-257 D8, phase 6).

Patterns are anchored at the repository root: ``*`` matches within one path segment,
``**`` across segments, and a pattern also matches everything under a directory it
names. ``CODEOWNERS`` lines follow their own convention (a leading ``/`` anchors, a
name with no ``/`` matches at any depth) and are translated to the same form.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

TIER_MAP_PATH = ".axiom/tiers.toml"
TIERS = (0, 1, 2)
CODEOWNERS_CANDIDATES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A root-anchored glob as a regex; it also matches everything under a directory it names."""
    text = pattern.strip().lstrip("/")
    if text.endswith("/"):
        text += "**"
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("^" + "".join(out) + "(?:/.*)?$")


def codeowners_patterns(text: str) -> tuple[str, ...]:
    """The path patterns of a CODEOWNERS file, in the tier map's glob form."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.split()[0]
        if pattern.startswith("!"):
            continue
        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        body = pattern.lstrip("/")
        found.append(body if anchored else f"**/{body}")
    return tuple(found)


@dataclass(frozen=True)
class TierRule:
    tier: int
    name: str
    patterns: tuple[str, ...]
    compiled: tuple[re.Pattern[str], ...] = field(default=(), compare=False, repr=False)

    def matches(self, path: str) -> bool:
        return any(regex.match(path) for regex in self.compiled)


@dataclass(frozen=True)
class PathTier:
    path: str
    tier: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"path": self.path, "tier": self.tier, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ChangeTier:
    tier: int
    paths: tuple[PathTier, ...]
    source: str = ""

    @property
    def deciding(self) -> tuple[str, ...]:
        """The paths that set the change's tier."""
        return tuple(p.path for p in self.paths if p.tier == self.tier) if self.paths else ()

    def line(self) -> str:
        if not self.paths:
            return "tier 0: no paths changed"
        top = [p for p in self.paths if p.tier == self.tier]
        why = sorted({reason for p in top for reason in p.reasons})
        shown = ", ".join(p.path for p in top[:3]) + (f" and {len(top) - 3} more" if len(top) > 3 else "")
        return f"tier {self.tier}: {shown} ({'; '.join(why) or 'default'})"

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "deciding": list(self.deciding),
            "paths": [p.to_dict() for p in self.paths],
            "source": self.source,
            "line": self.line(),
        }


@dataclass(frozen=True)
class TierMap:
    default: int
    rules: tuple[TierRule, ...]
    source: str = ""

    def tier_of(self, path: str) -> PathTier:
        clean = path.strip().lstrip("/")
        hits = [rule for rule in self.rules if rule.matches(clean)]
        if not hits:
            return PathTier(clean, self.default, ("default",))
        tier = max(rule.tier for rule in hits)
        return PathTier(clean, tier, tuple(rule.name for rule in hits if rule.tier == tier))

    def classify(self, paths: Iterable[str]) -> ChangeTier:
        readings = tuple(self.tier_of(p) for p in sorted({p for p in paths if p and p.strip()}))
        tier = max((r.tier for r in readings), default=0)
        return ChangeTier(tier=tier, paths=readings, source=self.source)


def _tier(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in TIERS:
        raise ValueError(f"{where}: a tier is 0, 1 or 2, not {value!r}")
    return value


def _rule(tier: int, name: str, patterns: Iterable[str]) -> TierRule:
    pats = tuple(str(p) for p in patterns if str(p).strip())
    if not pats:
        raise ValueError(f"tier rule {name!r} names no paths")
    return TierRule(tier=tier, name=name, patterns=pats, compiled=tuple(glob_regex(p) for p in pats))


def parse_tier_map(data: dict, *, codeowners_text: str = "", source: str = "") -> TierMap:
    """A tier map from a parsed TOML document. A malformed map is refused, never guessed."""
    if not isinstance(data, dict):
        raise ValueError("the tier map must be a table")
    default = _tier(data.get("default", 1), "default")
    rows = data.get("rule") or []
    if not isinstance(rows, list):
        raise ValueError("rule must be an array of tables")
    rules: list[TierRule] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rule {index} must be a table")
        name = str(row.get("name") or f"rule {index}")
        paths = row.get("paths")
        if not isinstance(paths, list):
            raise ValueError(f"tier rule {name!r}: paths must be a list")
        rules.append(_rule(_tier(row.get("tier"), f"rule {name!r}"), name, paths))
    if codeowners_text:
        owned = codeowners_patterns(codeowners_text)
        if owned:
            tier = _tier(data.get("codeowners_tier", 2), "codeowners_tier")
            rules.append(_rule(tier, "CODEOWNERS", owned))
    return TierMap(default=default, rules=tuple(rules), source=source)


def load_tier_map(root: Path) -> TierMap | None:
    """The repository's tier map, or None when it declares none."""
    root = Path(root)
    path = root / TIER_MAP_PATH
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    owners_name = str(data.get("codeowners") or "")
    candidates = (owners_name,) if owners_name else CODEOWNERS_CANDIDATES
    text = ""
    for candidate in candidates:
        owners = root / candidate
        if owners.is_file():
            text = owners.read_text(encoding="utf-8")
            break
    return parse_tier_map(data, codeowners_text=text, source=str(path))


__all__ = [
    "TIER_MAP_PATH",
    "ChangeTier",
    "PathTier",
    "TierMap",
    "TierRule",
    "codeowners_patterns",
    "glob_regex",
    "load_tier_map",
    "parse_tier_map",
]
