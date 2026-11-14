# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Flag lifecycle: which flags are work to remove, and whose (LOOP-F11, ADR-257).

Every flag declares an ``owner`` and a ``review_by`` date (``FlagDef.owned_by`` and
``FlagDef.review_by``). Two cases are stale:

``on_past_review``    every value the flag can serve in this environment is ``on`` (no rule,
                      override or kill switch can still serve anything else) and today is past
                      its review date: the flag has done its job; remove it and its dead branch.
``off_unreferenced``  every value it can serve is its off value and no code refers to it any
                      more: only the declaration is left; remove it.

A kill switch (and a permission flag) is meant to stay and is never stale. A flag with no
owner is listed as ``unowned``: reported, never failed.

A flag's owner is the maintainer of the extension that declares it, not a site's site admins
(owner's decision, 2026-11-12). Axiom names no person, so the manifest says ``@maintainers`` and
the node setting ``flags.maintainers`` (a list of ``@principal``) says who that is. Lifecycle
notices go to those people only, never to a site admin; with the setting unset they go nowhere
and the report says so. Site admins keep rollouts, stops and feedback asks, not lifecycle work.
The report is pure; the skills read the record (rollouts, overrides) when the database is there
and say so when it is not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.features.flag_registry import ON, FlagDef
from axiom.governance.roles import LEGACY_ROLE_ALIASES, SITE_ADMIN

ON_PAST_REVIEW = "on_past_review"
OFF_UNREFERENCED = "off_unreferenced"
UNOWNED = "unowned"
#: The owner every built-in flag declares: the maintainers of the declaring extension. Who that is
#: on a node is the ``flags.maintainers`` setting; the platform names no person.
MAINTAINERS = "@maintainers"
MAINTAINERS_SETTING = "flags.maintainers"
NO_MAINTAINER_NOTE = ("no maintainer configured: set flags.maintainers to the @principals who maintain this "
                      "node's extensions; until then no lifecycle notice is sent")

#: Files the reference search reads. Anything else (binaries, data, lock files) is skipped.
CODE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".html",
                           ".sh", ".yaml", ".yml", ".j2", ".jinja"})
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "tests",
                        "test", "__tests__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
_MAX_FILE_BYTES = 2_000_000


def _is_test_file(path: Path) -> bool:
    n = path.name
    return n.startswith("test_") or n.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
                                                ".test.js", ".spec.js"))


def _pattern(key: str) -> re.Pattern[str]:
    """A quoted key ("k", 'k' or `k`), a property (flags.k, flags?.k), or the env pin AXIOM_FLAG_K."""
    e = re.escape(key)
    return re.compile(rf"[\"'`]{e}[\"'`]|\.\??{e}\b|AXIOM_FLAG_{re.escape(key.upper())}\b")


def find_references(keys: Iterable[str], roots: Iterable[str | Path]) -> dict[str, list[str]]:
    """Where each key is referenced in code under ``roots``. Manifests and tests are not references:
    a flag that only its declaration or its own tests mention is effectively gone."""
    keys = sorted(set(keys))
    found: dict[str, list[str]] = {k: [] for k in keys}
    if not keys:
        return found
    pats = {k: _pattern(k) for k in keys}
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        files = [base] if base.is_file() else base.rglob("*")
        for path in files:
            if not path.is_file() or path.suffix not in CODE_SUFFIXES or _is_test_file(path):
                continue
            if path != base and any(part in _SKIP_DIRS for part in path.relative_to(base).parts[:-1]):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = text.lower()
            present = [k for k in keys if k in lowered]  # a cheap filter before the pattern
            if not present:
                continue
            for k in present:
                if pats[k].search(text) and str(path) not in found[k]:
                    found[k].append(str(path))
    return found


