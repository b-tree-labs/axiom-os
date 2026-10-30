# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Core code names a connector, never a storage vendor (ADR-220 §1).

A storage vendor is known only inside its provider package
(``connector/storage/<vendor>/``). Everywhere else reaches it through the
registry by a connector's ``kind``. This guard scans shipped code (tests are out
of scope, as for the domain guard) for a vendor's module paths, class names, API
hosts, environment variables and string branches.

**The debt list only shrinks.** ``DEBT`` names every file that still knows a
vendor on the day the guard landed (phase 1). Phase 2 switches those callers to
the registry and deletes their lines; the guard FAILS if a listed file is
already clean, so the list stays a to-do rather than an exemption. A new file is
clean by default.

``NOT_THE_VENDOR`` is different and permanent: files where the same word means
something else (a box plot). Only the bare-word and constant-name patterns are
skipped there; module, class, host and setting patterns still apply.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "axiom"
PROVIDERS = ROOT / "extensions" / "builtins" / "connector" / "storage"

#: Vendors with a provider package. Add one when its provider lands.
VENDORS = ("box",)


def _patterns(v: str) -> dict[str, re.Pattern[str]]:
    cap = v.capitalize()
    return {
        "module": re.compile(
            rf"sources[./]{v}\b|storage[./]{v}\b|providers[./]{v}\b|\b{v}_browser\b"
        ),
        "class": re.compile(rf"\b{cap}[A-Z][A-Za-z]+"),
        "host": re.compile(rf"\b{v}\.com\b"),
        "env": re.compile(rf"\b(?:AXIOM_|DOCFLOW_|DP1_)?{v.upper()}_[A-Z]+"),
        "setting": re.compile(
            rf"\boff{v}\b|\b{v}_(?:login|folder_id|secret_ref|remote|app|run)\b"
            rf"|\brun_{v}_to\b"
        ),
        "word": re.compile(rf"[\"']{v}(?:-[a-z]+)?[\"']"),
    }


#: Files where the bare word is a different word. Permanent; reason required.
NOT_THE_VENDOR = {
    "extensions/builtins/scidisplay/suggest.py": "a box plot",
    "extensions/builtins/scidisplay/chart_kinds.py": "a box plot and a frame's box",
    "extensions/builtins/scidisplay/plotly_import.py": "plotly's box trace",
    "extensions/builtins/chat/view_drive.py": "a box plot",
    "extensions/builtins/scidisplay/unified_spec.py": "a box plot",
}

#: The patterns a different meaning of the word can trip (the rest always apply).
_HOMONYM_KINDS = frozenset({"word", "env"})

#: Files that still name a vendor (phase 1, 2026-10-29). Only shrinks.
DEBT: dict[str, set[str]] = {
    "box": {
        "extensions/builtins/connector/wizard.py",
        "extensions/builtins/data_platform/agents/plinth/connectors.py",
        "extensions/builtins/data_platform/commands/cli.py",
        "extensions/builtins/data_platform/database/backup_policy.py",
        "extensions/builtins/data_platform/database/backup_uploader.py",
        "extensions/builtins/data_platform/forward/__init__.py",
        "extensions/builtins/data_platform/forward/forwarder.py",
        "extensions/builtins/data_platform/orchestration/__init__.py",
        "extensions/builtins/data_platform/orchestration/box_run.py",
        "extensions/builtins/data_platform/partner_health.py",
        "extensions/builtins/data_platform/skills/__init__.py",
        "extensions/builtins/data_platform/skills/backup.py",
        "extensions/builtins/data_platform/skills/box_login.py",
        "extensions/builtins/data_platform/skills/connector.py",
        "extensions/builtins/data_platform/skills/forward.py",
        "extensions/builtins/data_platform/skills/reindex.py",
        "extensions/builtins/data_platform/skills/verify.py",
        "extensions/builtins/data_platform/sources/__init__.py",
        "extensions/builtins/data_platform/sources/box/__init__.py",
        "extensions/builtins/data_platform/sources/box/api.py",
        "extensions/builtins/data_platform/sources/box/ccg_auth.py",
        "extensions/builtins/data_platform/sources/box/jwt_auth.py",
        "extensions/builtins/data_platform/sources/box/oauth_auth.py",
        "extensions/builtins/data_platform/sources/box/provider.py",
        "extensions/builtins/data_platform/sources/box/session_api.py",
        "extensions/builtins/data_platform/sources/box/setup.py",
        "extensions/builtins/data_platform/sources/box/source.py",
        "extensions/builtins/data_platform/sources/contracts.py",
        "extensions/builtins/publishing/cli.py",
        "extensions/builtins/publishing/providers/box.py",
        "extensions/builtins/publishing/providers/storage/box_browser.py",
        "extensions/builtins/secrets/foreign/rotation_providers.py",
        "extensions/builtins/signals/extractors/docflow_providers/__init__.py",
        "extensions/builtins/status/function_status.py",
        "infra/brand_migration.py",
        "infra/publication_registry.py",
        "infra/ratelimit.py",
    },
}


