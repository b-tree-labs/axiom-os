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
    """Offer a newer release before the command runs; otherwise, a one-line notice.

    Decided 2026-10-06: the passive banner alone left a new user on an install
    six releases behind for an afternoon. A person at a terminal is now asked,
    before the command they typed and at most once a day per release, and an
    upgrade re-runs their command on the new code. With no person to ask, in a
    source checkout, or for `update` and help, the one-line notice remains.

    1-hour cache via VersionChecker. Disable entirely with
    AXIOM_DISABLE_UPDATE_NUDGE=1.
    """
    # A person at a terminal is asked first, once a day per release, and the
    # command they typed then runs on the new code (update.offer.offer_at_entry).
    # Everyone else (no person, a checkout, `update`/help) gets the line below.
    try:
        from axiom.extensions.builtins.update.offer import offer_at_entry

        if offer_at_entry(sys.argv[1:], interactive=_is_interactive()) != "notice":
            return
    except Exception:  # noqa: BLE001 - an update offer never blocks the command
        pass
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


def _is_interactive() -> bool:
    """Whether a person is at the other end of this process.

    A named seam rather than two inline isatty() calls, because a test that
    wants to exercise the interactive path otherwise has to patch objects
    that pytest itself replaces — and ends up asserting on a function that
    returned early for a reason unrelated to what it was testing.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _self_heal_daemon_agents() -> None:
    """Opportunistic re-registration of missing always-on agents.

    Runs on CLI startup in interactive sessions, throttled to once per hour
    via a state file. This is the floor below Tidy's own drift detector: it
    re-registers the health agent itself when it is the thing that went
    missing (the scenario where no service-registration ever happened, or
    where the host was rebooted without user-service linger enabled).

    Never raises, and never blocks — which it used to, by asking a
    host-modifying question with ``input()`` in the middle of whatever
    command the person actually ran. Consent to install an OS task is a
    real question; arriving mid-command is when they have the least context
    and the least patience, and "don't ask again" is the cheapest key to
    press, so the interruption bought a decision it should not have been
    buying. It now leaves a line they can ignore and asks nowhere.
    """
    if not _is_interactive():
        return
    if os.environ.get("AXIOM_DISABLE_SELF_HEAL") == "1":
        return
    # A redirected state dir means a throwaway venv, a CI job or a review
    # environment — an install that will be gone shortly. There is no host
    # to keep an OS task alive on, so there is nothing worth saying.
    if os.environ.get("AXI_STATE_DIR"):
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
            # Previously approved: silently re-heal so a reboot or eviction of
            # an already-consented service repairs itself. Repair is not a
            # question, so it stays silent.
            register_all_daemon_agents()
        elif needs_prompt(consent, missing) or reoffer:
            # A notice, not a prompt. Silence would be worse — they would
            # never learn the agents are not running — but the decision
            # belongs to a moment they chose, not this one.
            if reoffer:
                print(
                    f"\n  Background agents are still off; {_product} has "
                    f"upgraded to {cur_ver} since you opted out."
                )
            else:
                # The category, not a line per agent. "TIDY, SCAN, PRESS" tells
                # somebody who has never run one of these nothing about what
                # would start on their machine — but this is a notice, and a
                # notice that grows into a paragraph stops being ignorable
                # (tests/infra/test_self_heal_does_not_interrupt.py caps it).
                # So the clause that says what kind of thing an agent is rides
                # inside the line that was already there.
                print(
                    f"\n  {len(missing)} background agent(s) — unattended upkeep "
                    f"on this machine — are not running: {', '.join(missing)}"
                )
            print(
                f"  `{_cli} agents register` describes each one and sets them up "
                f"(installs an OS task that survives reboots). `later` is an answer.\n"
            )
        # else: opted out and no upgrade to re-offer on — stay quiet.

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass
    except Exception:
        # Never block the CLI for self-heal. The swallow is right — a
        # cosmetic notice must not take down somebody's command — but a
        # silent one is undebuggable, and this function failing silently is
        # exactly how it sat broken without anybody knowing.
        if os.environ.get("AXIOM_SELF_HEAL_DEBUG"):
            raise


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
    # Three spellings of one act, because the cost of a newcomer stopping at the
    # first line of a guide is higher than the cost of an extra dict entry.
    # `start` is what somebody types having just installed this; nobody arriving
    # for the first time guesses `config`, which reads like changing a setting
    # you already have. A colleague hit `unknown subcommand` on `start` and
    # stopped there.
    "start": "axiom.setup.cli",  # Alias — the verb a newcomer reaches for
    "setup": "axiom.setup.cli",  # Alias — quickstart guide says `neut setup`
    "ext": "axiom.extensions.cli",
    "infra": "axiom.setup.infra",
    "plan": "axiom.cli.plan",  # Plan I/O (analysis §10.1)
    "approve": "axiom.cli.approve",  # Answer what an agent is waiting on
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
            print("💡 Model-generated analysis (unverified — the fixes above are the authoritative ones):")
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


def _entry_point_answers(script) -> tuple[bool, str, str]:
    """Run the console script and see whether it works.

    A source-pattern check goes stale silently: it recognises the module
    names it was written against, and every consumer shipping its own entry
    point is reported broken while working perfectly. Executing it is the
    question actually being asked.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [str(script), "--version"], capture_output=True, text=True, timeout=20
        )
    except Exception as exc:  # noqa: BLE001
        return False, "", f"Did not run ({type(exc).__name__}) at {script}"
    if proc.returncode == 0 and proc.stdout.strip():
        # `--version` prints one line per component on purpose, so the output
        # stays greppable. This is a one-line status field, and embedding the
        # raw text put a newline in the middle of it — the entry point's path
        # ended up on its own line under a version number.
        reported = " ".join(proc.stdout.split())
        return True, "console", f"Valid ({reported}) at {script}"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, "", f"Stale at {script}" + (f": {detail[0][:60]}" if detail else "")


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
                # Neither pattern matched — but the patterns name MODULES, and
                # a branded distribution has its own. `neut` imports
                # its own module, so the doctor called a working entry point
                # "Stale" and prescribed a reinstall that would change
                # nothing. Ask the entry point itself rather than reading its
                # source for a name we happen to know.
                entry_ok, entry_type, entry_status = _entry_point_answers(neut_script)
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
        from axiom.extensions.builtins.agents.cli import _discover_agent_extensions

        agent_exts = _discover_agent_extensions()
        daemon_agents = [e for e in agent_exts if e.agent and e.agent.is_always_on]
        if daemon_agents:
            # ONE Background Service dispatches every daemon agent; per-agent
            # OS services were the pre-0.11.1 model and are deliberately gone.
            # Asking each agent whether it has its own service therefore
            # answered "stopped" six times on a machine where all six were
            # dispatching happily, and prescribed `agents start`, which fixes
            # nothing because nothing is stopped.
            from axiom.extensions.builtins.agents.cli import (
                _make_background_service_manager,
            )

            service = _make_background_service_manager().status()
            agents_ok = service.status == "running"
            checks.append(
                {
                    "name": "Agent Services",
                    "ok": agents_ok,
                    "status": (
                        f"Background Service {service.status} ({service.provider}), "
                        f"dispatching {len(daemon_agents)} agents"
                    ),
                    "fix": f"{_cli} agents register" if not agents_ok else None,
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

    # 8. Whatever else the registered brand says has to be true.
    #
    # `doctor` ends in a verdict, and a verdict is only worth what the
    # questions behind it were worth. Its own checks cover the platform; a
    # product built on the platform usually has one or two more, and without
    # this it printed "looks healthy" over them.
    checks.extend(_brand_health_extras())

    return {
        "checks": checks,
        "python_version": sys.version,
        "platform": sys.platform,
        "cwd": cwd,
    }


def _brand_health_extras(brand: object | None = None) -> list[dict]:
    """Health rows the registered brand contributes, validated.

    Never raises and never trusts. This runs inside the one command somebody
    types when things are already wrong, so a brand whose hook is broken must
    cost them a row, not the diagnosis.

    A row with no name renders as a blank line; a row with no verdict would
    have to be assumed, and assuming True is how a check that never ran
    becomes a check that passed. Both are dropped.
    """
    if brand is None:
        try:
            from axiom.infra.branding import get_branding

            brand = get_branding()
        except Exception:  # noqa: BLE001 - branding is optional
            return []

    hook = getattr(brand, "health_extras_fn", None)
    if not callable(hook):
        return []
    try:
        rows = hook()
    except Exception:  # noqa: BLE001 - a brand must not break the diagnosis
        return []
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not row.get("name") or "ok" not in row:
            continue
        out.append(
            {
                "name": str(row["name"]),
                "ok": bool(row["ok"]),
                "status": str(row.get("status", "")),
                "fix": row.get("fix"),
            }
        )
    return out



#: Things a generated line may not tell somebody to do.
#:
#: Written after a clean macOS install was told, on its first `doctor`, to run
#: `sudo apt-get install -y launchd`, `sudo launchd -a` and
#: `sudo killall -9 axiom`. Wrong operating system, impossible package,
#: `killall -9`, and a reinstall of the platform — under a heading that did
#: not say a model wrote it.
_UNSAFE_IN_A_DIAGNOSIS = (
    "sudo ",
    "rm -rf",
    "rm /",
    "killall",
    "chmod 777",
    "apt-get",
    "apt install",
    "yum install",
    "brew install",
    "| sh",
    "| bash",
    "curl ",
    "wget ",
    "mkfs",
    "dd if=",
)


def _safe_diagnosis_line(line: str) -> str | None:
    """*line*, or ``None`` when it tells somebody to run something risky.

    Deliberately crude and deliberately one-directional: it drops lines rather
    than rewriting them, because a half-edited command is more dangerous than
    a missing one.
    """
    lowered = line.lower()
    if any(token in lowered for token in _UNSAFE_IN_A_DIAGNOSIS):
        return None
    return line


def _safe_diagnosis(text: str | None) -> str | None:
    """A generated diagnosis with any command-like lines removed.

    Filtered rather than discarded: a model mentioning a package manager in
    passing should not cost the explanation around it. But an analysis that is
    nothing BUT commands yields nothing at all, because a heading over an
    empty box is worse than no heading.
    """
    if not text:
        return None
    kept = [line for line in str(text).splitlines()
            if _safe_diagnosis_line(line) is not None]
    body = "\n".join(kept).strip()
    return body or None


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

WHY: [brief explanation of the likely root cause]

Rules:
- Be concise (under 150 words)
- Explain. Do NOT give commands to run, and do NOT invent installation or
  remediation steps. Each check above already carries the fix written by
  somebody who knew the answer, and the caller prints those.
- You do not know this machine's operating system, package manager or install
  method. Say what appears to be wrong, not what to type.
- Plain text only — no markdown formatting"""

        response = gateway.complete(prompt)
        raw = response.text if hasattr(response, "text") else str(response)
        # Asked-for or not, a model will sometimes produce commands. This is
        # the last thing between one and an adopter's shell.
        return _safe_diagnosis(raw)

    except Exception:
        # Silently fall back to basic mode
        return None


# Help text for core subcommands (extensions provide their own descriptions)
_SUBCOMMAND_HELP = {
    "config": "Interactive onboarding wizard",
    "setup": "Interactive onboarding wizard (alias for config)",
    "ext": "Manage extensions (builtin + user)",
    "plan": "Plan I/O — create, show, edit, import, approve plans",
    "approve": "Answer actions an agent is holding for a human decision",
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


def _parser_wanted_for(name: str) -> bool:
    """Whether this subcommand's own parser has to be built right now.

    `get_parser()` exists for completion and `--help`; dispatch goes through
    importlib separately. Building every subcommand's parser meant importing
    every builtin extension's CLI module, and an extension package's
    ``__init__`` re-exports its serving surface — so asking for a list of
    verbs imported FastAPI, SQLAlchemy and the MCP SDK. 463 modules to print
    help, which on a filesystem where reads are slow (a venv on a Windows
    drive mounted into WSL) reads as a hung CLI rather than a slow one.

    Two callers genuinely need the whole tree, and both announce themselves:
    argcomplete sets ``_ARGCOMPLETE`` in the environment, and the completion
    verb builds the tree on purpose. Everyone else needs the parser for the
    one verb they typed, so that is the only one imported.
    """
    if os.environ.get("_ARGCOMPLETE") or os.environ.get("AXI_FULL_PARSER"):
        return True
    for token in sys.argv[1:]:
        if not token.startswith("-"):
            return token == name
    return False


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

        if not _parser_wanted_for(name):
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

        if info.get("builtin") and not _parser_wanted_for(name):
            # The verb is listed by name and description, which the registry
            # already holds; its module is imported only when it is the verb
            # being run. See `_parser_wanted_for`.
            subparsers.add_parser(name, help=description)
        elif info.get("builtin"):
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


def banner_versions() -> list[tuple[str, str]]:
    """``(label, version)`` for the banner, broadest context first.

    Three numbers can be in play and they are not interchangeable:

    * the **site** this install is bound to, when it is bound to one
    * the **product** — whatever brand is registered, by its own
      distribution name
    * the **platform** — ``axiom-os-lm`` underneath

    The banner used to print the PLATFORM's version under whatever brand was
    registered, so an adopter of a branded distribution read a number that
    was not their product's and had no way to tell. A version string is the
    first sentence of every bug report, and that one was wrong.

    The site entry comes from the brand, because the platform has no
    business knowing what a site is. Never raises: a banner that cannot
    draw itself must still let the CLI run.
    """
    from importlib.metadata import version as _pkg_version

    out: list[tuple[str, str]] = []
    brand = None
    try:
        from axiom.infra.branding import get_branding

        brand = get_branding()
    except Exception:  # noqa: BLE001 - branding is optional here
        brand = None

    extras_fn = getattr(brand, "version_extras_fn", None)
    if callable(extras_fn):
        try:
            out.extend(tuple(pair) for pair in (extras_fn() or []))
        except Exception:  # noqa: BLE001 - a brand must not break the banner
            pass

    product_pkg = (getattr(brand, "package_name", "") or "").strip()
    platform_pkg = "axiom-os-lm"

    def _v(pkg: str) -> str:
        try:
            return _pkg_version(pkg)
        except Exception:  # noqa: BLE001 - not installed as a distribution
            return ""

    # A brand whose package IS the platform is not a separate product, so it
    # contributes no row of its own. Getting this wrong dropped BOTH rows and
    # fell through to a bare version, which is the bug this function exists
    # to fix, reintroduced one layer down.
    branded = bool(product_pkg) and product_pkg != platform_pkg
    if branded:
        product_version = _v(product_pkg)
        if product_version:
            label = (getattr(brand, "cli_name", "") or "").strip() or product_pkg
            out.append((label, product_version))

    platform_version = _v(platform_pkg)
    if platform_version:
        # Labelled whenever anything else is shown, so the reader can tell
        # which number is which; bare when it is the only one.
        out.append(("axiom" if out else "", platform_version))

    return out


def version_report() -> str:
    """What ``--version`` prints: every number that is in play, one per line.

    Resolved by :func:`banner_versions`, deliberately — this used to do its
    own lookup and print a single number, so the banner and ``--version``
    could disagree about the same install. ``--version`` is the surface an
    operator actually runs when filing a bug, and it was the one showing
    less. Two derivations of the same fact are two chances to be wrong about
    it.

    One line per component so the output stays greppable
    (``<consumer> --version | grep axiom``), broadest context first::

        site-a 1.6.74
        consumer 1.9.0
        axiom 0.47.0

    Never raises: ``--version`` must answer even from a broken install.
    """
    try:
        rows = banner_versions()
    except Exception:  # noqa: BLE001 - see the docstring
        rows = []
    if not rows:
        return "unknown"

    fallback = ""
    try:
        from axiom.infra.branding import get_branding

        fallback = (getattr(get_branding(), "cli_name", "") or "").strip()
    except Exception:  # noqa: BLE001 - branding is optional
        fallback = ""

    return "\n".join(f"{label or fallback or 'axiom'} {ver}".strip() for label, ver in rows)


def render_banner_versions() -> str:
    """The version line, or "" when nothing can be resolved."""
    parts = [f"{label} {ver}".strip() if label else f"v{ver}"
             for label, ver in banner_versions()]
    return "  ·  ".join(parts)


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


_DEFAULT_ART_STYLE = "bold #00cfff"


def _print_branded_banner(
    *,
    cli: str,
    product: str,
    ver: str,
    mascot: str,
    art: str,
    Console,
    Panel,
    Text,
    frames=None,
    style_map=None,
) -> None:
    """Draw a consumer's own mascot inside the platform frame.

    The frame is the platform and the mascot is the agent who lives in it,
    so a distribution supplies art and a name and gets the same first-touch
    panel — no robot-specific painters, because those describe Axi's anatomy
    and mean nothing to anyone else's creature.
    """
    # Every pose is padded to one width. A frame that is narrower than its
    # neighbours would slide the welcome column left and right as the
    # routine plays, which reads as a rendering fault rather than motion.
    poses = [art] + [f for f, _ in (frames or [])]
    art_width = max(
        (len(line) for pose in poses for line in pose.splitlines()), default=0
    )

    welcome = [
        Text.assemble(("Hi, I'm ", "white"), (mascot, "bold bright_white"), (".", "white")),
        Text.assemble(
            ("Directive: ", "italic bright_yellow"), ("assist.", "bold bright_green")
        ),
        Text(""),
        Text.assemble(
            ("Try ", "dim"), (f"`{cli} chat`", "bold green"), (" to talk.", "dim")
        ),
        Text("Or pick a command below.", style="dim"),
    ]
    # Offset the text by a row so it sits against the body rather than the
    # top of the head.
    welcome = [Text("")] + welcome

    def _compose(pose: str) -> "Text":
        lines = pose.splitlines()
        body = Text()
        for index in range(max(len(lines), len(welcome))):
            line = lines[index] if index < len(lines) else ""
            padded = line.ljust(art_width)
            if style_map:
                # Per glyph, so a consumer can paint a body one colour and its
                # markings another. Runs of the same style merge on append.
                for char in padded:
                    # Blanks carry no style: they are padding, not part of the
                    # creature, and styling them can tint a terminal's cell.
                    if char == " ":
                        body.append(" ")
                    else:
                        body.append(char, style=style_map.get(char, _DEFAULT_ART_STYLE))
            else:
                body.append(padded, style=_DEFAULT_ART_STYLE)
            body.append("    ")
            if index < len(welcome):
                body.append_text(welcome[index])
            body.append("\n")
        if ver:
            body.append("\n")
            body.append(ver, style="dim")
            body.append("  ·  ", style="dim")
            body.append(mascot, style="dim italic #00cfff")
            body.append(" + the agent team", style="dim italic")
        return body

    def _panel(body):
        return Panel(
            body,
            title=f"[bold bright_white on #0D2B5C] {product.upper()} [/]",
            title_align="center",
            border_style="bold #5DADE2",
            padding=(1, 2),
            expand=False,
        )

    if frames and _should_animate_banner():
        from time import sleep

        from rich.live import Live

        console = Console()
        try:
            with Live(
                _panel(_compose(frames[0][0])),
                console=console,
                refresh_per_second=30,
            ) as live:
                for pose, delay in frames:
                    live.update(_panel(_compose(pose)))
                    if delay:
                        sleep(delay)
        except Exception:
            # Motion is a courtesy. A terminal that cannot do it still gets
            # the banner.
            console.print(_panel(_compose(art)))
        _mark_welcome_shown()
        return

    Console().print(_panel(_compose(art)))


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
        # Version resolution moved into `banner_versions`, which has to run
        # for the branded and bound cases too; only rich is guarded here.
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except Exception:
        print(f"  [◉‿◉]  {cli} — {product}")
        print(f"         Hi, I'm {cli.title()}. Try '{cli} chat' to talk.")
        print()
        return

    try:
        ver = render_banner_versions()
    except Exception:
        ver = ""

    # A consumer that brought its own creature gets it, in the platform's
    # frame. Without this the same binary greeted you as Neut when you typed
    # `neut` and as Axi the moment you added `--help`, which is exactly the
    # "which robot is this?" confusion the branding layer exists to prevent.
    try:
        from axiom.infra.branding import get_branding as _gb_banner

        _brand = _gb_banner()
    except Exception:
        _brand = None
    _brand_art = (getattr(_brand, "banner_art", "") or "").strip("\n")
    _brand_frames = None
    _frames_fn = getattr(_brand, "banner_frames", None)
    if callable(_frames_fn):
        try:
            _brand_frames = _frames_fn()
        except Exception:
            _brand_frames = None
    if _brand_art:
        _print_branded_banner(
            cli=cli,
            product=product,
            ver=ver,
            mascot=getattr(_brand, "mascot_name", "") or product,
            art=_brand_art,
            Console=Console,
            Panel=Panel,
            Text=Text,
            frames=_brand_frames,
            style_map=getattr(_brand, "banner_style_map", None),
        )
        return

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

    # The hull placard stays a stencil — uppercase is right for a marking
    # painted on a robot's body. The prose is the mascot's NAME, and a name
    # is not shouted: it reads "Axi", not "AXI".
    _mascot_name = getattr(_brand, "mascot_name", "") or "Axi"

    def _axi_word() -> Text:
        """The mascot wordmark — its name on a yellow placard."""
        t = Text()
        t.append(_mascot_name, style="bold black on #FFC107")
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
            body.append(ver, style="dim")
            body.append("  ·  ", style="dim")
            body.append(_mascot_name, style="dim italic #FFC107")
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

    # Always-visible bootstrapping verbs — start + config + dr + ext + role are
    # how the user finds their way around regardless of competency.
    print("Commands:")
    print("  start     Set up this install — interactive wizard")
    print("  config    Same as `start`")
    print("  dr        Diagnose environment issues")
    print("  ext       Manage extensions (builtin + user)")
    print("  role      Manage your role membership")

    # Group remaining builtins by intent for readability — readers scan
    # by activity ("Research", "Build") faster than by alphabetised noun.
    # Iteration order: the user's activated intents first (so a researcher
    # sees signal under "Investigate", not "Maintain"), then any intents
    # the user *isn't* activating but has access to via reveal flags,
    # then unclassified commands under "Other".
    skip = {"start", "config", "setup", "dr", "ext", "role"}
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


def _cli_name() -> str:
    """The command the operator actually typed, for error prefixes.

    These messages hardcoded a consumer distribution's binary name ("neut"),
    which leaked a consumer brand out of the domain-agnostic `axi` CLI
    (sandbox audit 2026-09-18; consumer-name-leak program).
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


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
                    f"{_cli_name()}: extension {subcommand} declared entry "
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
                print(f"{_cli_name()}: extension module not found: {mod_rel}")
                sys.exit(1)
            import importlib.util

            spec = importlib.util.spec_from_file_location(f"neut_ext.{subcommand}", str(mod_file))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                handler = getattr(mod, function_name, None)
                if handler is None:
                    print(
                        f"{_cli_name()}: extension {subcommand} declared entry "
                        f"{module_path}:{function_name} but symbol is missing",
                    )
                    sys.exit(1)
                rc = handler()
                if isinstance(rc, int) and rc != 0:
                    sys.exit(rc)
            else:
                print(f"{_cli_name()}: cannot load extension module: {mod_file}")
                sys.exit(1)
    except KeyboardInterrupt:
        # Universal Ctrl+C policy: friendly cancel line, exit code 130
        # (the standard for SIGINT-terminated processes).  Avoids the
        # bare-traceback that would otherwise surface from nested
        # extension handlers.
        print("\n  Cancelled.", file=sys.stderr)
        sys.exit(130)
    except ImportError as e:
        print(f"{_cli_name()}: failed to load {subcommand}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"{_cli_name()}: command '{subcommand}' failed: {e}")
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
    # Tab completion needs the whole argparse tree. Nothing else does.
    #
    # `get_parser()` imports every builtin extension's module, because a
    # child parser is the only way to learn an extension's sub-verbs. That
    # costs ~1.4s — it pulls FastAPI, SQLAlchemy and the MCP type system into
    # a process that may be about to print one line of help — and it was paid
    # on EVERY invocation, then thrown away, because `autocomplete()` returns
    # immediately unless the shell is asking.
    #
    # The gate below is the same one argcomplete applies internally
    # (`if "_ARGCOMPLETE" not in os.environ: return`), moved out by one call
    # so the tree is never built to feed a no-op. Completions are unaffected:
    # when the shell does ask, the variable is set and the tree is built
    # exactly as before.
    #
    # Dispatch below never touched `parser` — it resolves the subcommand from
    # the manifest and imports that one module — so this removes work, not
    # behaviour.
    if "_ARGCOMPLETE" in os.environ:
        try:
            import argcomplete

            argcomplete.autocomplete(get_parser())
        except ImportError:
            pass  # argcomplete not installed — no completion, no crash

    # --version / -V flag
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V"):
        print(version_report())
        sys.exit(0)

    # Say so when this is the anchor worktree's code and you are standing
    # somewhere else. One string comparison, and it is the difference between
    # "that verb does not exist" and knowing why.
    try:
        from axiom.infra.source_provenance import warn_if_foreign_checkout

        warn_if_foreign_checkout()
    except Exception:  # noqa: BLE001 — a diagnostic never blocks the command
        pass

    # Arm the capability series. Every surface publishes a content-free
    # projection when a capability is invoked, and until something subscribes,
    # nothing is stored — the measurement reports as built and produces no
    # data. This is that something, for the CLI. Idempotent, honours
    # AXIOM_CAPABILITY_TELEMETRY, and never blocks a command.
    try:
        from axiom.infra.capability_telemetry import enable_capability_telemetry

        enable_capability_telemetry()
    except Exception:  # noqa: BLE001 — an observation never blocks the command
        pass

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
        print(f"{_cli_name()}: unknown subcommand '{subcommand}'")
        if suggestion:
            print(f"\nDid you mean: {_cli_name()} {suggestion}?")
        print(f"\nRun '{_cli_name()} --help' for usage.")
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
    sys.argv = [f"{_cli_name()} {subcommand}"] + handler_args

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
        surface_pending_diagnoses(command=subcommand or "")
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
                print(f"{_cli_name()}: failed to load {subcommand} handler: {e}")
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