def default_roots() -> list[str]:
    """The installed platform package and every discovered extension's directory, nested ones folded."""
    import axiom

    roots = {Path(axiom.__file__).resolve().parent}
    try:
        from axiom.extensions.discovery import discover_extensions

        for ext in discover_extensions():
            roots.add(Path(ext.manifest_path).resolve().parent)
    except Exception:  # noqa: BLE001 - the platform package alone is still a search
        pass
    ordered = sorted(roots, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for r in ordered:
        if not any(r == k or k in r.parents for k in kept):
            kept.append(r)
    return [str(r) for r in kept]


def _live(override: Mapping[str, Any], now: datetime) -> bool:
    exp = override.get("expires_at")
    if not exp:
        return True
    try:
        at = exp if isinstance(exp, datetime) else datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at > now


def _catch_all(rule: Mapping[str, Any]) -> bool:
    pct = rule.get("percentage")
    return (not rule.get("cohort") and not rule.get("match") and not rule.get("split")
            and (pct is None or float(pct) >= 100) and not rule.get("active_from") and not rule.get("active_until"))


def served_values(flag: FlagDef, environment: str, *, rollout: Mapping[str, Any] | None,
                  overrides: Iterable[Mapping[str, Any]], env_pin: str | None = None,
                  now: datetime | None = None) -> set[str]:
    """Every value ``flag`` can still resolve to in ``environment`` for somebody."""
    now = now or datetime.now(UTC)
    values: set[str] = set()
    rollout = rollout or {}
    if rollout.get("kill_switch"):
        values.add(flag.off_value)
    covered = False
    for rule in rollout.get("rules") or []:
        if not isinstance(rule, Mapping):
            continue
        if rule.get("split"):
            values |= {str(p.get("variant") or p.get("value")) for p in rule["split"] if isinstance(p, Mapping)}
        elif rule.get("value") is not None:
            v = flag.normalize(rule["value"])
            values.add(v if v is not None else str(rule["value"]))
        if _catch_all(rule):
            covered = True
            break
    for o in overrides:
        if o.get("key") == flag.key and _live(o, now):
            v = flag.normalize(o.get("value"))
            values.add(v if v is not None else str(o.get("value")))
    if not covered:
        values.add(env_pin or flag.default_for(environment))
    return values


def configured_maintainers(settings: Any = None) -> list[str]:
    """The ``@principal`` handles in the ``flags.maintainers`` setting; empty when it is unset.

    A handle that is a role (``@site-admin``, ``@operator``, ``@maintainers``) is dropped: this list
    names people, and a lifecycle notice never goes to a role a site admin holds."""
    try:
        if settings is None:
            from axiom.extensions.builtins.settings import store

            settings = store.SettingsStore()
        raw = settings.get(MAINTAINERS_SETTING, []) or []
    except Exception:  # noqa: BLE001 - unreadable settings: nobody is configured
        return []
    if isinstance(raw, str):
        raw = [v for v in raw.split(",")]
    out: list[str] = []
    for h in (str(v).strip() for v in raw):
        if h.startswith("@") and h.lstrip("@").split(":", 1)[0] not in _ROLE_NAMES and h not in out:
            out.append(h)
    return out


def is_maintainer(actor: str, maintainers: Iterable[str]) -> bool:
    """Whether ``actor`` is one of ``maintainers``; the ``:context`` suffix is ignored."""
    who = (actor or "").split(":", 1)[0]
    return bool(who) and who in {m.split(":", 1)[0] for m in maintainers}


#: Role handles are never maintainers (the site admin role is the thing a notice must not reach).
_ROLE_NAMES = frozenset({SITE_ADMIN, *LEGACY_ROLE_ALIASES, "admin", "owner", "maintainers", "viewer"})


def stale_report(
    flags: Mapping[str, FlagDef],
    *,
    environment: str,
    today: date,
    rollouts: Mapping[str, Mapping[str, Any]],
    overrides: Iterable[Mapping[str, Any]],
    referenced: Callable[[str], bool],
    env_pins: Mapping[str, str] | None = None,
    maintainers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The stale flags, grouped by owner, and the unowned ones. Never fails a flag."""
    overrides = list(overrides)
    env_pins = env_pins or {}
    now = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    stale: list[dict[str, Any]] = []
    unowned: list[str] = []
    for key, f in sorted(flags.items()):
        if not f.owned_by:
            unowned.append(key)
        if f.stale_exempt:
            continue
        served = served_values(f, environment, rollout=rollouts.get(key), overrides=overrides,
                               env_pin=env_pins.get(key), now=now)
        item = {"key": key, "owner": f.owned_by or UNOWNED, "extension": f.owner, "kind": f.kind,
                "review_by": f.review_by.isoformat() if f.review_by else None, "served": sorted(served)}
        if not f.multivariate and served == {ON} and f.review_by is not None and today > f.review_by:
            stale.append({**item, "case": ON_PAST_REVIEW, "days_past": (today - f.review_by).days,
                          "why": f"fully on since before its review date {f.review_by.isoformat()}: "
                                 "remove the flag and the code path it switched off"})
        elif served == {f.off_value} and not referenced(key):
            stale.append({**item, "case": OFF_UNREFERENCED,
                          "why": "off everywhere and no code refers to it any more: remove the declaration"})
    by_owner: dict[str, list[str]] = {}
    for i in stale:
        by_owner.setdefault(i["owner"], []).append(i["key"])
    maintainers = list(maintainers or [])
    return {"ok": True, "environment": environment, "today": today.isoformat(), "stale": stale,
            "by_owner": by_owner, "unowned": unowned, "declared": len(flags), "maintainers": maintainers,
            "note": "" if maintainers else NO_MAINTAINER_NOTE}


def message_for(items: list[dict[str, Any]], *, environment: str) -> tuple[str, str]:
    """One grouped message for a maintainer: (summary, body)."""
    summary = f"{len(items)} flag(s) to remove in {environment}"
    lines = [f"These feature flags are work to remove (LOOP-F11), {environment}:", ""]
    for i in items:
        lines.append(f"- {i['key']} ({i['extension'] or 'platform'}): {i['why']}")
    lines += ["", "See them with `flags stale`; a flag that must stay longer gets a later review_by in its manifest."]
    return summary, "\n".join(lines)


__all__ = ["CODE_SUFFIXES", "MAINTAINERS", "MAINTAINERS_SETTING", "NO_MAINTAINER_NOTE", "OFF_UNREFERENCED",
           "ON_PAST_REVIEW", "UNOWNED", "configured_maintainers", "default_roots", "find_references", "is_maintainer", "message_for",
           "served_values", "stale_report"]
