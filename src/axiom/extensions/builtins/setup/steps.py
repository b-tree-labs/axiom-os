# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The setup steps. Each one detects, proposes, applies (after a yes) and verifies.

Every step has the same four parts, and the split is the point:

- ``check`` only reads, so ``doctor`` runs exactly these checks and changes nothing.
- ``plan`` says in one sentence what ``apply`` would do, or ``None`` when there is
  nothing this command can do and the remedy is the person's (connect the VPN).
- ``apply`` does it, then ends with ``check``, so a step never reports success it
  did not observe.
- A failing ``check`` carries exactly one remedy: the line to read, not a list.

Steps are idempotent: running setup again re-checks everything, skips what is
already right and repairs what drifted. That is also what makes it resumable.

A product built on the platform adds its own steps (binding to its site, say)
through the brand's ``setup_steps_fn``, in the same shape.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .environment import SetupEnv, installer


@dataclass(frozen=True)
class Line:
    """One line of the checklist: green, or red with the one thing to do about it."""

    step: str
    title: str
    ok: bool
    status: str
    fix: str = ""
    #: Not applicable here (no AI apps found, a source checkout): shown, never red.
    skipped: bool = False

    @property
    def green(self) -> bool:
        return self.ok or self.skipped

    def as_doctor_row(self) -> dict[str, Any]:
        return {
            "name": f"Setup: {self.title}",
            "ok": self.green,
            #: Not applicable here: shown with a dot, never counted.
            "info": self.skipped and not self.ok,
            "status": self.status,
            "fix": None if self.green else (self.fix or None),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "title": self.title,
            "ok": self.ok,
            "skipped": self.skipped,
            "status": self.status,
            "fix": self.fix,
        }


class Step:
    """A setup step. Subclasses set ``name``/``title`` and implement the three methods."""

    name = ""
    title = ""
    #: Steps that must be green first; a step waiting on one is red with its remedy.
    needs: tuple[str, ...] = ()

    def check(self, env: SetupEnv) -> Line:  # pragma: no cover - abstract
        raise NotImplementedError

    def plan(self, env: SetupEnv) -> str | None:
        """What ``apply`` would do, in one sentence; ``None`` if nothing can be done here."""
        return None

    def apply(self, env: SetupEnv) -> Line:
        return self.check(env)

    # helpers
    def ok(self, status: str, *, skipped: bool = False) -> Line:
        return Line(self.name, self.title, True, status, skipped=skipped)

    def red(self, status: str, fix: str) -> Line:
        return Line(self.name, self.title, False, status, fix)

    def skip(self, status: str) -> Line:
        return Line(self.name, self.title, False, status, skipped=True)


# --------------------------------------------------------------------------- probes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):  # noqa: D401 - urllib's name
        return None