def _shipped() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if "/tests/" in f"/{rel}" or p.name.startswith("test_") or p.name == "conftest.py":
            continue
        out.append(p)
    return out


def _offences(vendor: str) -> dict[str, list[str]]:
    home = PROVIDERS / vendor
    pats = _patterns(vendor)
    found: dict[str, list[str]] = {}
    for p in _shipped():
        if home in p.parents:
            continue
        rel = p.relative_to(ROOT).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        hits = []
        for kind, pat in pats.items():
            if kind in _HOMONYM_KINDS and rel in NOT_THE_VENDOR:
                continue
            m = pat.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{kind} {m.group(0)!r} at line {line}")
        if hits:
            found[rel] = hits
    return found


def test_no_new_file_names_a_storage_vendor():
    for vendor in VENDORS:
        new = {f: h for f, h in _offences(vendor).items() if f not in DEBT.get(vendor, set())}
        assert not new, (
            f"these files name the storage vendor {vendor!r} outside its provider package "
            f"(connector/storage/{vendor}/). Reach it through the storage registry by a "
            f"connector's kind (ADR-220):\n"
            + "\n".join(f"  {f}: {'; '.join(h)}" for f, h in sorted(new.items()))
        )


def test_the_debt_list_only_shrinks():
    for vendor in VENDORS:
        clean = sorted(DEBT.get(vendor, set()) - set(_offences(vendor)))
        assert not clean, (
            f"these files no longer name {vendor!r}; delete their lines from DEBT so the "
            f"list stays a to-do:\n  " + "\n  ".join(clean)
        )


def test_every_debt_entry_is_a_real_file():
    for vendor, files in DEBT.items():
        missing = sorted(f for f in files if not (ROOT / f).is_file())
        assert not missing, f"DEBT[{vendor!r}] names files that do not exist: {missing}"


def test_the_guard_can_fail(tmp_path, monkeypatch):
    """Negative control: a new caller that branches on the vendor is caught."""
    fake_root = tmp_path / "src" / "axiom"
    (fake_root / "extensions" / "builtins" / "connector" / "storage" / "box").mkdir(parents=True)
    (fake_root / "infra").mkdir(parents=True)
    (fake_root / "infra" / "new_caller.py").write_text('if kind == "box":\n    pass\n')
    (fake_root / "extensions" / "builtins" / "connector" / "storage" / "box" / "p.py").write_text(
        'KIND = "box"\n'
    )
    import sys

    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "ROOT", fake_root)
    monkeypatch.setattr(
        mod, "PROVIDERS", fake_root / "extensions" / "builtins" / "connector" / "storage"
    )
    assert set(_offences("box")) == {"infra/new_caller.py"}  # the provider's own file is home
