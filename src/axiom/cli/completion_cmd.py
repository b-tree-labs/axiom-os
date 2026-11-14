# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``completion``: turn Tab on, check it, and refresh the site values it offers.

Four verbs, each a thin wrapper over one skill (ADR-056):

- ``completion setup`` (``completion.setup``): the setup step on its own. Detects
  the shell, says what it would add, asks, adds a marked block, and verifies in a
  new shell. ``--yes`` answers for you; ``--dry-run`` only says what it would do.
- ``completion check`` (``completion.check``): the same verification, read only.
  ``doctor`` runs exactly this.
- ``completion refresh`` (``completion.refresh``): fetch this site's values now
  (roles, channels, days...) instead of waiting for Tab to do it in the background.
- ``completion script <shell>`` (``completion.script``): print the registration
  script, for anybody who manages their own start-up files.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from axiom.infra.skills import SkillResult

#: The CLI exits with what ``main()`` returns (see ``axiom_cli`` core dispatch).
EXIT_WITH_RETURN_CODE = True


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


# --------------------------------------------------------------------------- skills


def setup_skill(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """``action`` = check (default), plan, or apply. ``apply`` is the caller's decision."""
    from axiom.cli import completion_setup as cs

    env = cs.default_env(params)
    step = cs.CompletionStep()
    action = str(params.get("action") or "check")
    if action == "check":
        line = step.check(env)
    elif action == "plan":
        line = step.check(env)
        plan = None if line.green else step.plan(env)
        return SkillResult(ok=True, value={**line.as_dict(), "plan": plan})
    elif action == "apply":
        line = step.apply(env)
    else:
        return SkillResult(ok=False, errors=[f"action must be check, plan or apply, not {action!r}"])
    return SkillResult(ok=line.green, value=line.as_dict(), errors=[] if line.green else [line.status])


def check_skill(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    return setup_skill({**params, "action": "check"}, ctx)


def refresh_skill(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    from axiom.cli import completion_values as cv

    cache = cv.refresh()
    counts = {k: len(v) for k, v in sorted(cache.values.items())}
    return SkillResult(
        ok=not (cache.empty and cache.errors),
        value={"path": str(cv.cache_path()), "counts": counts, "binding": cache.binding,
               "errors": cache.errors},
        errors=list(cache.errors) if cache.empty else [],
    )


def script_skill(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    from axiom.cli import completion_setup as cs

    shell = str(params.get("shell") or "")
    if shell not in cs.SUPPORTED:
        return SkillResult(ok=False, errors=[f"no completion script for {shell!r}; one of: {', '.join(cs.SUPPORTED)}"])
    return SkillResult(ok=True, value={"shell": shell, "script": cs.script_for(shell, _cli())})


def bind(registry) -> None:
    from axiom.infra.skills import SkillSpec

    specs = [
        SkillSpec(name="completion.setup", fn=setup_skill,
                  description="Tab completion for this CLI: action=check (read only), plan, or apply "
                              "(adds a marked block to the shell's start-up file, then verifies).",
                  inputs={"action": "check | plan | apply"}, side_effects=True, idempotent=True,
                  risk="reversible-write", surfaces=("cli",)),
        SkillSpec(name="completion.check", fn=check_skill,
                  description="Does Tab complete this CLI in a new terminal? Read only; what doctor runs.",
                  inputs={}, side_effects=False, idempotent=True, risk="read",
                  surfaces=("cli", "mcp", "agent_tool")),
        SkillSpec(name="completion.refresh", fn=refresh_skill,
                  description="Fetch this site's values for Tab now (sites, roles, channels, feeds, days).",
                  inputs={}, side_effects=True, idempotent=True, risk="reversible-write",
                  surfaces=("cli",)),
        SkillSpec(name="completion.script", fn=script_skill,
                  description="The completion registration script for a shell.",
                  inputs={"shell": "zsh | bash | fish | pwsh | powershell"}, side_effects=False,
                  idempotent=True, risk="read", surfaces=("cli",)),
    ]
    for spec in specs:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=bool(spec.side_effects))


# --------------------------------------------------------------------------- CLI


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli()} completion",
                                description="Tab completion: turn it on, check it, refresh its site values.")
    sub = p.add_subparsers(dest="verb")
    s = sub.add_parser("setup", help="Turn Tab completion on for this shell (asks first), then verify it.")
    s.add_argument("--yes", action="store_true", help="answer yes")
    s.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    sub.add_parser("check", help="Does Tab complete in a new terminal? Read only (what doctor runs).")
    sub.add_parser("refresh", help="Fetch this site's values for Tab now.")
    sc = sub.add_parser("script", help="Print the registration script for a shell.")
    sc.add_argument("shell", choices=["zsh", "bash", "fish", "pwsh", "powershell"])
    return p


def _say(line: dict[str, Any]) -> None:
    mark = "✓" if line.get("ok") else ("·" if line.get("skipped") else "✗")
    print(f"  {mark} {line.get('title')}  {line.get('status')}")
    if not (line.get("ok") or line.get("skipped")) and line.get("fix"):
        print(f"    → {line['fix']}")


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(sys.argv[1:] if argv is None else argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.completion"),
                       user_prompt=input if sys.stdin.isatty() else None)
    cli = _cli()

    def call(name: str, params: dict[str, Any]) -> SkillResult:
        return invoke_capability(registry, name, params, ctx, surface=CLI_SURFACE)

    if args.verb in (None, "check"):
        res = call("completion.check", {})
        if isinstance(res.value, dict):
            _say(res.value)
        if args.verb is None:
            print(f"\nnext:\n  {cli} completion setup\n  {cli} explore")
        return res.exit_code
    if args.verb == "script":
        res = call("completion.script", {"shell": args.shell})
        if res.ok:
            sys.stdout.write(res.value["script"])
        else:
            print("\n".join(res.errors), file=sys.stderr)
        return res.exit_code
    if args.verb == "refresh":
        res = call("completion.refresh", {})
        v = res.value or {}
        counts = ", ".join(f"{n} {k}{'' if n == 1 else 's'}" for k, n in (v.get("counts") or {}).items() if n)
        print(f"Tab now knows: {counts or 'nothing yet'}")
        for err in v.get("errors") or []:
            print(f"  could not read {err}")
        return res.exit_code
    # setup
    plan = call("completion.setup", {"action": "plan"})
    value = plan.value or {}
    if value.get("ok"):
        _say(value)
        return 0
    if not value.get("plan"):
        _say(value)
        return 1
    if args.dry_run:
        print(f"  would {value['plan']}")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print(f"  not changed (no terminal to ask): run `{cli} completion setup --yes` to allow it")
            return 1
        answer = input(f"Tab completion: {value['plan']}? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            print("  not changed")
            return 1
    res = call("completion.setup", {"action": "apply"})
    if isinstance(res.value, dict):
        _say(res.value)
    if res.ok:
        print("  open a new terminal (or `exec $SHELL`) and press Tab after "
              f"`{cli} `; `{cli} explore` browses everything")
    return res.exit_code


__all__ = ["bind", "check_skill", "get_parser", "main", "refresh_skill", "script_skill", "setup_skill"]


if __name__ == "__main__":
    raise SystemExit(main())
