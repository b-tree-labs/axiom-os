# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.vault`` — say, once, where this machine's vault is.

Without arguments it reports how the vault settings resolve here and where each came from
(environment, the settings file, or nothing), never a value. With ``backend`` it writes the
settings file (``vault_settings``) that a program started outside a terminal reads, so an MCP
server under a desktop app finds the vault a shell finds. The token is named by its file, which
must be readable by its owner only; the token itself is never taken here.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from .. import vault_settings

    backend = str(params.get("backend") or "").strip()
    actions: list[str] = []
    if backend:
        try:
            path = vault_settings.write(
                backend=backend,
                url=str(params.get("url") or ""),
                token_file=str(params.get("token_file") or ""),
                mount=str(params.get("mount") or ""),
            )
        except vault_settings.VaultSettingsError as exc:
            return SkillResult(ok=False, errors=[str(exc)])
        actions.append(f"vault settings written to {path} (no secret in it)")
    r = vault_settings.describe()
    value = {
        "settings_file": r.settings_file,
        "backend": r.backend or "(none: the OS keychain)",
        "backend_from": r.backend_from,
        "url_from": r.url_from,
        "token_from": r.token_from,
        "token_file": r.token_file,
    }
    return SkillResult(ok=True, value=value, actions_taken=actions)


__all__ = ["run"]
