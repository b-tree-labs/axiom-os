# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kit-init``: write a working ``data/`` folder with one example per kind.

Every example runs against bundled sample bronze, so the first thing a
provider sees is the whole loop working on a fresh clone, before they change
anything. The template is real files under ``template/``, copied with the
tenant substituted, so what ships is exactly what the tests exercise.
"""

from __future__ import annotations

from pathlib import Path

from .project import KIT_DIRNAME, STATE_DIRNAME, KitError, slug, validate_tenant

TEMPLATE = Path(__file__).parent / "template"

#: Text files get the tenant substituted; anything else is copied as bytes.
_TEXT = {".toml", ".sql", ".json", ".py", ".md", ".jsonl", ".gitignore"}


def init(repo: Path | str, tenant: str, *, force: bool = False) -> list[str]:
    """Write ``<repo>/data`` from the template; return the paths written.

    Refuses a non-empty ``data/`` unless ``force``: a scaffold that overwrites
    somebody's contribution has destroyed the one thing it exists to help make.
    """
    validate_tenant(tenant)
    target = Path(repo).expanduser().resolve() / KIT_DIRNAME
    if target.exists() and any(target.iterdir()) and not force:
        raise KitError(
            f"{target} already has files in it. Pass --force to write the examples "
            f"beside them (existing files with the same name are replaced)."
        )
    written: list[str] = []
    for src in sorted(p for p in TEMPLATE.rglob("*") if p.is_file()):
        if "__pycache__" in src.parts:
            continue
        rel = src.relative_to(TEMPLATE)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in _TEXT or src.name == ".gitignore":
            text = src.read_text(encoding="utf-8")
            text = text.replace("{{tenant}}", tenant).replace("{{tenant_slug}}", slug(tenant))
            dest.write_text(text, encoding="utf-8")
        else:
            dest.write_bytes(src.read_bytes())
        written.append(str(Path(KIT_DIRNAME) / rel))
    (target / STATE_DIRNAME).mkdir(exist_ok=True)
    return written


__all__ = ["TEMPLATE", "init"]
