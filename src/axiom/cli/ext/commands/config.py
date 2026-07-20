# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext config`` — per-extension key/value configuration.

Config is stored as JSON under ``$AXIOM_HOME/config/<ext>.json`` (default
``~/.axiom/config/<ext>.json``). Values are strings; callers that need
richer types are expected to JSON-encode on their own side. The rationale
for a dedicated per-extension file rather than a shared ``config.toml``:

- Safe concurrent edits across parallel extensions (each file is its own
  write unit — no shared structure to merge).
- Uninstalling an extension is a single ``rm`` rather than a structured
  key purge.
- Trivial to inspect with ``cat`` / ``jq`` from a shell.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path

from axiom.cli.ext.provider import CliContext

# Subcommands for the ``axi ext config <ext> <op>`` parser.
_OPS: tuple[str, ...] = ("get", "set", "list", "unset")


def _axiom_home() -> Path:
    """Return the user-level Axiom config root.

    Resolution: ``AXIOM_HOME`` env var wins; otherwise ``~/.axiom``.

    The darwin default is the same as linux — a plain ``~/.axiom`` rather
    than the XDG or macOS ``Application Support`` conventions. Simpler,
    matches the rest of the project's ``get_user_state_dir()`` pattern
    (which uses ``~/.<cli_name>``), and keeps shell muscle-memory (``cd
    ~/.axiom/config``) portable across platforms.
    """
    override = os.environ.get("AXIOM_HOME")
    if override:
        return Path(override)
    return Path.home() / ".axiom"


def _config_path_for(ext_name: str) -> Path:
    return _axiom_home() / "config" / f"{ext_name}.json"


def _load_config(ext_name: str) -> dict[str, str]:
    path = _config_path_for(ext_name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_config(ext_name: str, data: dict[str, str]) -> None:
    path = _config_path_for(ext_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_ext_from_cwd(cwd: Path) -> str | None:
    manifest = cwd / "axiom-extension.toml"
    if not manifest.exists():
        return None
    try:
        with manifest.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return None
    name = data.get("extension", {}).get("name")
    if isinstance(name, str) and name:
        return name
    return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ConfigProvider:
    """Built-in provider for ``axi ext config <ext> <op> [args...]``.

    The ``<ext>`` positional is optional: if the current working directory
    is an extension (``axiom-extension.toml`` present), the manifest's
    ``name`` is used as the default.
    """

    verb = "config"
    description = "Manage per-extension key/value configuration"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        # We accept a flat list of positionals and decode their shape in run().
        # argparse subparsers would give a cleaner surface, but the spec
        # asks for `axi ext config <ext> [get <key> | set <key> <value> |
        # list | unset <key>]` — with an optional ``<ext>`` — which is
        # awkward to express with nested subparsers while keeping the
        # Provider interface uniform.
        parser.add_argument(
            "args",
            nargs="*",
            help="<ext> <op> [args...] — ext defaults to the cwd extension",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit `list` output as JSON",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        tokens: list[str] = list(args.args or [])
        if not tokens:
            print("axi ext config: missing operation (get/set/list/unset)")
            return 2

        # The first token is either an op (cwd resolution kicks in) or an
        # extension name (next token must be an op).
        if tokens[0] in _OPS:
            ext_name = _resolve_ext_from_cwd(context.cwd)
            if ext_name is None:
                print(
                    "axi ext config: no extension specified and current directory "
                    "is not an extension (no axiom-extension.toml); "
                    "pass the extension name explicitly"
                )
                return 2
            op = tokens[0]
            rest = tokens[1:]
        else:
            ext_name = tokens[0]
            if len(tokens) < 2 or tokens[1] not in _OPS:
                print(
                    f"axi ext config: unknown operation "
                    f"{tokens[1] if len(tokens) > 1 else '(missing)'!r}; "
                    f"expected one of {', '.join(_OPS)}"
                )
                return 2
            op = tokens[1]
            rest = tokens[2:]

        if op == "get":
            return self._op_get(ext_name, rest)
        if op == "set":
            return self._op_set(ext_name, rest)
        if op == "list":
            return self._op_list(ext_name, as_json=args.as_json)
        if op == "unset":
            return self._op_unset(ext_name, rest)
        return 2  # unreachable — tokens[1] already validated

    # -- operations ---------------------------------------------------------

    def _op_get(self, ext_name: str, rest: list[str]) -> int:
        if len(rest) != 1:
            print("axi ext config: `get` requires exactly one <key> argument")
            return 2
        data = _load_config(ext_name)
        key = rest[0]
        if key not in data:
            print(f"axi ext config: {ext_name}: key {key!r} is not set")
            return 1
        print(data[key])
        return 0

    def _op_set(self, ext_name: str, rest: list[str]) -> int:
        if len(rest) != 2:
            print("axi ext config: `set` requires <key> <value>")
            return 2
        key, value = rest
        data = _load_config(ext_name)
        data[key] = value
        _save_config(ext_name, data)
        print(f"axi ext config: {ext_name}: set {key}={value}")
        return 0

    def _op_list(self, ext_name: str, *, as_json: bool) -> int:
        data = _load_config(ext_name)
        if as_json:
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
        if not data:
            print(f"axi ext config: {ext_name}: (no keys set)")
            return 0
        width = max(len(k) for k in data)
        for key in sorted(data):
            print(f"  {key.ljust(width)}  {data[key]}")
        return 0

    def _op_unset(self, ext_name: str, rest: list[str]) -> int:
        if len(rest) != 1:
            print("axi ext config: `unset` requires exactly one <key> argument")
            return 2
        key = rest[0]
        data = _load_config(ext_name)
        if key in data:
            del data[key]
            _save_config(ext_name, data)
            print(f"axi ext config: {ext_name}: unset {key}")
        else:
            # Idempotent — unsetting a missing key is a no-op.
            print(f"axi ext config: {ext_name}: {key} was not set")
        return 0


__all__ = ["ConfigProvider"]
