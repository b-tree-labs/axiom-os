#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""
axi — Axiom CLI dispatcher

Routes subcommands to their respective handlers via the extension system.
Core commands (config, ext, infra, doctor) are handled directly.
All other nouns are dispatched to builtin or user extensions.

Domain products (e.g. a consumer extension) register branding before calling main(),
so the CLI identity (name, banner, version) is driven by the active branding.

Usage:
    axi <subcommand> [args...]
    python -m axiom.axiom_cli <subcommand> [args...]

Installation:
    pip install axiom   # registers 'axi' and 'axiom' entry points
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure repo root is on sys.path when running from source checkout.
# Skip when installed as a wheel (inside site-packages).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_in_site_packages = "site-packages" in os.path.abspath(__file__)
if not _in_site_packages and REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _load_dotenv():
    """Load .env file from repo root if it exists (no external deps)."""
    env_path = os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Don't overwrite explicitly set env vars
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _check_and_prompt_update() -> None:
    """Non-blocking update nudge.

    Prints a single-line banner if a newer version is available and returns
    immediately. The user runs `<cli> update` when they choose to. This is
    the MOTD/Homebrew/apt pattern — universally accepted by IT orgs because
    it respects the user's focus and never interrupts ongoing work.

    Only runs in interactive TTY sessions. 1-hour cache via VersionChecker.
    Disable entirely with AXIOM_DISABLE_UPDATE_NUDGE=1.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if os.environ.get("AXIOM_DISABLE_UPDATE_NUDGE") == "1":
        return
    try:
        from axiom.extensions.builtins.update.version_check import VersionChecker

        checker = VersionChecker()
        info = checker.check_remote_version(timeout=3.0)
        if not info.is_newer:
            return

        current = info.current
        available = info.available or "latest"
        from axiom.infra.branding import get_branding as _gb_upd

        _cli = _gb_upd().cli_name
        print(
            f"\n  ↑ {_cli} update available ({current} → {available}). "
            f"Run `{_cli} update --check` for details.\n"
        )
    except Exception:
        pass  # Never block the CLI for an update check


def _self_heal_daemon_agents() -> None:
    """Opportunistic re-registration of missing always-on agents.

    Runs on CLI startup in interactive sessions, throttled to once per hour
    via a state file. This is the floor below Tidy's own drift detector: it
    re-registers the health agent itself when it is the thing that went
    missing (the scenario where no service-registration ever happened, or
    where the host was rebooted without user-service linger enabled).

    Never raises; never blocks the CLI.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if os.environ.get("AXIOM_DISABLE_SELF_HEAL") == "1":
        return
    try:
        import time

        from axiom.infra.paths import get_user_state_dir

        marker = get_user_state_dir() / "self_heal_agents.last"
        now = time.time()
        try:
            if marker.exists() and (now - marker.stat().st_mtime) < 3600:
                return
        except OSError:
            pass

        from axiom.extensions.builtins.agents.cli import (
            missing_daemon_agents,
            register_all_daemon_agents,
            run_interactive_registration,
        )

        missing = missing_daemon_agents()
        if not missing:
            # Touch marker to avoid re-checking for the hour.
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError:
                pass
            return

        # Host-persistent service registration (schtasks/systemd/launchd) is a
        # consequential, long-running, host-modifying action — NEVER install it
        # without explicit operator consent (the 2026-05-28 silent-install
        # incident). Consult the recorded decision; prompt only when undecided
        # (or to gently re-offer after an upgrade if they once opted out).
        from axiom.extensions.builtins.agents.consent import (
            current_version,
            load_consent,
            needs_prompt,
            record_decision,
            should_reoffer_after_optout,
        )

        try:
            from axiom.infra.branding import get_branding as _gb

            _brand = _gb()
            _cli = (_brand.cli_name or "axi").strip()
            _product = _brand.product_name or "Axiom"
        except Exception:
            _cli, _product = "axi", "Axiom"

        cur_ver = current_version()
        consent = load_consent()
        reoffer = should_reoffer_after_optout(consent, cur_ver)

        if consent.decided and not consent.opted_out:
            # Previously approved: silently re-heal so a reboot/eviction of an
            # already-consented service self-repairs.
            register_all_daemon_agents()
        elif needs_prompt(consent, missing) or reoffer:
            if reoffer:
                print(
                    f"\n  You previously opted out of background agents; "
                    f"{_product} has upgraded to {cur_ver} since."
                )
            else:
                print(
                    f"\n  {len(missing)} agent(s) aren't registered as background "
                    f"services yet: {', '.join(missing)}"
                )
            print(
                "  Registering installs an OS task so their heartbeats survive "
                "reboots — this modifies your host."
            )
            print(f"  See `{_cli} agents info` for what each one does.")
            try:
                ans = input(
                    "  Set them up now? [y]es (choose which) / "
                    "[N]o (don't ask again) / [l]ater: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans in ("y", "yes"):
                # Route into the informed picker — never a blind enable-all.
                run_interactive_registration()
            elif ans in ("l", "later"):
                if reoffer:
                    # Stamp the current version so we don't re-nag until the
                    # next upgrade; they stay opted out for now.
                    record_decision(enabled=[], opted_out=True)
                print(f"  OK — run `{_cli} agents register` when you're ready.")
            else:
                record_decision(enabled=[], opted_out=True)
                print(
                    f"  Won't ask again. Enable later with `{_cli} agents register`."
                )
            print()
        # else: opted out and no upgrade to re-offer on — stay quiet.

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass
    except Exception:
        pass  # Never block the CLI for self-heal


def _do_self_update(old_version: str) -> None:
    """Perform the actual self-update and stash a changelog for next launch."""
    import subprocess

    from axiom.infra.branding import get_branding as _gb_su
    from axiom.infra.paths import get_user_state_dir

    _b = _gb_su()
    update_repo_url = _b.update_repo_url
    if not update_repo_url:
        print("  Self-update is not configured for this product.")
        return

    venv_pip = get_user_state_dir() / "venv" / "bin" / "pip"

    # Prefer the venv pip (end-user install); fall back to current interpreter's pip
    pip_cmd = str(venv_pip) if venv_pip.exists() else f"{sys.executable} -m pip"

    print(f"  Updating {_b.cli_name}...")
    result = subprocess.run(
        [*pip_cmd.split(), "install", "--upgrade", f"git+{update_repo_url}"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"  Update failed:\n{result.stderr.strip()}")
        return

    # Stash changelog so it shows on next launch
    try:
        from importlib.metadata import version as pkg_version

        from axiom.extensions.builtins.update.cli import Updater
        from axiom.infra.branding import get_branding

        new_version = pkg_version(get_branding().package_name)
        updater = Updater()
        updater._stash_changelog(old_version, new_version, [])
    except Exception:
        pass

    from axiom.infra.branding import get_branding as _gb_done

    print(f"  Done. Restart {_gb_done().cli_name} to use the new version.\n")


def _show_pending_changelog() -> None:
    """Display pending changelog from a recent update, then clear it."""
    try:
        from axiom.extensions.builtins.update.version_check import (
            clear_pending_changelog,
            read_pending_changelog,
        )

        changelog = read_pending_changelog()
        if not changelog or changelog.get("shown"):
            return

        old_v = changelog.get("old_version", "?")
        new_v = changelog.get("new_version", "?")
        categories = changelog.get("categories", {})
        count = changelog.get("commit_count", 0)

        print(f"\n  Updated {old_v} \u2192 {new_v} ({count} commits)")
        print("  " + "\u2500" * 38)

        _LABELS = {
            "features": "New",
            "fixes": "Fixed",
            "improvements": "Improved",
            "other": "Other",
        }
        for key, label in _LABELS.items():
            items = categories.get(key, [])
            if items:
                print(f"  {label}:")
                for item in items[:5]:
                    print(f"    - {item}")
                if len(items) > 5:
                    print(f"    ... and {len(items) - 5} more")

        print()
        clear_pending_changelog()
    except Exception:
        pass  # Never crash the CLI for changelog display


# Core commands — platform infrastructure that is NOT an extension.
# Everything else is discovered via the extension system (builtins + user).
SUBCOMMANDS = {
    "config": "axiom.setup.cli",
    "setup": "axiom.setup.cli",  # Alias — quickstart guide says `neut setup`
    "ext": "axiom.extensions.cli",
    "infra": "axiom.setup.infra",
    "plan": "axiom.cli.plan",  # Plan I/O (analysis §10.1)
    # `connect` adds the preset-based wiring framework on top of the legacy
    # connection-credential setup; the preset module dispatches to the legacy
    # extension main() for any args it doesn't recognize as preset commands.
    "connect": "axiom.cli.connect",
    "doctor": None,  # Built-in, handled specially
    "dr": None,  # Shorthand alias for doctor
    "role": "axiom.cli.role",  # Manage user role membership (drives `axi help` filtering)
    "tasks": "axiom.infra.tasks.cli",  # Persistent, federation-aware background tasks
    "schedule": "axiom.cli.schedule",  # Per-host cron primitives (issue #203)
    "skills": "axiom.cli.skills",  # SkillRegistry surface (ADR-063)
}

# Capability requirements for core commands (ADR-047). Extension commands
# declare theirs in the AEOS manifest (`requires = [...]`); this map is the
# equivalent for the built-in nouns above. Empty = no external dependencies.
_SUBCOMMAND_REQUIRES: dict[str, list[str]] = {}


def _merge_extension_commands() -> dict[str, dict]:
    """Discover CLI commands from all extensions (builtin + user).

    Returns dict mapping noun -> {module, description, extension, root, builtin}.
    Core SUBCOMMANDS take precedence over extension commands.
    """
    try:
        from axiom.extensions.discovery import discover_cli_commands

        ext_cmds = discover_cli_commands()
        return {
            noun: info
            for noun, info in ext_cmds.items()
            if noun not in SUBCOMMANDS  # Core commands take precedence
        }
    except Exception:
        return {}


def cmd_doctor(error_context: str | None = None, auto_fix: bool = False):
    """Diagnose environment issues using RAG+LLM for intelligent fixes.

    When `auto_fix=True`, also attempts to remediate the issues that can
    be safely auto-fixed (agent services not running, etc.). Issues that
    inherently require user shell action (e.g., activating a venv) are
    flagged with the platform-correct command to run.
    """

    # Resolve the active CLI brand once; fall back to "axi" if branding
    # isn't registered (which would only happen in a totally fresh axi
    # install where nothing has called branding.register yet).
    try:
        from axiom.infra.branding import get_branding

        _brand = get_branding()
    except Exception:
        _brand = None
    _cli = (_brand.cli_name if _brand else "axi") or "axi"
    _pkg = (_brand.package_name if _brand else "axiom-os-lm") or "axiom-os-lm"

    print(f"🩺 {_cli} dr — AI-Powered Diagnostics")
    print("=" * 50)

    diagnostics = _gather_diagnostics()

    # Print quick summary
    print("\n📋 Environment Summary:")
    for check in diagnostics["checks"]:
        status = "✓" if check["ok"] else "✗"
        print(f"   {status} {check['name']}: {check['status']}")

    issues = [c for c in diagnostics["checks"] if not c["ok"]]

    # If there are issues OR user provided error context, use LLM
    if issues or error_context:
        print("\n🤖 Analyzing with AI...")
        analysis = _llm_diagnose(diagnostics, error_context)
        if analysis:
            print("\n" + "=" * 50)
            print("💡 AI Analysis:")
            print(analysis)
        else:
            # Fallback to basic suggestions
            print("\n" + "=" * 50)
            if issues:
                print(f"❌ Found {len(issues)} issue(s):")
                for issue in issues:
                    print(f"   • {issue['name']}: {issue['status']}")
                    if issue.get("fix"):
                        print(f"     Fix: {issue['fix']}")
            print(f"\nRun '{_cli} config' to complete setup.")

    # --fix: actually remediate the auto-fixable issues.
    if auto_fix and issues:
        print("\n" + "=" * 50)
        print("🔧 Auto-fix: attempting safe remediations…")
        _auto_fix_issues(issues, cli=_cli)

    if not issues:
        print("\n" + "=" * 50)
        print("✅ Environment looks healthy!")

    return 1 if issues else 0


def _auto_fix_issues(issues: list[dict], cli: str) -> None:
    """Auto-run the remediable fixes; print user-action for the rest."""
    import subprocess

    for issue in issues:
        name = issue["name"]
        if name == "Agent Services":
            print(f"  ▶ starting agents ({cli} agents start)…")
            try:
                subprocess.run([cli, "agents", "start"], check=False)
            except FileNotFoundError:
                # Console-script not on PATH (the very thing doctor flagged
                # for Entry Point). Fall back to `python -m`.
                subprocess.run(
                    [sys.executable, "-m", "axiom.axiom_cli", "agents", "start"],
                    check=False,
                )
        elif name == "LLM Gateway":
            print(f"  ▶ launching config wizard ({cli} config)…")
            try:
                subprocess.run([cli, "config"], check=False)
            except FileNotFoundError:
                subprocess.run(
                    [sys.executable, "-m", "axiom.axiom_cli", "config"],
                    check=False,
                )
        elif name == "Virtual Environment":
            # Can't activate the parent shell's venv from inside Python.
            # Re-print the platform-correct command so the user can paste it.
            print(f"  ⏭ Virtual Environment: please run manually — {issue['fix']}")
        elif name == "Entry Point":
            # Re-running pip install is safe but requires user confirmation
            # since it modifies their site-packages. Don't auto-do it.
            print(f"  ⏭ Entry Point: please run manually — {issue['fix']}")
        elif name == "Package":
            print(f"  ⏭ Package: please run manually — {issue['fix']}")
        # Python version and other unforeseen issues: skip, just leave the
        # fix string in the output above.


def _gather_diagnostics() -> dict:
    """Gather all environment diagnostics into a structured dict."""
    import shutil
    import subprocess
    from pathlib import Path

    # Resolve brand-aware fix-command names so we don't leak "axi" /
    # "axiom" through to consumer-layer (e.g., neut) users.
    try:
        from axiom.infra.branding import get_branding

        _b = get_branding()
        _cli = (_b.cli_name or "axi").strip()
        _pkg = (_b.package_name or "axiom-os-lm").strip()
    except Exception:
        _cli = "axi"
        _pkg = "axiom-os-lm"

    # Platform-aware venv-activate suggestion. We can't activate the
    # user's parent shell from inside Python, so the best we can do is
    # emit a paste-able one-liner that works on their platform.
    if sys.platform == "win32":
        # PowerShell is the modern Windows default; the script also works
        # in cmd.exe via the .bat variant. We pick PowerShell here.
        _venv_fix = "python -m venv .venv ; .venv\\Scripts\\Activate.ps1"
    else:
        _venv_fix = "python -m venv .venv && source .venv/bin/activate"

    checks = []

    # 1. Python version
    py_ok = sys.version_info >= (3, 10)
    checks.append(
        {
            "name": "Python",
            "ok": py_ok,
            "status": sys.version.split()[0],
            "fix": "Install Python 3.10+" if not py_ok else None,
        }
    )

    # 2. Virtual environment — use sys.prefix (actual running venv, not env var)
    _in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )
    venv_path = sys.prefix if _in_venv else ""
    checks.append(
        {
            "name": "Virtual Environment",
            "ok": _in_venv,
            "status": venv_path or "Not active",
            "fix": _venv_fix if not _in_venv else None,
        }
    )

    # 3. Package installation
    pkg_ok = False
    pkg_status = "Unknown"
    pkg_location = ""
    from axiom.infra.branding import get_branding as _gb

    for _pkg_name in (_gb().package_name, "axiom") if _gb().package_name != "axiom" else ("axiom",):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "show", _pkg_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                pkg_ok = True
                for line in result.stdout.split("\n"):
                    if line.startswith("Editable project location:"):
                        pkg_location = line.split(":", 1)[1].strip()
                        pkg_status = f"Editable at {pkg_location}"
                        break
                else:
                    pkg_status = f"Installed ({_pkg_name})"
                break
            else:
                pkg_status = "Not installed"
        except Exception as e:
            pkg_status = f"Check failed: {e}"

    checks.append(
        {
            "name": "Package",
            "ok": pkg_ok,
            "status": pkg_status,
            "location": pkg_location,
            "fix": "pip install -e ." if not pkg_ok else None,
        }
    )

    # 4. Entry point — check current interpreter's venv bin dir first,
    # then fall back to PATH. This avoids picking up a stale entry point
    # from a parent workspace when running inside a fresh install venv.
    _venv_bin = Path(sys.executable).parent
    from axiom.infra.branding import get_branding as _gb2

    _cli = _gb2().cli_name
    _venv_cli = _venv_bin / _cli
    _venv_axiom = _venv_bin / "axiom"
    neut_script = (
        str(_venv_cli)
        if _venv_cli.exists()
        else str(_venv_axiom)
        if _venv_axiom.exists()
        else shutil.which(_cli) or shutil.which("axiom")
    )
    entry_ok = False
    entry_status = "Not found"
    entry_content = ""
    entry_type = None
    if neut_script:
        try:
            # Explicit UTF-8: the default file encoding on Windows is
            # cp1252 ("charmap"), which raises `UnicodeDecodeError:
            # 'charmap' codec can't decode byte 0x90` when reading
            # pip-generated entry-point scripts that contain non-ASCII
            # bytes in their wrapper preamble. Forcing UTF-8 (with a
            # latin-1 fallback for the wrapper-with-binary-shebang case)
            # makes this check work uniformly on Windows + Unix.
            try:
                with open(neut_script, encoding="utf-8") as f:
                    entry_content = f.read()
            except UnicodeDecodeError:
                with open(neut_script, encoding="latin-1") as f:
                    entry_content = f.read()
            # Check for pip-generated Python entry point
            if (
                "from axiom.axiom_cli import main" in entry_content
                or "axiom.axiom_cli" in entry_content
            ):
                entry_ok = True
                entry_type = "pip"
                entry_status = f"Valid (pip) at {neut_script}"
            # Check for our self-healing shell wrapper
            elif (
                "python -m tools.neut_cli" in entry_content or "-m tools.neut_cli" in entry_content
            ):
                entry_ok = True
                entry_type = "shell"
                entry_status = f"Valid (shell wrapper) at {neut_script}"
            else:
                entry_status = f"Stale at {neut_script}"
        except Exception as e:
            entry_status = f"Cannot read: {e}"

    checks.append(
        {
            "name": "Entry Point",
            "ok": entry_ok,
            "status": entry_status,
            "type": entry_type,
            "content": entry_content[:500] if entry_content else "",
            "fix": f"pip install '{_pkg}[runtime]'" if not entry_ok else None,
        }
    )

    # 5. Gateway/LLM availability
    gateway_ok = False
    gateway_status = "Not configured"
    try:
        from axiom.infra.gateway import Gateway

        gw = Gateway()
        if gw.available:
            gateway_ok = True
            provider = gw.active_provider
            gateway_status = f"{provider.name} ({provider.model})" if provider else "Available"
        else:
            gateway_status = "No providers configured"
    except ImportError:
        gateway_status = "Gateway module not found"
    except Exception as e:
        gateway_status = f"Error: {e}"

    checks.append(
        {
            "name": "LLM Gateway",
            "ok": gateway_ok,
            "status": gateway_status,
            "fix": f"{_cli} config --set anthropic_api_key" if not gateway_ok else None,
        }
    )

    # 6. Agent services
    try:
        from axiom.extensions.builtins.agents.cli import (
            _discover_agent_extensions,
            _get_service_status,
        )

        agent_exts = _discover_agent_extensions()
        daemon_agents = [e for e in agent_exts if e.agent and e.agent.is_always_on]
        if daemon_agents:
            running = sum(1 for e in daemon_agents if _get_service_status(e) == "running")
            agents_ok = running == len(daemon_agents)
            agent_names = ", ".join(
                f"{e.name}({'running' if _get_service_status(e) == 'running' else 'stopped'})"
                for e in daemon_agents
            )
            checks.append(
                {
                    "name": "Agent Services",
                    "ok": agents_ok,
                    "status": f"{running}/{len(daemon_agents)} running: {agent_names}",
                    "fix": f"{_cli} agents start" if not agents_ok else None,
                }
            )
    except Exception:
        pass  # Agents extension may not be available yet

    # 7. Working directory
    cwd = os.getcwd()
    # A consumer layer self-identifies its working dir via this env var or a
    # marker file at its repo root; the platform stays domain-agnostic.
    in_consumer_repo = bool(os.environ.get("AXIOM_CONSUMER_REPO")) or Path(
        cwd, "axiom-consumer.toml"
    ).exists()
    checks.append(
        {
            "name": "Working Directory",
            "ok": True,  # Not critical
            "status": cwd,
            "in_consumer_repo": in_consumer_repo,
        }
    )

    return {
        "checks": checks,
        "python_version": sys.version,
        "platform": sys.platform,
        "cwd": cwd,
    }


def _llm_diagnose(diagnostics: dict, error_context: str | None = None) -> str | None:
    """Use LLM with project context to diagnose issues intelligently."""
    try:
        from pathlib import Path

        from axiom.infra.gateway import Gateway

        gateway = Gateway()
        if not gateway.available:
            return None

        # Load CLAUDE.md for project context
        claude_md = Path(REPO_ROOT) / "CLAUDE.md"
        project_context = ""
        if claude_md.exists():
            try:
                content = claude_md.read_text()
                # Extract troubleshooting section
                if "## Troubleshooting" in content:
                    start = content.index("## Troubleshooting")
                    end = content.find("\n## ", start + 1)
                    project_context = content[start:end] if end > 0 else content[start:]
                else:
                    # Take first 2000 chars as context
                    project_context = content[:2000]
            except Exception:
                pass

        # Build diagnostic summary
        diag_text = "Environment Diagnostics:\n"
        for check in diagnostics["checks"]:
            status = "OK" if check["ok"] else "ISSUE"
            diag_text += f"- {check['name']}: {status} - {check['status']}\n"
            if check.get("content"):
                diag_text += f"  Entry point content: {check['content'][:200]}...\n"

        if error_context:
            diag_text += f"\nUser-reported error:\n{error_context}\n"

        prompt = f"""You are a diagnostic assistant for Axiom, a Python-based operations platform.

PROJECT CONTEXT (from CLAUDE.md):
{project_context}

{diag_text}

Provide a diagnosis in PLAIN TEXT only (no markdown, no **, no ```, no code fences):

DIAGNOSIS: [one line explaining the problem]

FIX: [numbered steps with exact commands]

WHY: [brief explanation of root cause]

Rules:
- Be concise (under 200 words)
- Use exact paths from diagnostics
- Commands should be copy-pasteable
- Plain text only — no markdown formatting"""

        response = gateway.complete(prompt)
        return response.text if hasattr(response, "text") else str(response)

    except Exception:
        # Silently fall back to basic mode
        return None


# Help text for core subcommands (extensions provide their own descriptions)
_SUBCOMMAND_HELP = {
    "config": "Interactive onboarding wizard",
    "setup": "Interactive onboarding wizard (alias for config)",
    "ext": "Manage extensions (builtin + user)",
    "plan": "Plan I/O — create, show, edit, import, approve plans",
    "connect": "Wire LLM + RAG endpoints from a preset (or manage connections)",
    "doctor": "AI-powered environment diagnostics",
    "dr": "AI-powered environment diagnostics (alias for doctor)",
}


def _copy_subparsers(
    src_parser: argparse.ArgumentParser,
    dst_parser: argparse.ArgumentParser,
) -> None:
    """Copy subparser definitions from *src_parser* into *dst_parser*.

    This lets argcomplete see the full completion tree (e.g. ``neut signal
    ingest``) without duplicating parser definitions.
    """
    for action in src_parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            dst_sub = dst_parser.add_subparsers(dest=action.dest)
            for name, sub in action.choices.items():
                # Determine help text from _choices_actions if available
                help_text = sub.description or ""
                for choice_action in action._choices_actions:
                    if choice_action.dest == name:
                        help_text = choice_action.help or help_text
                        break
                new_sub = dst_sub.add_parser(name, help=help_text, description=sub.description)
                # Copy arguments (flags) from the child parser
                for sub_action in sub._actions:
                    if isinstance(sub_action, (argparse._HelpAction, argparse._SubParsersAction)):
                        continue
                    # Reconstruct the add_argument call from the action
                    kwargs = {}
                    if sub_action.option_strings:
                        names = sub_action.option_strings
                    else:
                        names = [sub_action.dest]
                    if sub_action.help:
                        kwargs["help"] = sub_action.help
                    if sub_action.choices:
                        kwargs["choices"] = sub_action.choices
                    if sub_action.metavar:
                        kwargs["metavar"] = sub_action.metavar
                    if isinstance(sub_action, argparse._StoreTrueAction):
                        kwargs["action"] = "store_true"
                    elif isinstance(sub_action, argparse._StoreFalseAction):
                        kwargs["action"] = "store_false"
                    elif isinstance(sub_action, argparse._CountAction):
                        kwargs["action"] = "count"
                    elif sub_action.nargs is not None:
                        kwargs["nargs"] = sub_action.nargs
                    try:
                        new_sub.add_argument(*names, **kwargs)
                    except Exception:
                        pass  # Skip arguments that can't be copied cleanly
            break  # Only one _SubParsersAction expected


def _copy_top_level_args(
    src_parser: argparse.ArgumentParser,
    dst_parser: argparse.ArgumentParser,
) -> None:
    """Copy top-level arguments (flags like --resume, --model) from *src* to *dst*."""
    for action in src_parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        kwargs = {}
        if action.option_strings:
            names = action.option_strings
        else:
            names = [action.dest]
        if action.help:
            kwargs["help"] = action.help
        if action.choices:
            kwargs["choices"] = action.choices
        if action.metavar:
            kwargs["metavar"] = action.metavar
        if action.option_strings:
            # Only set dest explicitly if it differs from the auto-derived name
            auto_dest = action.option_strings[0].lstrip("-").replace("-", "_")
            if action.dest and action.dest != auto_dest:
                kwargs["dest"] = action.dest
        if isinstance(action, argparse._StoreTrueAction):
            kwargs["action"] = "store_true"
        elif isinstance(action, argparse._StoreFalseAction):
            kwargs["action"] = "store_false"
        elif isinstance(action, argparse._CountAction):
            kwargs["action"] = "count"
        elif action.nargs is not None:
            kwargs["nargs"] = action.nargs
        try:
            new_action = dst_parser.add_argument(*names, **kwargs)
            # Carry over argcomplete completers
            if hasattr(action, "completer") and action.completer is not None:  # type: ignore[union-attr]
                new_action.completer = action.completer  # type: ignore[union-attr,attr-defined]
        except Exception:
            pass


def get_parser() -> argparse.ArgumentParser:
    """Build top-level parser for argcomplete and help generation.

    This mirrors SUBCOMMANDS + discovered extension commands with real argparse
    subparsers so that argcomplete can provide tab completion.  The actual
    command dispatch still uses importlib — argparse is used only for
    completion and ``--help``.
    """
    import importlib

    try:
        from axiom.infra.branding import get_branding as _gb2

        _cli2 = _gb2().cli_name
    except Exception:
        _cli2 = "axi"
    parser = argparse.ArgumentParser(
        prog=_cli2,
        description=f"{_cli2} CLI",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    seen = set()

    # Core commands
    for name, module_path in SUBCOMMANDS.items():
        if name in seen:
            continue
        seen.add(name)

        if module_path is None:
            subparsers.add_parser(name, help=_SUBCOMMAND_HELP.get(name, ""))
            continue

        try:
            mod = importlib.import_module(module_path)
            _get = getattr(mod, "get_parser", None) or getattr(mod, "build_parser", None)
            if _get:
                child_parser = _get()
                sub = subparsers.add_parser(
                    name,
                    help=child_parser.description or _SUBCOMMAND_HELP.get(name, ""),
                    description=child_parser.description,
                )
                _copy_subparsers(child_parser, sub)
                _copy_top_level_args(child_parser, sub)
            else:
                subparsers.add_parser(name, help=_SUBCOMMAND_HELP.get(name, ""))
        except ImportError:
            subparsers.add_parser(name, help=_SUBCOMMAND_HELP.get(name, ""))

    # Extension commands (builtin + user)
    ext_cmds = _merge_extension_commands()
    _show_unavail = bool(os.environ.get("AXI_SHOW_UNAVAILABLE"))
    from axiom.infra import cli_gating as _gating
    for name, info in ext_cmds.items():
        if name in seen:
            continue
        # Availability gate (ADR-047): hide commands whose declared
        # capabilities are unmet, unless the operator asks to see them.
        if not _show_unavail and not _gating.is_available(info.get("requires", [])):
            continue
        seen.add(name)

        module_path = info["module"]
        description = info.get("description", "")

        if info.get("builtin"):
            # Builtin: importable module, try to get parser for tab completion
            try:
                mod = importlib.import_module(module_path)
                _get = getattr(mod, "get_parser", None) or getattr(mod, "build_parser", None)
                if _get:
                    child_parser = _get()
                    sub = subparsers.add_parser(
                        name,
                        help=child_parser.description or description,
                        description=child_parser.description,
                    )
                    _copy_subparsers(child_parser, sub)
                    _copy_top_level_args(child_parser, sub)
                else:
                    subparsers.add_parser(name, help=description)
            except ImportError:
                subparsers.add_parser(name, help=description)
        else:
            # User extension: just add stub parser with description
            subparsers.add_parser(name, help=description)

    return parser


_INTENT_HEADINGS = {
    "start": "Start",
    "research": "Research",
    "teach": "Teach",
    "learn": "Learn",
    "operate": "Operate",
    "build": "Build",
    "maintain": "Maintain",
    "govern": "Govern",
    "investigate": "Investigate",
}


def _should_animate_banner() -> bool:
    """Decide whether to play the AXI wake-up animation.

    Animate only when:
      * stdout is a real terminal (not piped, not redirected)
      * `AXI_NO_ANIMATE` is unset (escape hatch for users who hate motion)
      * Either `AXI_ANIMATE=1` is set, OR the first-run sentinel is missing.

    The first-run sentinel (`~/.axi/.welcome-shown`) is written after the
    first successful animation so future invocations skip straight to the
    static banner — no 700ms tax on repeat use.
    """
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        return False
    if os.environ.get("AXI_NO_ANIMATE"):
        return False
    if os.environ.get("AXI_ANIMATE"):
        return True
    try:
        from axiom.infra.paths import get_user_state_dir
        sentinel = get_user_state_dir() / ".welcome-shown"
    except Exception:
        return False
    return not sentinel.exists()


def _mark_welcome_shown() -> None:
    """Write the first-run sentinel so future invocations skip animation."""
    try:
        from axiom.infra.paths import get_user_state_dir
        sentinel = get_user_state_dir() / ".welcome-shown"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except Exception:
        pass


def _print_welcome_banner(*, cli: str, product: str) -> None:
    """Render the platform's mascot inside a framed panel — the
    first-touch banner.

    Visual story: the mascot character (eyes + body + treads) sits inside
    a framed panel titled with the product name.  The frame is the
    platform; the mascot is the agent who lives in it.

    Adapts to the binary the user invoked: typing ``axi`` surfaces the
    agent first; typing ``axiom`` surfaces the platform first.

    Robot art is descended from a consumer layer's ``_NEUT_ART``; bringing it
    into core so domain distributions become thin branding overrides.

    On first run (or with `AXI_ANIMATE=1`), plays a brief wake-up
    animation: eyes power on, hull saturates, welcome materializes.
    Subsequent runs are static.

    Falls back to a plain print when rich isn't importable (partial
    install, broken env) — first-touch must never crash.
    """
    try:
        from importlib.metadata import version as _pkg_version

        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except Exception:
        print(f"  [◉‿◉]  {cli} — {product}")
        print(f"         Hi, I'm AXI. Try '{cli} chat' to talk.")
        print()
        return

    try:
        ver = _pkg_version("axiom-os-lm")
    except Exception:
        ver = ""

    # Mascot anatomy:
    #   1. Two SEPARATE binocular eye-housings (cylinders, not dots in
    #      a single face — that's what makes the silhouette read as a
    #      character rather than a generic robot face).
    #   2. A yoke connecting the eyes to the body (the neck stalk, so
    #      the head tilt reads).
    #   3. A cube body with a hull-stencil label.
    #   4. Treads at the base, slightly offset like a tank's tracks.
    # Earlier iterations tried `◕`, `◉‿◉`, `[●] [●]` — all read as
    # "robot face" but not specifically a character.  Two distinct eye
    # cylinders is what gives the mascot identity.
    #
    # Eye section:
    #   * Two cylindrical housings connected by a single horizontal
    #     bridge `═` at pupil level (binocular-bar between barrels).
    #   * Pupil is `◉` (BULLSEYE — ring + center dot, reads as
    #     iris-around-pupil).
    #   * SINGLE center post `║` drops from the head's center down to
    #     the body's center (`╩` socket).  One neck, not two stalks.
    #
    # Arm pose — "Z"-shape, NOT straight-down:
    #   * Hands raised toward chest level, claws pointing INWARD
    #     toward each other (`⊏` left / `⊐` right — those open inward).
    #   * Forearms angle DOWN-AND-OUTWARD from hand to elbow
    #     (`╱` on left arm, `╲` on right arm).
    #   * Elbows at the LOWEST point of the arm assembly, near the
    #     body's bottom outer corners (`●` joints).
    #   * Whole arm assembly stays inside the body silhouette.
    # Proportions per 2026-05-04 photo reference (iteration 5):
    #   * Eye assembly 11ch wide (cylinders `╭───╮` at 5ch each).
    #   * Neck `║║` is now 2 rows tall (user feedback: taller neck).
    #   * Body 19ch wide — accommodates 2-char claws + visible elbow
    #     joints + AXI label without crowding.
    #   * Claws `━⊏` / `⊐━` are 2 chars: gripper jaws + wrist
    #     extension that visually CONNECTS to the forearm slope below.
    #   * Forearms `╱` / `╲` slope from wrist (upper-inner) to elbow
    #     (lower-outer).
    #   * Elbows `└` / `┘` are corner box-drawing chars — read as
    #     articulated joints, not just dots.
    # Single-line box-drawing throughout for reliable rendering — the
    # chat-TUI mascot pane uses the same art via `setup.renderer`, so
    # the two surfaces stay visually identical.
    # Iteration 6 — vertical arm columns (hand + elbow share the
    # same column so the arm visually reads as connected) and a
    # narrower body that's less rectangular per user feedback.
    # Iteration 7 — bigger eye cylinders (7ch wide, was 5).  AXI's
    # eyes are his most expressive feature; they need real visual
    # weight.  Head now matches body width (15ch each) for cube-like
    # overall silhouette.
    # Iteration 11 — body shrunk slightly (15ch wide, was 17), one
    # fewer air row, AND the AXI label placard moves to a low
    # position just above the body bottom (used to be center-row).
    # Eyes + pupils keep their current size — body shrinks to make
    # the eyes feel proportionally more prominent.
    art_lines = [
        "   ╭────╮ ╭────╮   ",  # 0: eye top frame (rounded; contains pupil)
        "   │ (●)│─│(●) │   ",  # 1: pupil row + sides + bridge (painted)
        "   ╰────╯ ╰────╯   ",  # 2: eye bottom frame
        "         │         ",  # 3: neck row 1
        "         │         ",  # 4: neck row 2
        "   ┌─────┴─────┐   ",  # 5: body top (13ch wide)
        "   │           │   ",  # 6: top air row
        "   │ ┏━━   ━━┓ │   ",  # 7: claw tops (LARGER: 3ch × 2 rows, gray)
        "   │ ┗━━   ━━┛ │   ",  # 8: claw bottoms
        "   │ │       │ │   ",  # 9: forearm verticals
        "   │ └       ┘ │   ",  # 10: elbow corners
        "   │    AXI    │   ",  # 11: label placard (low on body)
        "   ╔═╤═╗───╔═╤═╗   ",  # 12: tread tops + body floor between tracks
        "   ╚═╧═╝   ╚═╧═╝   ",  # 13: tread bottoms (outer walls closed)
    ]

    # Adapt the welcome to which binary was invoked.  When the user typed
    # `axi`, lead with AXI; otherwise lead with the platform (cli typed
    # = `axiom`).
    axi_first = cli.lower() == "axi"

    def _axi_word() -> Text:
        """The "AXI" wordmark — three letters on a yellow placard."""
        t = Text()
        t.append("AXI", style="bold black on #FFC107")
        return t

    welcome_lines: list[Text] = []
    if axi_first:
        l1 = _axi_word()
        l1.append(".", style="white")
        welcome_lines.append(l1)
        l2 = Text()
        l2.append("Directive: ", style="italic bright_yellow")
        l2.append("assist.", style="bold bright_green")
        welcome_lines.append(l2)
    else:
        l1 = Text()
        l1.append("Welcome aboard the ", style="white")
        l1.append("Axiom", style="bold bright_white")
        l1.append(".", style="white")
        welcome_lines.append(l1)
        l2 = Text()
        l2.append("I'm ", style="white")
        l2.append_text(_axi_word())
        l2.append(". ", style="white")
        l2.append("Directive: ", style="italic bright_yellow")
        l2.append("assist.", style="bold bright_green")
        welcome_lines.append(l2)
    welcome_lines.append(Text(""))  # spacer
    chat_line = Text()
    chat_line.append("Try ", style="dim")
    chat_line.append(f"`{cli} chat`", style="bold green")
    chat_line.append(" to talk.", style="dim")
    welcome_lines.append(chat_line)
    cmd_line = Text()
    cmd_line.append("Or pick a command below.", style="dim")
    welcome_lines.append(cmd_line)

    # Vertically center the welcome text against the taller art.
    art_height = len(art_lines)
    msg_height = len(welcome_lines)
    top_pad = (art_height - msg_height) // 2
    welcome_padded: list[Text] = (
        [Text("")] * top_pad
        + welcome_lines
        + [Text("")] * max(0, art_height - msg_height - top_pad)
    )

    # Mascot palette:
    #   * Eye cylinder housings — `bright_white` (bare metal)
    #   * Eye pupils — `bold #00BCD4` (cyan-glow scanning beam)
    #   * Body — `bold #FFC107` (warm hull yellow)
    #   * Arms (forearms + claws + elbow joints) — `dim #FFC107`
    #     (weathered hull yellow, slightly darker than body)
    #   * Treads — `dim white` (worn rubber)
    art_styles = [
        "bright_white",       # 0: eye top frame (rounded)
        None,                 # 1: pupil row — painted manually
        "bright_white",       # 2: eye bottom frame
        "bright_white",       # 3: neck row 1
        "bright_white",       # 4: neck row 2
        "bold #FFC107",       # 5: body top
        "bold #FFC107",       # 6: top air row
        None,                 # 7: claw tops — painted manually
        None,                 # 8: claw bottoms — painted manually
        None,                 # 9: forearm vertical — painted manually
        None,                 # 10: elbows — painted manually
        None,                 # 11: AXI label placard — painted manually
        "bold #FFC107",       # 12: tread tops + body floor (yellow hull)
        "dim white",          # 13: tread bottoms (worn-rubber cleats)
    ]

    def _paint_eye_pupils(pupil_char: str = "●") -> Text:
        """Pupil row (middle of 3-row eye).  Side walls + pupil + bridge.
        Pupils shifted INWARD toward the bridge:
          Left  eye: `│ (●)│` (1-space pad on left → pupil right-aligned)
          Right eye: `│(●) │` (pupil left-aligned).
        The pupil is contained vertically by the rounded top frame on row 0
        and the rounded bottom frame on row 2."""
        t = Text()
        t.append("   ", style="bright_white")            # 3-space prefix
        # LEFT eye (6 wide): │ + 1 space + (●) + │
        t.append("│ ", style="bright_white")
        t.append("(", style="bright_white")
        t.append(pupil_char, style="bold #00BCD4")
        t.append(")", style="bright_white")
        t.append("│", style="bright_white")
        t.append("─", style="bold #FFC107")              # binocular bridge
        # RIGHT eye (6 wide): │ + (●) + 1 space + │
        t.append("│", style="bright_white")
        t.append("(", style="bright_white")
        t.append(pupil_char, style="bold #00BCD4")
        t.append(")", style="bright_white")
        t.append(" │", style="bright_white")
        t.append("   ", style="bright_white")            # 3-space suffix
        return t

    def _paint_hand_tops() -> Text:
        """Top row of the 2-row mechanical claw clamps.  3ch wide each,
        gray metallic — closed end on the OUTSIDE, jaws facing inward."""
        t = Text()
        t.append("   ", style="")
        t.append("│", style="bold #FFC107")
        t.append(" ", style="bold #FFC107")
        t.append("┏━━", style="bold grey70")             # left claw top (closed-left)
        t.append("   ", style="bold #FFC107")           # 3 spaces between claws
        t.append("━━┓", style="bold grey70")             # right claw top (closed-right)
        t.append(" ", style="bold #FFC107")
        t.append("│", style="bold #FFC107")
        t.append("   ", style="")
        return t

    def _paint_hand_bottoms() -> Text:
        """Bottom row of the 2-row mechanical claw clamps."""
        t = Text()
        t.append("   ", style="")
        t.append("│", style="bold #FFC107")
        t.append(" ", style="bold #FFC107")
        t.append("┗━━", style="bold grey70")             # left claw bottom
        t.append("   ", style="bold #FFC107")           # 3 spaces between claws
        t.append("━━┛", style="bold grey70")             # right claw bottom
        t.append(" ", style="bold #FFC107")
        t.append("│", style="bold #FFC107")
        t.append("   ", style="")
        return t

    def _paint_forearms_only() -> Text:
        """Vertical forearms (no label — label moved down per
        2026-05-04 user feedback)."""
        t = Text()
        t.append("   ", style="")
        t.append("│", style="bold #FFC107")
        t.append(" ", style="bold #FFC107")
        t.append("│", style="dim #FFC107")               # left forearm
        t.append("       ", style="bold #FFC107")       # 7 spaces
        t.append("│", style="dim #FFC107")               # right forearm
        t.append(" ", style="bold #FFC107")
        t.append("│", style="bold #FFC107")
        t.append("   ", style="")
        return t

    def _paint_elbows() -> Text:
        """Elbow corners directly below forearms."""
        t = Text()
        t.append("   ", style="")
        t.append("│", style="bold #FFC107")
        t.append(" ", style="bold #FFC107")
        t.append("└", style="dim #FFC107")
        t.append("       ", style="bold #FFC107")       # 7 spaces
        t.append("┘", style="dim #FFC107")
        t.append(" ", style="bold #FFC107")
        t.append("│", style="bold #FFC107")
        t.append("   ", style="")
        return t

    def _paint_label_placard() -> Text:
        """AXI hull stencil placard, low on body (just above bottom).
        11-ch body interior; 3-ch label centered (4 spaces left, 4
        spaces right — the label's geometric center lands on the
        interior's center). Black letters on yellow hull background."""
        t = Text()
        t.append("   ", style="")
        t.append("│", style="bold #FFC107")
        t.append("    ", style="bold #FFC107")           # 4 outer-left pad
        t.append("AXI", style="bold black on #FFC107")
        t.append("    ", style="bold #FFC107")           # 4 outer-right pad
        t.append("│", style="bold #FFC107")
        t.append("   ", style="")
        return t

    def _compose_body(*, pupil: str = "◉", show_welcome: bool = True) -> Text:
        """Build the full panel body for one frame.

        `pupil` controls the eye state: `◉` (BULLSEYE — ring + center
        dot, the powered state), `·` (faint dot, mid-power-up), or
        `─` (closed/asleep).
        `show_welcome` gates whether the right-column text appears yet.
        """
        # Custom painters per line index — index → painter (or None for
        # the styled raw art line).  Indices match the new 11-row art.
        painters = {
            1: lambda: _paint_eye_pupils(pupil),
            7: _paint_hand_tops,
            8: _paint_hand_bottoms,
            9: _paint_forearms_only,
            10: _paint_elbows,
            11: _paint_label_placard,
        }
        body = Text()
        for i, (art, style, msg) in enumerate(
            zip(art_lines, art_styles, welcome_padded, strict=False)
        ):
            painter = painters.get(i)
            if painter is not None:
                body.append_text(painter())
            else:
                body.append(art, style=style)
            body.append("    ")
            if show_welcome:
                body.append_text(msg)
            body.append("\n")
        if ver:
            body.append("\n")
            body.append(f"v{ver}", style="dim")
            body.append("  ·  ", style="dim")
            body.append("AXI", style="dim italic #FFC107")
            body.append(" + the agent team", style="dim italic")
        return body

    def _frame_panel(content: Text) -> Panel:
        return Panel(
            content,
            title=f"[bold bright_white on #0D2B5C] {product.upper()} [/]",
            title_align="center",
            border_style="bold #5DADE2",
            padding=(1, 2),
            expand=False,
        )

    if _should_animate_banner():
        # Wake-up sequence (~600ms total). Frames model film behaviors:
        #   F0: asleep — eyes closed, no welcome
        #   F1: powering on — left eye flickers
        #   F2: both eyes faint dots
        #   F3: full glow, no welcome yet (looking around)
        #   F4: welcome materializes, settles
        from time import sleep

        from rich.live import Live
        frames = [
            (_compose_body(pupil="─", show_welcome=False), 0.12),
            (_compose_body(pupil="·", show_welcome=False), 0.10),
            (_compose_body(pupil="●", show_welcome=False), 0.18),
            (_compose_body(pupil="●", show_welcome=True),  0.0),
        ]
        console = Console()
        with Live(_frame_panel(frames[0][0]), console=console, refresh_per_second=24) as live:
            for content, delay in frames[:-1]:
                live.update(_frame_panel(content))
                sleep(delay)
            live.update(_frame_panel(frames[-1][0]))
        _mark_welcome_shown()
    else:
        Console().print(_frame_panel(_compose_body()))


def print_usage(
    show_all: bool = False,
    *,
    tier_override: str | None = None,
    include_internal: bool = False,
    intent_group: str | None = None,
    role_override: tuple[str, ...] | None = None,
):
    """Render the top-level help.

    Surface respects the user's roles + competency tier (from
    `~/.axi/competency.json`) and each command's manifest-declared
    `intent_groups` + `tier` (per `prd-axi-cli.md §Progressive Disclosure`).

    Reveal flags widen the surface deliberately:

    - ``--all`` → every command except `internal`
    - ``--tier <starter|core|advanced|internal>`` → ceiling override
    - ``--internal`` → also include `internal` commands
    - ``--group <intent>`` → filter to a single intent group
    - ``--role <role>`` → temporarily peek at a different role's surface
    - ``AXI_HELP_FLAT=1`` → bypass filtering entirely for scripts/CI

    Undeclared commands default to intent `start` (universal) and tier
    `core` so legacy manifests surface for every role.
    """
    try:
        from axiom.infra.branding import get_branding as _gb

        _b = _gb()
        _cli = _b.cli_name
        _prod = _b.product_name
    except Exception:
        _cli, _prod = "axi", "Axiom"
    # Detect which binary the user actually invoked (axi / axiom / neut)
    # so the banner text and the Usage line reflect that name.
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else _cli
    if invoked in {"axi", "axiom", "neut"}:
        _cli = invoked
    _print_welcome_banner(cli=_cli, product=_prod)
    print(f"Usage: {_cli} <command> [args...]")
    print()

    ext_cmds = _merge_extension_commands()

    # Role + intent + tier filtering. Falls back gracefully when
    # help_engine is unavailable (partial install).
    competency = None
    try:
        from axiom.cli.help_engine import (
            filter_commands,
            group_by_intent,
            is_quiet,
            load_competency,
        )

        competency = load_competency()
        flat = is_quiet() or show_all
        ext_cmds = filter_commands(
            ext_cmds,
            user_competency=competency,
            role_override=role_override,
            tier_override=tier_override,
            intent_group=intent_group,
            show_all=flat,
            include_internal=include_internal,
        )
    except Exception:
        # No-op on engine failure — fall through to legacy behavior.
        group_by_intent = None  # type: ignore[assignment]

    # Categorise builtin vs user extensions
    builtins = {n: i for n, i in ext_cmds.items() if i.get("builtin")}
    user_exts = {n: i for n, i in ext_cmds.items() if not i.get("builtin")}

    # Always-visible bootstrapping verbs — config + dr + ext + role are
    # how the user finds their way around regardless of competency.
    print("Commands:")
    print("  config    Interactive onboarding wizard")
    print("  dr        Diagnose environment issues")
    print("  ext       Manage extensions (builtin + user)")
    print("  role      Manage your role membership")

    # Group remaining builtins by intent for readability — readers scan
    # by activity ("Research", "Build") faster than by alphabetised noun.
    # Iteration order: the user's activated intents first (so a researcher
    # sees signal under "Investigate", not "Maintain"), then any intents
    # the user *isn't* activating but has access to via reveal flags,
    # then unclassified commands under "Other".
    skip = {"config", "dr", "ext", "role"}
    remaining = {n: i for n, i in builtins.items() if n not in skip}
    if remaining and group_by_intent is not None:
        groups = group_by_intent(remaining)
        # Active intents first (user's roles), in canonical order.
        active = competency.expand_intents() if competency else frozenset()
        canonical_order = ("start", "research", "teach", "learn", "operate",
                           "build", "maintain", "govern", "investigate")
        active_first = [i for i in canonical_order if i in active]
        rest = [i for i in canonical_order if i not in active]
        seen: set[str] = set()
        for intent in active_first + rest:
            nouns = [n for n in groups.get(intent, []) if n not in seen]
            if not nouns:
                continue
            print()
            print(f"{_INTENT_HEADINGS[intent]}:")
            for noun in sorted(nouns):
                print(f"  {noun:<12}{remaining[noun]['description']}")
                seen.add(noun)
        # Unclassified — universal fallback commands (no intent_groups
        # declared).  Render under "Other:" rather than miscategorising.
        leftovers = sorted(set(remaining) - seen)
        if leftovers:
            print()
            print("Other:")
            for noun in leftovers:
                print(f"  {noun:<12}{remaining[noun]['description']}")
    elif remaining:
        # Engine unavailable: flat alphabetised fallback.
        print()
        print("Domain Commands:")
        for noun, info in sorted(remaining.items()):
            print(f"  {noun:<12}{info['description']}")

    if user_exts:
        print()
        print("User Extensions:")
        for noun, info in sorted(user_exts.items()):
            print(f"  {noun:<12}{info['description']}")

    if competency and not (
        show_all or tier_override or include_internal or intent_group or role_override
    ):
        # Bottom-of-help reveal hint — only when we're actually filtering.
        roles_str = ", ".join(competency.roles)
        print()
        print(
            f"  Showing roles: {roles_str} · tier {competency.global_tier} · "
            f"'{_cli} --all' to widen, '{_cli} role add <role>' to expand, "
            f"'{_cli} --internal' for debug verbs."
        )


def _suggest_command(cmd: str, valid_commands: list[str]) -> str | None:
    """Suggest a similar command using fuzzy matching."""
    from difflib import get_close_matches

    matches = get_close_matches(cmd, valid_commands, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _dispatch_extension(subcommand: str, ext_info: dict, eventbus=None) -> None:
    """Dispatch to an extension command (builtin or user).

    Reads `function` from the discovery dict (defaulting to ``main``) so
    AEOS `entry = "module:func"` declarations route to the named symbol —
    no longer hard-coded to `.main()`.

    Builtins use importlib.import_module() (they are part of the package).
    User extensions use spec_from_file_location() (loaded from arbitrary paths).

    `eventbus` (optional) — when provided, generic-exception failures publish a
    `cli.arg_error` event so TRIAGE's listener can match the failure to a
    known pattern and surface a remedy on the next CLI invocation. See
    `extensions/builtins/diagnostics/cli_listener.py`.
    """
    module_path = ext_info["module"]
    function_name = ext_info.get("function", "main") or "main"
    is_builtin = ext_info.get("builtin", False)

    try:
        if is_builtin:
            import importlib

            mod = importlib.import_module(module_path)
            handler = getattr(mod, function_name, None)
            if handler is None:
                print(
                    f"neut: extension {subcommand} declared entry "
                    f"{module_path}:{function_name} but symbol is missing",
                )
                sys.exit(1)
            # Propagate non-zero exit codes. Noun modules that follow the
            # convention `def main() -> int` signal failure via return code;
            # discarding it hides real errors (we shipped with the bug that
            # `axi install-shim --target /fake` printed an error and exited 0).
            rc = handler()
            if isinstance(rc, int) and rc != 0:
                sys.exit(rc)
        else:
            # User extension — load from file path
            ext_root = Path(ext_info.get("root", ""))
            mod_rel = ext_info.get("module", "")
            mod_file = ext_root / mod_rel.replace(".", "/")
            # Try as .py file or as package
            if mod_file.with_suffix(".py").exists():
                mod_file = mod_file.with_suffix(".py")
            elif (mod_file / "__init__.py").exists():
                mod_file = mod_file / "__init__.py"
            else:
                print(f"neut: extension module not found: {mod_rel}")
                sys.exit(1)
            import importlib.util

            spec = importlib.util.spec_from_file_location(f"neut_ext.{subcommand}", str(mod_file))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                handler = getattr(mod, function_name, None)
                if handler is None:
                    print(
                        f"neut: extension {subcommand} declared entry "
                        f"{module_path}:{function_name} but symbol is missing",
                    )
                    sys.exit(1)
                rc = handler()
                if isinstance(rc, int) and rc != 0:
                    sys.exit(rc)
            else:
                print(f"neut: cannot load extension module: {mod_file}")
                sys.exit(1)
    except KeyboardInterrupt:
        # Universal Ctrl+C policy: friendly cancel line, exit code 130
        # (the standard for SIGINT-terminated processes).  Avoids the
        # bare-traceback that would otherwise surface from nested
        # extension handlers.
        print("\n  Cancelled.", file=sys.stderr)
        sys.exit(130)
    except ImportError as e:
        print(f"neut: failed to load {subcommand}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"neut: command '{subcommand}' failed: {e}")
        # Emit cli.arg_error so TRIAGE's listener can match a known pattern
        # and stage a remedy for the next CLI invocation.  Soft-fails: a
        # broken bus/listener must not block the user's exit path.
        if eventbus is not None:
            try:
                from axiom.infra.self_heal import emit_cli_error
                emit_cli_error(
                    bus=eventbus,
                    command=subcommand,
                    argv=list(sys.argv),
                    error=e,
                    recovered=False,
                )
            except Exception:
                pass
        sys.exit(1)


def main():
    # Build the argparse tree so argcomplete can offer tab completions.
    # autocomplete() exits early when the shell is requesting completions;
    # otherwise it's a no-op and we fall through to the manual dispatch below.
    parser = get_parser()
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except ImportError:
        pass  # argcomplete not installed — no completion, no crash

    # --version / -V flag
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V"):
        try:
            from axiom.infra.branding import get_branding as _gb3

            _b3 = _gb3()
            from importlib.metadata import version as _ver

            try:
                v = _ver(_b3.package_name)
            except Exception:
                try:
                    v = _ver("axi-platform")
                except Exception:
                    v = _ver("axiom")
            print(f"{_b3.cli_name} {v}")
        except Exception:
            print("axi unknown")
        sys.exit(0)

    # Show pending changelog from a recent update, then check for newer version
    _show_pending_changelog()
    _check_and_prompt_update()
    _self_heal_daemon_agents()

    if len(sys.argv) < 2:
        # Bare invocation → print help, like `claude` / `gh` / `kubectl`.
        # Earlier behavior dropped the user into `chat --bare` (a session
        # picker), which is a confusing first-touch experience for new
        # users who don't yet know what `axi` does.  Help shows them the
        # surface AND points at `axi chat` if that's what they wanted.
        print_usage()
        sys.exit(0)

    subcommand = sys.argv[1]

    # Top-level reveal flags are accepted both AS the subcommand
    # (`axi --all`, `axi --tier core`) and after the literal `help`
    # subcommand (`axi help --all`). The first form is the natural one
    # for users who just want a wider listing without typing 'help'.
    _help_flags = {"-h", "--help", "help", "--all", "--internal"}
    _help_kw_flags = ("--tier", "--group", "--role")
    _is_help_flag = (
        subcommand in _help_flags
        or any(subcommand.startswith(f"{kw}=") or subcommand == kw for kw in _help_kw_flags)
    )
    if _is_help_flag:
        # Parse optional reveal flags consumed by `axi help` directly:
        # --all, --tier <t>, --internal, --group <name>, --role <r>.
        rest = sys.argv[1:] if subcommand != "help" else sys.argv[2:]
        show_all = "--all" in rest
        include_internal = "--internal" in rest
        tier_override = None
        intent_group = None
        role_override: list[str] = []
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg == "--tier" and i + 1 < len(rest):
                tier_override = rest[i + 1]
                i += 2
                continue
            if arg.startswith("--tier="):
                tier_override = arg.split("=", 1)[1]
                i += 1
                continue
            if arg == "--group" and i + 1 < len(rest):
                intent_group = rest[i + 1]
                i += 2
                continue
            if arg.startswith("--group="):
                intent_group = arg.split("=", 1)[1]
                i += 1
                continue
            if arg == "--role" and i + 1 < len(rest):
                role_override.append(rest[i + 1])
                i += 2
                continue
            if arg.startswith("--role="):
                role_override.append(arg.split("=", 1)[1])
                i += 1
                continue
            i += 1
        print_usage(
            show_all=show_all,
            tier_override=tier_override,
            include_internal=include_internal,
            intent_group=intent_group,
            role_override=tuple(role_override) if role_override else None,
        )
        sys.exit(0)

    if subcommand == "--help-all":
        print_usage(show_all=True)
        sys.exit(0)

    if subcommand in ("doctor", "dr"):
        # Accept optional error context + optional --fix flag.
        #   axi dr                          → diagnose only
        #   axi dr "error message"          → diagnose with context
        #   axi dr --fix                    → diagnose then auto-run remediable fixes
        #   axi dr --fix "error message"    → both
        error_context = None
        auto_fix = False
        args = sys.argv[2:]
        # Strip --fix anywhere it appears so it doesn't end up in error_context.
        if "--fix" in args:
            auto_fix = True
            args = [a for a in args if a != "--fix"]
        if args:
            if args[0] in ("--error", "-e") and len(args) > 1:
                error_context = args[1]
            elif not args[0].startswith("-"):
                error_context = " ".join(args)
        sys.exit(cmd_doctor(error_context, auto_fix=auto_fix))

    module_path = SUBCOMMANDS.get(subcommand)

    # Check extension commands (builtin + user) if not a core command
    ext_cmd_info = None
    if not module_path:
        ext_cmds = _merge_extension_commands()
        if subcommand in ext_cmds:
            ext_cmd_info = ext_cmds[subcommand]

    if not module_path and not ext_cmd_info:
        all_cmds = list(SUBCOMMANDS.keys()) + list(_merge_extension_commands().keys())
        suggestion = _suggest_command(subcommand, all_cmds)
        print(f"neut: unknown subcommand '{subcommand}'")
        if suggestion:
            print(f"\nDid you mean: neut {suggestion}?")
        print("\nRun 'neut --help' for usage.")
        sys.exit(1)

    # Availability gate (ADR-047): refuse a command whose declared capability
    # requirements are unmet, with a reason + remedy, rather than letting it
    # crash on the missing dependency mid-run.
    from axiom.infra import cli_gating

    _requires = (
        ext_cmd_info.get("requires", [])
        if ext_cmd_info
        else _SUBCOMMAND_REQUIRES.get(subcommand, [])
    )
    _unmet = cli_gating.unmet_requirements(_requires)
    if _unmet:
        print(cli_gating.format_unavailable(subcommand, _unmet))
        sys.exit(1)

    # Remove the subcommand from argv so the handler sees only its own args
    handler_args = list(sys.argv[2:])
    sys.argv = [f"neut {subcommand}"] + handler_args

    # cli.command_started observer event — fired before dispatching, soft-fails
    # if the bus or platform-hook subsystems aren't available (e.g., partial
    # install). See `docs/specs/spec-hooks.md` §4 + §8c.
    _started_at = time.monotonic()
    _hook_eventbus = None
    try:
        from axiom.infra.bus import EventBus
        from axiom.infra.cli_hooks import (
            publish_command_started,
            surface_pending_diagnoses,
        )
        from axiom.infra.paths import get_project_root

        _hook_eventbus = EventBus(
            log_path=get_project_root() / "runtime" / "logs" / "cli_events.jsonl",
        )
        # Pre-dispatch: surface any pending TRIAGE diagnoses from prior CLI
        # failures so the user sees the remedy before re-running the broken
        # command. Soft-fails internally; never blocks dispatch.
        surface_pending_diagnoses()
        publish_command_started(
            command_path=subcommand,
            args=handler_args,
            principal=os.environ.get("USER", ""),
            eventbus=_hook_eventbus,
        )
        # Wire TRIAGE's CLI failure listener so any cli.arg_error event
        # published during this command becomes a pending diagnosis the
        # NEXT command will surface. Idempotent across processes; the bus
        # subscription is per-process.
        try:
            from axiom.extensions.builtins.diagnostics import cli_listener
            cli_listener.register(_hook_eventbus)
        except Exception:
            pass
    except Exception:
        # Never let hook plumbing block the CLI itself.
        _hook_eventbus = None

    _exit_code = 0
    try:
        if ext_cmd_info:
            _dispatch_extension(subcommand, ext_cmd_info, eventbus=_hook_eventbus)
        else:
            try:
                import importlib

                assert module_path is not None
                module = importlib.import_module(module_path)
                module.main()
            except ImportError as e:
                print(f"neut: failed to load {subcommand} handler: {e}")
                _exit_code = 1
                sys.exit(1)
            except KeyboardInterrupt:
                # Universal Ctrl+C policy (see top-level handler).
                print("\n  Cancelled.", file=sys.stderr)
                _exit_code = 130
                sys.exit(130)
    except SystemExit as se:
        _exit_code = int(se.code) if isinstance(se.code, int) else (0 if se.code is None else 1)
        raise
    finally:
        try:
            from axiom.infra.cli_hooks import publish_command_ended

            duration_ms = int((time.monotonic() - _started_at) * 1000)
            publish_command_ended(
                command_path=subcommand,
                exit_code=_exit_code,
                duration_ms=duration_ms,
                eventbus=_hook_eventbus,
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
