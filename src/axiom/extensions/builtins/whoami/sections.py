# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What ``whoami`` reports, one section per question someone asks when they
start a task or chase a fault on this machine.

Every section reads a source that already exists; none keeps data of its own.
Every section is read-only: it never creates a keypair, opens a database, or
calls a model unless the caller asked for that section by name, and it never
reads a secret's value. A section that fails reports its error in place, so
one broken source never hides the others.

Consumer layers add sections through the ``axiom.whoami_sections`` entry-point
group: each entry is a zero-argument callable returning ``(title, mapping)``.
The platform never names them.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Sections shown with no flags: who, what is running, where it belongs, and
#: what it can reach. The rest are named to be shown.
DEFAULT = ("you", "software", "node", "site", "access", "dev")
ALL = (*DEFAULT, "harness", "llm", "env")


# -- helpers -----------------------------------------------------------------


def _state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _identity_file() -> dict[str, Any]:
    """The raw identity record. Read raw because ``load_identity`` drops aliases."""
    path = _state_dir() / "identity" / "identity.json"
    if not path.is_file():
        path = Path.home() / ".axi" / "identity" / "identity.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    keep = ("owner", "aliases", "display_name", "node_id", "profile")
    return {k: data[k] for k in keep if k in data} | {"file": str(path)}


# -- sections ----------------------------------------------------------------


def you() -> dict[str, Any]:
    from axiom.infra.principal import node_posture, open_principal, principal_provenance

    posture = node_posture()
    out: dict[str, Any] = {"posture": posture}
    if posture == "open":
        out |= principal_provenance(open_principal())
    else:
        # Resolving an attested principal can create a keypair; whoami changes nothing.
        out["principal"] = f"({posture}: not resolved here, to leave the keychain untouched)"
    ident = _identity_file()
    if ident:
        out["owner"] = ident.get("owner")
        out["aliases"] = ident.get("aliases") or []
        out["display_name"] = ident.get("display_name")
    try:
        from axiom.extensions.builtins.principal.store import load

        desk = load()
        if desk is not None:
            out["desk"] = {
                "handle": desk.handle,
                "display_name": desk.display_name,
                "kind": desk.kind,
            }
    except Exception:  # noqa: BLE001 - an absent or older principal store is not a fault here
        pass
    out["os_user"] = getpass.getuser()
    git_email = _git(Path.cwd(), "config", "user.email")
    if git_email:
        out["git_email"] = git_email
    return out


def software() -> dict[str, Any]:
    import importlib.metadata as md

    import axiom
    from axiom.infra.branding import discover_portfolio_members, get_branding

    brand = get_branding()
    source = Path(axiom.__file__).resolve().parent
    root = Path(_git(source, "rev-parse", "--show-toplevel") or "")
    out: dict[str, Any] = {
        "product": brand.product_name,
        "cli": brand.cli_name,
        "axiom": {"installed_as": _version(md, "axiom-os-lm"), "source": str(source)},
        "python": f"{platform.python_version()} ({sys.executable})",
    }
    if str(root):
        out["axiom"]["checkout"] = {
            "root": str(root),
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
            "commit": _git(root, "rev-parse", "--short", "HEAD"),
            "uncommitted_files": len(
                [ln for ln in _git(root, "status", "--porcelain").splitlines() if ln]
            ),
            "pyproject_version": _pyproject_version(root),
        }
    members = []
    for m in discover_portfolio_members():
        if m.package_name == "axiom-os-lm":
            continue  # the platform itself, reported above
        members.append(
            {
                "product": m.product_name,
                "package": m.package_name,
                "version": _version(md, m.package_name),
            }
        )
    if members:
        out["consumers"] = members
    return out


def _version(md, package: str) -> str | None:
    try:
        return md.version(package)
    except md.PackageNotFoundError:
        return None


def _pyproject_version(root: Path) -> str | None:
    import tomllib

    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ]
    except (OSError, KeyError, ValueError):
        return None


def node() -> dict[str, Any]:
    ident = _identity_file()
    out: dict[str, Any] = {
        "node_id": ident.get("node_id"),
        "display_name": ident.get("display_name"),
        "federation_role": ident.get("profile"),
        "hardware_profile": None,
        "functions": None,
        "state_dir": str(_state_dir()),
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
    }
    out["notes"] = [
        "hardware profile (ADR-019) is not recorded anywhere yet",
        "node functions (ADR-164, proposed) are not declared yet",
    ]
    return out


def site() -> dict[str, Any]:
    out: dict[str, Any] = {"site": os.environ.get("AXIOM_SITE") or None}
    try:
        from axiom.vega.federation.discovery import NodeRegistry

        reg = NodeRegistry()
        reg.load()
        peers = reg.list_all()
        out["federation_peers"] = [
            getattr(p, "name", None) or getattr(p, "node_id", "?") for p in peers
        ]
    except Exception as exc:  # noqa: BLE001
        out["federation_peers"] = f"unreadable: {exc}"
    return out


