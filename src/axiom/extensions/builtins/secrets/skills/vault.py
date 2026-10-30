# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.where`` and ``vault.setup`` — where this machine's vault is, and starting it.

``vault.where`` was ``secrets vault``; ``vault.setup`` was ``secrets vault setup``
(ADR-218). ``run`` keeps the old single entry point, which chose between them on
``action``, for the deprecated ``secrets.vault`` alias.

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
    """The old ``secrets.vault`` entry: ``action=setup`` starts the vault, else ``where``."""
    if str(params.get("action") or "").strip() == "setup":
        return _setup(params, ctx)
    return where(params, ctx)


def setup(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """``vault setup``: start this computer's local vault, then (optionally) point shells at it."""
    return _setup(params, ctx)


def where(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """``vault where``: how the vault settings resolve here, or write them with ``backend``."""
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


def _names(raw: Any) -> list[str]:
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [p.strip().lower() for i in items if i for p in str(i).split(",") if p.strip()]


def _setup(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """``vault setup``: start the local vault, then (optionally) point shells at it."""
    import os
    from pathlib import Path

    from axiom.infra.paths import get_platform_state_dir

    from .. import shell_profiles as sp
    from .. import vault_setup

    system = str(params.get("_system") or sp.system_name())
    home = Path(params.get("_home") or Path.home())
    try:
        res = vault_setup.setup(
            state_dir=get_platform_state_dir(),
            container=str(
                params.get("container")
                or os.environ.get("AXI_OPENBAO_CONTAINER")
                or vault_setup.DEFAULT_CONTAINER
            ),
            port=int(
                params.get("port") or os.environ.get("AXI_OPENBAO_PORT") or vault_setup.DEFAULT_PORT
            ),
            system=system,
        )
    except vault_setup.SetupError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except ValueError as exc:
        return SkillResult(ok=False, errors=[f"bad option: {exc}"])

    actions = list(res["actions"]) + list(res["notes"])
    url, token_file = res["addr"], res["token_file"]
    detected = sp.detect(home=home, system=system)
    asked = _names(params.get("shell"))
    unknown = [a for a in asked if a not in sp.SHELLS]
    if unknown:
        return SkillResult(
            ok=False,
            errors=[f"unknown shell {', '.join(unknown)}; choose from {', '.join(sp.SHELLS)}"],
        )
    if asked:
        chosen = asked
    elif params.get("all_detected"):
        chosen = detected
    else:
        chosen = detected[:1]

    interactive = bool(params.get("interactive"))
    yes = bool(params.get("yes"))
    plan: list[tuple[str, Path | None]] = []
    for shell in chosen:
        if shell == "powershell" and system != "windows":
            actions.append("Windows PowerShell 5 exists only on Windows; skipped")
            continue
        plan.append((shell, sp.profile_path(shell, home=home, system=system)))

    def lines_for(shell: str) -> list[str]:
        if shell == "cmd":
            return [f"setx {sp.ENV_BACKEND} openbao", f"setx {sp.ENV_URL} {url}"]
        return sp.render(shell, url=url, token_file=token_file).rstrip("\n").split("\n")

    go = yes
    if plan and not yes and interactive and ctx is not None and ctx.user_prompt is not None:
        names = ", ".join(f"{sp.LABELS[s]}" + (f" ({p})" if p else "") for s, p in plan)
        go = ctx.user_prompt(
            f"Let new terminals use the vault by editing: {names}? [Y/n] "
        ).strip().lower() not in ("n", "no")
    elif plan and not yes:
        actions.append(
            "Not changing any shell profile (no --yes and no terminal to ask). "
            "To use the vault in a terminal, add these lines to its start-up file:"
        )

    applied = []
    for shell, path in plan:
        if not go:
            actions.append(f"{sp.LABELS[shell]}" + (f" ({path}):" if path else ":"))
            actions += ["    " + line for line in lines_for(shell)]
            continue
        if shell == "cmd":
            actions += sp.cmd_setx(url)
            actions.append(sp.CMD_NOTE)
            continue
        assert path is not None
        done = sp.apply(shell, path, sp.render(shell, url=url, token_file=token_file))
        applied.append(done)
        said = {
            "added": "added the vault settings to",
            "updated": "updated the vault settings in",
            "unchanged": "already set up, left alone:",
            "refused": "NOT changed:",
        }[done.outcome]
        actions.append(
            f"{sp.LABELS[shell]}: {said} {path}" + (f" ({done.detail})" if done.detail else "")
        )
        if done.backup:
            actions.append(f"  previous contents saved as {done.backup}")
    if system == "windows" and "cmd" not in chosen and detected and "cmd" in detected:
        actions.append(
            "cmd.exe cannot read a start-up file: use PowerShell, or pass --shell cmd to "
            "save the address (never the token). " + "Apps and the CLI find the token "
            "through secrets.toml either way."
        )

    actions.append(
        "Shell profiles are optional: the CLI, the MCP server and apps started outside a "
        f"terminal find the vault from {res['settings_file']} and the token file it names."
    )
    value = {
        **res,
        "shells": [s for s, _ in plan],
        "detected": detected,
        "profiles_changed": [str(a.path) for a in applied if a.outcome in ("added", "updated")],
    }
    ok = not any(a.outcome == "refused" for a in applied)
    return SkillResult(
        ok=ok,
        value=value,
        actions_taken=actions,
        errors=[a.detail for a in applied if a.outcome == "refused"],
    )


__all__ = ["run", "setup", "where"]