def http_status(url: str, *, timeout: float, headers: dict[str, str] | None = None
                ) -> tuple[int | None, dict[str, str], str]:
    """``(status, headers, reason)`` without following a redirect. ``status`` is
    ``None`` when nothing answered; ``reason`` then says why, in words."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=headers or {}, method="GET")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - the node's own address
            return resp.status, dict(resp.headers.items()), ""
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, ""
    except urllib.error.URLError as exc:
        return None, {}, str(getattr(exc, "reason", exc))
    except (TimeoutError, OSError) as exc:
        return None, {}, str(exc) or type(exc).__name__


def _host(url: str) -> str:
    return urlparse(url).hostname or url


# --------------------------------------------------------------------------- 1. upgrade


class UpgradeStep(Step):
    name = "upgrade"
    title = "Up to date"

    def _versions(self, env: SetupEnv) -> tuple[str, str | None, bool]:
        """``(installed, newest published or None if unknown, newer than installed?)``."""
        probe = env.seams.get("versions")
        if probe is not None:
            return probe()
        from axiom.extensions.builtins.update.version_check import VersionChecker

        info = VersionChecker().check_remote_version(timeout=env.timeout_s)
        return info.current, info.available, bool(info.is_newer)

    def _plan(self, env: SetupEnv):
        from axiom.extensions.builtins.update.installer import plan_upgrade
        from axiom.infra.branding import get_branding

        return env.seams.get("upgrade_plan") or plan_upgrade(get_branding().package_name)

    def check(self, env: SetupEnv) -> Line:
        how = env.seams.get("installer") or installer()
        if how == "checkout":
            return self.ok("running from a source checkout; git keeps it current", skipped=True)
        try:
            current, available, newer = self._versions(env)
        except Exception as exc:  # noqa: BLE001 - an index we cannot reach is not a fault here
            return self.ok(f"could not check for a newer version ({type(exc).__name__})",
                           skipped=True)
        if available is None:
            return self.ok(f"could not reach the package index; {current} installed", skipped=True)
        if newer:
            return self.red(f"{current} installed, {available} available",
                            f"run `{env.cli} setup` again and answer yes, or: {self._plan(env).manual}")
        return self.ok(f"{env.product} {current} is current")

    def plan(self, env: SetupEnv) -> str | None:
        p = self._plan(env)
        if p.cmd is None:
            return None
        return f"upgrade {env.product} with {p.label} ({p.manual})"

    def apply(self, env: SetupEnv) -> Line:
        p = self._plan(env)
        if p.cmd is None:
            return self.red("this install cannot upgrade itself", p.manual)
        runner = env.seams.get("run") or subprocess.run
        done = runner(p.cmd, capture_output=True, text=True)
        if getattr(done, "returncode", 1) != 0:
            last = ((getattr(done, "stderr", "") or "").strip().splitlines() or ["no message"])[-1]
            return self.red(f"the upgrade failed: {last}", f"run it yourself: {p.manual}")
        env.seams["upgraded"] = True
        return self.ok(f"{env.product} upgraded; the rest of setup continues on the new version")


# --------------------------------------------------------------------------- 2. vault


class VaultStep(Step):
    name = "vault"
    title = "Vault"

    def _settings(self):
        from axiom.extensions.builtins.secrets import vault_settings

        return vault_settings

    def check(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.secrets.vault_setup import Vault

        vs = self._settings()
        backend = vs.foreign_backend()
        if backend and backend != "openbao":
            return self.ok(f"credentials are kept by the {backend} backend (configured here)")
        if backend != "openbao":
            if self._runtime(env) is None:
                from axiom.extensions.builtins.secrets.vault_setup import binary_hint

                return self.red("no vault on this computer yet", binary_hint(env.system))
            return self.red("no vault on this computer yet",
                            f"run `{env.cli} setup` again and answer yes to start one")
        url = vs.openbao_url()
        vault = env.seams.get("vault_factory", Vault)(url)
        if not vault.reachable():
            return self.red(f"the vault at {url} is not running",
                            f"run `{env.cli} setup` again: it starts and unseals it")
        try:
            sealed = bool(vault.call("GET", "/v1/sys/seal-status").get("sealed"))
        except Exception:  # noqa: BLE001 - answered but not like a vault
            return self.red(f"something at {url} answers, but not as a vault",
                            f"stop whatever else uses that port, then run `{env.cli} setup` again")
        if sealed:
            return self.red(f"the vault at {url} is sealed",
                            f"run `{env.cli} setup` again: it unseals it")
        return self.ok(f"OpenBao at {url}, unsealed")

    def _runtime(self, env: SetupEnv) -> str | None:
        from axiom.extensions.builtins.secrets import vault_setup

        which = env.seams.get("which")
        if vault_setup.check_docker(env.system, which=which, run=env.seams.get("run", subprocess.run)) is None:
            return "docker"
        if vault_setup.bao_binary(which):
            return "binary"
        return None

    def plan(self, env: SetupEnv) -> str | None:
        runtime = self._runtime(env)
        if runtime == "docker":
            return ("start your vault in Docker, listening on this computer only (the first "
                    "start downloads OpenBao and can take a few minutes)")
        if runtime == "binary":
            return "start your vault from the OpenBao program, as you, listening on this computer only"
        return None

    def apply(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.secrets import vault_setup

        kwargs: dict[str, Any] = {}
        for seam in ("run", "vault_factory", "which", "popen", "sleep", "write_settings"):
            if seam in env.seams:
                kwargs[seam] = env.seams[seam]
        try:
            # The same container and port `vault setup` uses (AXI_OPENBAO_CONTAINER,
            # AXI_OPENBAO_PORT): on a computer that already runs a vault, a fresh home's setup
            # used the defaults and would have replaced it.
            container, port = vault_setup.resolve_target(environ=env.seams.get("environ"))
        except ValueError as exc:
            return self.red(f"{vault_setup.PORT_ENV} is not a port ({exc})",
                            f"set {vault_setup.PORT_ENV} to a free port number, or unset it")
        try:
            res = vault_setup.setup(
                state_dir=env.state_dir, system=env.system, runtime="auto",
                wait_s=env.vault_wait_s, container=container, port=port, **kwargs,
            )
        except vault_setup.ForeignContainer as exc:
            return self.red(f"left alone: a container named {container!r} keeps another home's vault",
                            str(exc))
        except vault_setup.SetupError as exc:
            first, *_rest = str(exc).splitlines() or ["the vault did not start"]
            return self.red(first, str(exc) if len(str(exc)) < 300 else f"run `{env.cli} vault setup` to see the log")
        for said in res.get("actions", []):
            env.say(f"    {said}")
        return self.check(env)


# --------------------------------------------------------------------------- 3. shell


class ShellStep(Step):
    name = "shell"
    title = "Shell"
    needs = ("vault",)

    def _target(self, env: SetupEnv) -> tuple[str, Path | None] | None:
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        found = sp.detect(
            home=env.home, env=env.seams.get("environ"), system=env.system,
            which=env.seams.get("which") or __import__("shutil").which,
            run=env.seams.get("run", subprocess.run),
        )
        if not found:
            return None
        shell = found[0]
        if shell == "powershell" and env.system != "windows":
            return None
        return shell, sp.profile_path(shell, home=env.home, system=env.system, run=None)

    def _block(self, shell: str) -> str:
        from axiom.extensions.builtins.secrets import shell_profiles as sp
        from axiom.extensions.builtins.secrets import vault_settings as vs

        return sp.render(shell, url=vs.openbao_url(), token_file=vs.describe().token_file)

    def check(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.secrets import shell_profiles as sp
        from axiom.extensions.builtins.secrets import vault_settings as vs

        if vs.foreign_backend() != "openbao":
            return self.skip("nothing to add: no local vault to point at")
        target = self._target(env)
        if target is None or target[1] is None:
            return self.skip("no start-up file for this shell; apps find the vault from its settings file")
        shell, path = target
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        span = sp._find_block(text)
        if span is not None and span[1] != -1:
            have = text[span[0]:span[1]]
            want = self._block(shell)
            if all(line in have for line in want.splitlines()[1:-1]):
                return self.ok(f"{sp.LABELS[shell]} finds the vault ({_tilde(path, env.home)})")
        return self.red(f"new {sp.LABELS[shell]} terminals do not know where the vault is",
                        f"run `{env.cli} setup` again and answer yes to add the marked lines to "
                        f"{_tilde(path, env.home)}")

    def plan(self, env: SetupEnv) -> str | None:
        target = self._target(env)
        if target is None or target[1] is None:
            return None
        return (f"add three marked lines to {_tilde(target[1], env.home)} so new terminals find "
                "the vault (a backup is made first; the token itself is never written there)")

    def apply(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.secrets import shell_profiles as sp

        target = self._target(env)
        if target is None or target[1] is None:
            return self.check(env)
        shell, path = target
        done = sp.apply(shell, path, self._block(shell))
        if done.outcome == "refused":
            return self.red(f"{_tilde(path, env.home)} was not changed", done.detail)
        if done.backup:
            env.say(f"    previous {path.name} saved as {done.backup.name}")
        return self.check(env)


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- 4. node


class NodeStep(Step):
    name = "node"
    title = "Node"

    def check(self, env: SetupEnv) -> Line:
        if not env.node:
            return self.red("no node address yet",
                            f"run `{env.cli} setup --node https://<your node>` with the address "
                            "your site administrator gave you")
        parsed = urlparse(env.node)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
            return self.red(f"{env.node!r} is not a node's base address",
                            "give the bare address, for example https://node.example.org "
                            "(nothing after the host)")
        probe = env.seams.get("http_status", http_status)
        status, _h, reason = probe(env.node.rstrip("/") + "/gate/keys", timeout=env.timeout_s)
        if status is not None:
            return self.ok(f"{_host(env.node)} answers from this network")
        low = reason.lower()
        if "name" in low and ("resolve" in low or "known" in low or "not found" in low):
            return self.red(f"{_host(env.node)} could not be found ({reason})",
                            "check the address for a typo; if it is a campus-only name, "
                            "connect your VPN and run setup again")
        return self.red(f"{_host(env.node)} did not answer from this network ({reason})",
                        "if you are off the node's network (off campus), connect your VPN, "
                        f"then run `{env.cli} setup` again")


# --------------------------------------------------------------------------- 5. key


class KeyStep(Step):
    name = "key"
    title = "Signed in"
    needs = ("vault", "node")

    def _store(self, env: SetupEnv):
        from axiom.extensions.builtins.auth.node_signin import default_store

        return env.seams.get("store") or default_store()

    def check(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.auth.node_signin import key_name_for

        name = key_name_for(env.node)
        store = self._store(env)
        try:
            present = store.exists(name)
        except Exception as exc:  # noqa: BLE001
            return self.red(f"the vault could not be read ({type(exc).__name__})",
                            f"run `{env.cli} setup` again: the vault step repairs it")
        if not present:
            return self.red(f"no key for {_host(env.node)} on this computer yet",
                            f"run `{env.cli} setup` again: it opens your browser to sign in")
        try:
            with store.get(name) as secret:
                bearer = {"Authorization": "Bearer " + secret.as_str().strip()}
        except Exception as exc:  # noqa: BLE001
            return self.red(f"the key {name!r} could not be read from the vault ({type(exc).__name__})",
                            f"run `{env.cli} setup` again: the vault step repairs it")
        probe = env.seams.get("http_status", http_status)
        status, headers, reason = probe(env.node.rstrip("/") + "/gate/verify",
                                        timeout=env.timeout_s, headers=bearer)
        bearer.clear()
        if status == 200:
            who = headers.get("X-Axiom-User-Id") or headers.get("x-axiom-user-id") or "you"
            site = headers.get("X-Axiom-User-Site") or headers.get("x-axiom-user-site") or ""
            if site and not env.site:
                env.site = site
            return self.ok(f"{_host(env.node)} accepts your key as {who}" + (f" for {site}" if site else ""))
        if status in (401, 403):
            return self.red(f"{_host(env.node)} no longer accepts the key stored as {name!r}",
                            f"run `{env.cli} setup` again to sign in and replace it")
        if status in (404, 405):
            return self.ok(f"a key for {_host(env.node)} is stored (this node cannot confirm keys)")
        return self.red(f"could not ask {_host(env.node)} about the key ({reason or status})",
                        f"check the node line above, then run `{env.cli} setup` again")

    def plan(self, env: SetupEnv) -> str | None:
        if env.seams.get("invitation_code"):
            return f"present your invitation code to {_host(env.node)}; the key goes straight into your vault"
        return (f"open your browser to sign in to {_host(env.node)}; the key goes straight into "
                "your vault and is never shown")

    def apply(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.auth import node_signin

        store = self._store(env)
        code = env.seams.pop("invitation_code", "")
        if code:
            result = node_signin.redeem_invitation(
                env.node, code, store=store, post_json=env.seams.get("post_json", node_signin._post_json))
        else:
            kwargs = {k: env.seams[k] for k in ("open_browser", "post_json") if k in env.seams}
            result = node_signin.sign_in(env.node, site=env.site, store=store, say=env.say,
                                         timeout_s=env.signin_timeout_s, **kwargs)
        if not result.ok:
            return self.red(result.detail, result.remedy)
        if result.site and not env.site:
            env.site = result.site
        env.say(f"    key stored in your vault as {result.stored_as!r} (never shown)"
                + (f"; key id {result.key_id}" if result.key_id else ""))
        return self.check(env)


# --------------------------------------------------------------------------- 6. AI apps


_APP_LABELS = {
    "claude-code": "Claude Code", "claude-desktop": "Claude Desktop", "cursor": "Cursor",
    "windsurf": "Windsurf", "gemini": "Gemini CLI", "hermes": "Hermes", "opencode": "OpenCode",
    "vscode": "VS Code", "codex": "Codex",
}


class McpStep(Step):
    name = "mcp"
    title = "AI apps"

    def _found(self, env: SetupEnv) -> tuple[list[str], list[str]]:
        from axiom.extensions.builtins.mcp import install as mcp

        detected = env.seams.get("detect_tools", mcp.detect_tools)()
        found = [t for t, present in detected.items() if present]
        server = mcp.resolve_server()[0]
        missing = []
        for tool in found:
            spec = mcp.spec_for(tool)
            try:
                entry = mcp.read_entry(spec, server) if spec else None
            except Exception:  # noqa: BLE001 - an unreadable config counts as not connected
                entry = None
            if entry is None:
                missing.append(tool)
        return found, missing

    def check(self, env: SetupEnv) -> Line:
        found, missing = self._found(env)
        if not found:
            return self.skip("no AI apps found (Claude Code, Claude Desktop, VS Code, Cursor, Codex ...)")
        names = ", ".join(_APP_LABELS.get(t, t) for t in found)
        if not missing:
            return self.ok(f"connected over MCP: {names}")
        return self.red("not connected yet: " + ", ".join(_APP_LABELS.get(t, t) for t in missing),
                        f"run `{env.cli} setup` again and answer yes, or `{env.cli} mcp install`")

    def plan(self, env: SetupEnv) -> str | None:
        _found, missing = self._found(env)
        if not missing:
            return None
        return ("add the tool server to " + ", ".join(_APP_LABELS.get(t, t) for t in missing)
                + " (each app's own config file; restart the app afterwards)")

    def apply(self, env: SetupEnv) -> Line:
        from axiom.extensions.builtins.mcp import install as mcp

        _found, missing = self._found(env)
        out = env.seams.get("mcp_install", mcp.install)(tools=missing)
        broken = [t for t, r in (out.get("results") or {}).items() if r.get("action") == "error"]
        if broken:
            return self.red("could not update the config of " + ", ".join(broken),
                            f"run `{env.cli} mcp install` to see why")
        return self.check(env)


# --------------------------------------------------------------------------- the list


def builtin_steps() -> list[Step]:
    from axiom.cli.completion_setup import setup_step as completion_step

    # Tab completion comes after the shell block it shares a start-up file with,
    # and before anything that needs the node: it works offline.
    return [UpgradeStep(), VaultStep(), ShellStep(), completion_step(), NodeStep(), KeyStep(), McpStep()]


def brand_steps() -> list[Step]:
    """Steps the product adds (``setup_steps_fn`` on the brand). Never raises."""
    try:
        from axiom.infra.branding import get_branding

        hook = getattr(get_branding(), "setup_steps_fn", None)
        extra = hook() if callable(hook) else []
    except Exception:  # noqa: BLE001 - a brand must not break setup
        return []
    return [s for s in (extra or []) if isinstance(s, Step) and s.name and s.title]


def all_steps() -> list[Step]:
    """The platform's steps with the product's inserted after sign-in, before the apps."""
    base = builtin_steps()
    at = next(i for i, s in enumerate(base) if s.name == "key") + 1
    names = {s.name for s in base}
    extra = [s for s in brand_steps() if s.name not in names]
    return base[:at] + extra + base[at:]


__all__ = [
    "KeyStep",
    "Line",
    "McpStep",
    "NodeStep",
    "ShellStep",
    "Step",
    "UpgradeStep",
    "VaultStep",
    "all_steps",
    "brand_steps",
    "builtin_steps",
    "http_status",
]
