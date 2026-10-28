# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Would an MCP server started by a desktop app find the vault and the binding?

A desktop AI app or an editor starts the MCP server without the person's shell, so nothing a shell
profile exports reaches it. A terminal check proves nothing about that process: the terminal has
the variables. This probe starts a fresh interpreter with only what such an app passes (``HOME``,
a bare ``PATH`` and the env block the installer writes into the client's config) and asks it, in
its own words, whether the vault and the site binding resolve. It never prints a secret: the child
reports where each setting came from and whether a key resolved, not what it is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

#: Run inside the clean interpreter. Prints one JSON object; never a value.
_PROBE = r"""
import json
out = {}
try:
    from axiom.extensions.builtins.secrets import vault_settings as vs
    r = vs.describe()
    out["vault"] = {"backend": r.backend, "backend_from": r.backend_from, "url_from": r.url_from,
                    "token_from": r.token_from, "settings_file": r.settings_file}
except Exception as exc:
    out["vault"] = {"error": type(exc).__name__ + ": " + str(exc)[:200]}
try:
    from urllib.parse import urlparse
    from axiom.extensions.builtins.data_platform import read_target as rt
    bound = rt.current_binding()
    if bound is None:
        out["binding"] = None
    else:
        site, face = bound
        out["binding"] = {"site": site, "host": urlparse(face).hostname or face,
                          "token_ref_set": bool(rt._binding_token_ref()),
                          "key_resolves": bool(rt.bound_key_for(face))}
except Exception as exc:
    out["binding"] = {"error": type(exc).__name__ + ": " + str(exc)[:200]}
print(json.dumps(out))
"""

#: The only variables a desktop app can be counted on to pass to a server it starts.
_PASSED_THROUGH = ("HOME", "USER", "LOGNAME", "TMPDIR")


@dataclass(frozen=True)
class Row:
    name: str
    ok: bool
    status: str
    fix: str = ""


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """What a desktop app gives the MCP server: a few login variables and the installer's env block."""
    env = {k: os.environ[k] for k in _PASSED_THROUGH if os.environ.get(k)}
    env["PATH"] = "/usr/bin:/bin"
    try:
        from .install import server_env

        env.update(server_env())
    except Exception:  # noqa: BLE001 - the probe still runs without the block
        pass
    env.update(extra or {})
    return env


def probe(env: dict[str, str] | None = None, *, python: str | None = None, timeout: float = 60.0) -> dict:
    """Run the probe in a fresh interpreter under ``env`` (default :func:`clean_env`)."""
    completed = subprocess.run(  # noqa: S603 - our own interpreter and our own probe text
        [python or sys.executable, "-c", _PROBE],
        env=env if env is not None else clean_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    for line in reversed(completed.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    tail = (completed.stderr or "").strip().splitlines()[-1:] or ["no output"]
    return {"error": f"the probe did not run: {tail[0][:200]}"}


def rows(result: dict, *, cli: str = "axi") -> list[Row]:
    """The probe's answer as rows a person can act on."""
    if "error" in result:
        return [Row("MCP server start", False, result["error"], f"run `{cli} mcp install` again")]
    out: list[Row] = []
    older = f"the platform this app starts is older than this check: `{cli} update`, then `{cli} mcp install`"
    vault = result.get("vault") or {}
    fix_vault = (
        f"`{cli} secrets vault --backend openbao --url <vault url> --token-file <0600 file>` "
        "(never put the token in an MCP client's config)"
    )
    if "error" in vault:
        out.append(Row("Vault", False, vault["error"], older if "ImportError" in vault["error"] else fix_vault))
    else:
        backend = vault.get("backend") or "the OS keychain"
        out.append(Row("Vault backend", True, f"{backend} (from {vault.get('backend_from')})"))
        if vault.get("backend") == "openbao":
            token_from = str(vault.get("token_from") or "none")
            ok = token_from in ("env", "config")
            out.append(Row("Vault token", ok, f"from {token_from}", "" if ok else fix_vault))
    binding = result.get("binding")
    if binding is None:
        out.append(Row("Site binding", True, "not bound (nothing to follow)"))
    elif "error" in binding:
        out.append(Row("Site binding", False, binding["error"], older if "ImportError" in binding["error"] else ""))
    else:
        out.append(Row("Site binding", True, f"{binding['site']} at {binding['host']}"))
        ok = bool(binding.get("key_resolves"))
        status = "resolves" if ok else (
            "does not resolve" if binding.get("token_ref_set") else "no token_ref and no stored key"
        )
        out.append(Row("Binding key", ok, status, "" if ok else fix_vault))
    return out


__all__ = ["Row", "clean_env", "probe", "rows"]
