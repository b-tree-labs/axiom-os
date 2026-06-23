# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi install — environment-aware setup runner.

Usage:
    axi install                  detect environment and run pending steps
    axi install --env hpc        force a specific environment
    axi install --list           show environments and step status
    axi install --force          re-run all steps (ignore state)
    axi install --step <id>      run a single step by id
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .installer import (
    Environment,
    _load_state,
    _save_state,
    detect_environment,
    load_manifest,
    run_step,
)


def _print_status(env: Environment, state: dict) -> None:
    print(f"\n  Environment: {env.name}")
    if env.description:
        print(f"  {env.description}")
    print()
    print(f"  {'Step':<35} {'Status'}")
    print("  " + "─" * 50)
    for step in env.steps:
        done = state.get(step.id, False)
        symbol = "✓" if done else "○"
        label = step.description or step.id
        print(f"  {symbol} {label:<33} {'done' if done else 'pending'}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi install",
        description="Run environment setup steps from runtime/config/install.toml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi install                  Auto-detect environment and run pending steps
  axi install --env hpc        Run hpc environment steps
  axi install --list           Show step status without running anything
  axi install --force          Re-run all steps (ignore completion state)
  axi install --step connect-llm-hpc   Run one step by id
""",
    )
    parser.add_argument("--env", metavar="NAME", help="Override environment detection")
    parser.add_argument("--list", action="store_true", help="Show step status and exit")
    parser.add_argument("--force", action="store_true", help="Re-run all steps")
    parser.add_argument("--step", metavar="ID", help="Run a single step by id")
    return parser


def main(argv: list[str] | None = None) -> int:
    from axiom.infra.branding import get_branding

    parser = build_parser()
    args = parser.parse_args(argv)

    brand = get_branding()
    cli = brand.cli_name
    product = brand.product_name
    env_var = f"{cli.upper()}_ENV"

    envs = load_manifest()
    if not envs:
        # load_manifest auto-inits from config.example when possible;
        # reaching here means neither the customized file nor the
        # example was found on disk.
        print("\n  No install manifest is available.")
        print(
            "  The shipped install template was not found in this distribution."
        )
        print(
            f"  If you have a custom manifest, place it at "
            f"runtime/config/install.toml and re-run `{cli} install`.\n"
        )
        return 1

    env = detect_environment(envs, override=args.env or "")
    if env is None:
        if args.env:
            print(f"\n  Environment '{args.env}' not found in install.toml")
            print(f"  Available: {', '.join(e.name for e in envs)}\n")
            return 1
        print("\n  No matching environment detected.")
        print(f"  Available environments: {', '.join(e.name for e in envs)}")
        print(f"  Use --env <name> to specify one, or set {env_var}=<name>\n")
        return 1

    state = _load_state()

    if args.list:
        _print_status(env, state)
        return 0

    # Single step override
    if args.step:
        step = next((s for s in env.steps if s.id == args.step), None)
        if step is None:
            print(f"\n  Step '{args.step}' not found in environment '{env.name}'")
            ids = [s.id for s in env.steps]
            print(f"  Available: {', '.join(ids)}\n")
            return 1
        ok = run_step(step, state, force=True)
        _save_state(state)
        return 0 if ok else 1

    # Run all pending steps
    print(f"\n  {product} Install — {env.name}")
    if env.description:
        print(f"  {env.description}")
    print()

    total = len(env.steps)
    completed = sum(1 for s in env.steps if state.get(s.id))
    pending = total - completed

    if pending == 0 and not args.force:
        print(f"  ✓ All {total} steps complete. Use --force to re-run.\n")
        return 0

    print(f"  {completed}/{total} steps complete — running {pending} pending steps")

    any_failed = False
    for step in env.steps:
        ok = run_step(step, state, force=args.force)
        _save_state(state)
        if not ok and step.type != "connect":
            # connect steps can be skipped (user may not have key yet)
            any_failed = True

    print()
    done_count = sum(1 for s in env.steps if state.get(s.id))
    print(f"  {done_count}/{total} steps complete")

    # Finalize: register always-on agents as OS services so they survive reboot.
    # Soft-fail: print clear remediation but do not abort install. Drift is
    # healed automatically on the next `axi`/`neut` invocation via the
    # self-heal hook in axiom_cli.
    _finalize_register_agents()

    # Finalize: drop ~/.local/bin/axi shim so federation SSH peers can locate
    # axi without filesystem-walking. Soft-fail — users can always re-run
    # `axi install-shim` manually.
    _finalize_install_shim()

    # Finalize: probe for federated LLM endpoints + prompt the user to
    # adopt one as their default. Implements the first slice of
    # spec-federation §6.6 (install-time trigger only; on-demand +
    # periodic + mDNS + DNS-SRV follow in later slices). Soft-fail —
    # any probe error logs and continues; never blocks install.
    _finalize_federation_probe()

    if done_count == total:
        print("  ✓ Installation complete\n")
    else:
        remaining = [s.id for s in env.steps if not state.get(s.id)]
        print(f"  Remaining: {', '.join(remaining)}")
        print(f"  Re-run `{cli} install` after addressing any issues\n")

    return 1 if any_failed else 0


def _finalize_register_agents() -> None:
    """Register always-on agents as OS services. Soft-fail with clear logging."""
    try:
        from axiom.extensions.builtins.agents.cli import register_all_daemon_agents
    except ImportError:
        return

    results = register_all_daemon_agents()
    if not results:
        return

    # Fully-idempotent run (every service was already in the desired state)
    # → don't print the "Agent services" block at all. Avoids the visual
    # noise of "✓ registered" lines on every `axi install` re-run (#208).
    if all(r.ok and r.unchanged for r in results):
        return

    print()
    print("  Agent services")
    print("  " + "─" * 50)
    failed: list[str] = []
    for r in results:
        if r.ok and r.unchanged:
            # Silent: nothing changed for this service.
            continue
        if r.ok:
            print(f"  ✓ {r.agent_name} — registered ({r.provider})")
        else:
            detail = r.error or "install or start failed"
            print(f"  ⚠ {r.agent_name} — {detail}")
            failed.append(r.agent_name)
    if failed:
        print()
        print("  One or more agents did not start. Install will not fail,")
        print("  but heartbeats are OFF for these agents until remedied:")
        for name in failed:
            print(f"    axi agents start {name}")
        print("  (Next interactive `axi` invocation will also retry automatically.)")


def _finalize_install_shim() -> None:
    """Drop ~/.local/bin/axi shim. Soft-fail on any error."""
    try:
        from .shim import path_contains_local_bin, resolve_current_axi, write_shim
    except ImportError:
        return
    try:
        target = resolve_current_axi()
        if target is None:
            return
        result = write_shim(target_axi=target)
        print()
        print("  Federation shim")
        print("  " + "─" * 50)
        if result.conflict:
            print(f"  ⚠ ~/.local/bin/axi -> {result.previous_target}")
            print(f"    current install lives at {target}")
            print("    run `axi install-shim --force` to repoint")
        elif result.written:
            print(f"  ✓ wrote shim: {result.path} -> {target}")
        else:
            print(f"  ✓ shim up to date: {result.path}")
        if not path_contains_local_bin(result.path.parent):
            print(f"    note: add {result.path.parent} to PATH")
    except Exception as exc:  # noqa: BLE001 — soft-fail finalizer
        print(f"  ⚠ install-shim skipped: {exc}")


def _finalize_federation_probe() -> None:
    """Run the install-time federation probe. Soft-fail on any error."""
    try:
        from axiom.setup.federation_probe import run_install_probe
    except ImportError:
        return
    try:
        adopted = run_install_probe()
        if adopted:
            print(f"  ✓ federation probe adopted {adopted} provider(s)\n")
    except Exception as exc:  # noqa: BLE001 — soft-fail finalizer
        log_path = Path.home() / ".axi" / "logs" / "install.log"
        print(f"  ⚠ federation probe skipped: {exc} (see {log_path})")


if __name__ == "__main__":
    sys.exit(main())