def access() -> dict[str, Any]:
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    store = ForeignCredentialStore(_state_dir())
    entries = store.list()
    now = datetime.now(UTC)
    horizon = now + timedelta(days=14)
    expired, soon = [], []
    for meta in entries:
        raw = meta.get("expires_at")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when < now:
            expired.append(meta["name"])
        elif when < horizon:
            soon.append(meta["name"])
    out: dict[str, Any] = {
        "vault_entries": len(entries),
        "expired": expired,
        "expiring_within_14_days": soon,
    }
    try:
        from axiom.extensions.builtins.dev.signin import detect

        client, _ = detect(entries)
        out["sign_in_client"] = (
            {"credential": client.credential, "provider": client.provider} if client else None
        )
    except Exception:  # noqa: BLE001 - the dev extension is optional here
        pass
    for var in ("AXIOM_GATE_USERS_FILE", "AXIOM_GATE_API_KEYS_FILE"):
        if os.environ.get(var):
            out[var.lower()] = os.environ[var]
    return out


def dev() -> dict[str, Any]:
    root = _state_dir() / "dev"
    nodes = []
    if root.is_dir():
        for record in sorted(root.glob("*/node.json")):
            try:
                rec = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = int(rec.get("pid") or 0)
            nodes.append(
                {
                    "lane": record.parent.name,
                    "url": rec.get("url"),
                    "root": rec.get("root"),
                    "running": _alive(pid),
                }
            )
    return {"dev_nodes": nodes}


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def harness() -> dict[str, Any]:
    from axiom.extensions.builtins.mcp import install

    present = install.detect_tools()
    rows = {}
    for tool, found in sorted(present.items()):
        if not found:
            continue
        spec = install.spec_for(tool)
        installed = False
        if spec is not None:
            for name in install._our_server_names():
                try:
                    if install.read_entry(spec, name):
                        installed = True
                        break
                except Exception:  # noqa: BLE001 - a malformed harness config is reported below
                    rows[tool] = "config unreadable"
                    break
        rows.setdefault(tool, "installed" if installed else "present, not installed")
    return {"mcp": rows}


def llm() -> dict[str, Any]:
    """Configured model providers. Asked for by name: constructing the gateway may probe a local port."""
    from axiom.llm.gateway import Gateway

    gw = Gateway()
    return {
        "providers": [
            {"name": p.name, "model": p.model, "endpoint": p.endpoint, "usable": _usable(p)}
            for p in gw.providers
        ],
        "active": getattr(gw.active_provider, "name", None),
    }


def _usable(provider) -> bool:
    flag = getattr(provider, "is_usable", False)
    return bool(flag() if callable(flag) else flag)


def env() -> dict[str, Any]:
    """Which settings are set in this shell: names only, never values."""
    return {"set": sorted(k for k in os.environ if k.startswith(_env_prefixes()))}


def _env_prefixes() -> tuple[str, ...]:
    """The platform's prefixes plus each installed product's command name.

    Read from branding rather than listed here, so a consumer's settings show
    without the platform naming the consumer. A consumer with settings under
    another prefix reports them in its own section.
    """
    from axiom.infra.branding import get_branding

    names = {"AXIOM", "AXI", (get_branding().cli_name or "").upper()}
    return tuple(f"{n}_" for n in sorted(names) if n)


BUILTIN: dict[str, Callable[[], dict[str, Any]]] = {
    "you": you,
    "software": software,
    "node": node,
    "site": site,
    "access": access,
    "dev": dev,
    "harness": harness,
    "llm": llm,
    "env": env,
}


def contributed() -> list[tuple[str, Callable[[], tuple[str, dict[str, Any]]]]]:
    from importlib.metadata import entry_points

    out = []
    for ep in entry_points(group="axiom.whoami_sections"):
        try:
            out.append((ep.name, ep.load()))
        except Exception:  # noqa: BLE001 - a broken contributor must not hide the rest
            continue
    return out


# -- warnings ----------------------------------------------------------------


def warnings(report: dict[str, Any]) -> list[str]:
    """Mismatches that explain a surprising result before anyone goes looking."""
    out: list[str] = []
    you_ = report.get("you") or {}
    owner = you_.get("owner")
    principal = str(you_.get("principal") or "")
    if owner and principal and you_.get("posture") == "open":
        out.append(
            f"you act as {principal} (from the OS user, posture open), not as the identity owner {owner}; "
            "set AXIOM_IDENTITY_POSTURE or use an attested identity for that"
        )
    sw = report.get("software") or {}
    ax = sw.get("axiom") or {}
    co = ax.get("checkout") or {}
    if (
        co
        and ax.get("installed_as")
        and co.get("pyproject_version")
        and ax["installed_as"] != co["pyproject_version"]
    ):
        out.append(
            f"axiom runs from a checkout ({co.get('branch')} @ {co.get('commit')}, version {co['pyproject_version']}) "
            f"but its install record says {ax['installed_as']}; reports of the installed version are stale"
        )
    if co.get("uncommitted_files"):
        out.append(
            f"the axiom checkout has {co['uncommitted_files']} uncommitted file(s); this is not a released build"
        )
    acc = report.get("access") or {}
    if acc.get("expired"):
        out.append("expired credentials in the vault: " + ", ".join(acc["expired"]))
    if "node" in report and not (report.get("node") or {}).get("node_id"):
        out.append("this machine has no node identity; run the identity setup")
    return out


def collect(names: tuple[str, ...]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name in names:
        fn = BUILTIN.get(name)
        if fn is None:
            continue
        try:
            report[name] = fn()
        except Exception as exc:  # noqa: BLE001 - one broken source never hides the others
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
    for name, fn in contributed():
        try:
            title, body = fn()
            report[f"{title}"] = body
        except Exception as exc:  # noqa: BLE001
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
    report["warnings"] = warnings(report)
    return report


__all__ = ["ALL", "BUILTIN", "DEFAULT", "collect", "contributed", "warnings"]
