# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What this node is for, and therefore what it shows (ADR-164).

A node declares the functions it performs. Until it does, it is a general
install and shows everything, which is what every existing install expects.
Once it does, the command line shows the commands those functions own plus the
few every node needs to look after itself, background agents stay off, and the
rest is one ``features enable`` away. Nothing is uninstalled: hiding is a
statement about the surface, so turning a feature on later needs no reinstall.

The declaration lives in a small file in the node's state directory::

    [node]
    role = "collector"                       # a label people read
    functions = ["acquire", "transmit"]      # ADR-164 vocabulary
    features = ["chat"]                      # turned on later, by name

    [settings]                               # free-form, for the role's owner
    config = "/srv/site/site.toml"

Products define named roles (a preset function set) through the
``axiom.node_roles`` entry-point group; this module only knows the vocabulary.
A command says which functions own it with ``functions = [...]`` on its
``[[extension.provides]]`` ``cmd`` entry. A command no function owns is hidden
on a confined node and can be enabled by its own name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

#: The ADR-164 vocabulary. Consumer layers fill these in; they do not add to it.
FUNCTIONS: tuple[str, ...] = (
    "acquire",
    "transmit",
    "control",
    "ingest",
    "data_platform",
    "front_end",
    "assistant",
)

#: What every node needs whatever it is for: to say who and what it is, how it
#: is doing, to diagnose and update itself, to hold its own secrets, and to turn
#: other things on.
ALWAYS_VISIBLE: frozenset[str] = frozenset(
    {
        "whoami",
        "status",
        "doctor",
        "dr",
        "update",
        "secrets",
        "vault",
        "features",
        "help",
        "version",
        "topology",
    }
)

#: Background agents are a feature like any other on a confined node: off
#: until someone turns them on, so nothing unexplained starts on the machine.
AGENTS_FEATURE = "background-agents"

CONFIG_ENV = "AXIOM_NODE_CONFIG"
CONFIG_FILE = "node.toml"


class UnknownFeature(ValueError):
    """A function or command name nobody on this node defines."""


class RoleFunction(ValueError):
    """An attempt to switch off a function the role itself declares."""


@dataclass(frozen=True)
class NodeConfig:
    role: str = ""
    functions: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)
    #: Why the file could not be read, when it could not. A broken file must
    #: not lock anyone out of their own node, so it reads as unconfigured and
    #: says so rather than refusing every command.
    problem: str = ""

    @property
    def confined(self) -> bool:
        return bool(self.functions)

    @property
    def active_functions(self) -> frozenset[str]:
        return frozenset(self.functions) | {f for f in self.features if f in FUNCTIONS}


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / CONFIG_FILE


def load(path: Path | None = None) -> NodeConfig:
    """The node's declaration, or an unconfined node when there is none."""
    path = path or config_path()
    if not path.is_file():
        return NodeConfig()
    try:
        import tomllib

        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return NodeConfig(problem=f"{path} could not be read ({exc}); showing everything")
    node = doc.get("node") or {}
    return NodeConfig(
        role=str(node.get("role") or ""),
        functions=tuple(str(f) for f in node.get("functions") or ()),
        features=tuple(str(f) for f in node.get("features") or ()),
        settings=dict(doc.get("settings") or {}),
    )


def save(cfg: NodeConfig, path: Path | None = None) -> NodeConfig:
    import tomlkit

    path = path or config_path()
    doc = tomlkit.document()
    doc.add(tomlkit.comment("What this node is for (ADR-164). Edit with `features`."))
    node = tomlkit.table()
    if cfg.role:
        node["role"] = cfg.role
    node["functions"] = list(cfg.functions)
    node["features"] = list(cfg.features)
    doc["node"] = node
    if cfg.settings:
        settings = tomlkit.table()
        for key, value in cfg.settings.items():
            settings[key] = value
        doc["settings"] = settings
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)
    return load(path)


def _check_functions(functions: Iterable[str]) -> tuple[str, ...]:
    out = tuple(dict.fromkeys(str(f) for f in functions))
    unknown = [f for f in out if f not in FUNCTIONS]
    if unknown:
        raise UnknownFeature(
            f"not a node function: {', '.join(unknown)}; the functions are {', '.join(FUNCTIONS)}"
        )
    return out


def apply(
    *,
    role: str,
    functions: Iterable[str],
    settings: Mapping[str, Any] | None = None,
    path: Path | None = None,
) -> NodeConfig:
    """Give the node a role. Features already turned on stay on."""
    current = load(path)
    merged = dict(current.settings)
    merged.update(settings or {})
    cfg = replace(
        current,
        role=role,
        functions=_check_functions(functions),
        settings=merged,
        problem="",
    )
    return save(cfg, path)


def clear(path: Path | None = None) -> NodeConfig:
    """Back to a general install: everything shows. Settings are kept."""
    current = load(path)
    return save(replace(current, role="", functions=(), features=(), problem=""), path)


def enable(name: str, *, known: Mapping[str, Any], path: Path | None = None) -> NodeConfig:
    """Turn on a function, a command or background agents, by name."""
    if name not in FUNCTIONS and name not in known and name != AGENTS_FEATURE:
        raise UnknownFeature(f"nothing on this node is called {name!r}")
    current = load(path)
    if name in current.features or name in current.functions:
        return current
    return save(replace(current, features=current.features + (name,)), path)


def disable(name: str, *, path: Path | None = None) -> NodeConfig:
    current = load(path)
    if name in current.functions:
        raise RoleFunction(
            f"{name!r} is part of this node's role ({current.role or 'unnamed'}); "
            "change the role instead"
        )
    return save(replace(current, features=tuple(f for f in current.features if f != name)), path)


def _owners(info: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(info.get("functions") or ())


def allowed(cfg: NodeConfig, noun: str, info: Mapping[str, Any]) -> bool:
    """Whether ``noun`` is shown and runs on this node."""
    if not cfg.confined:
        return True
    if noun in ALWAYS_VISIBLE or noun in cfg.features:
        return True
    return bool(_owners(info) & cfg.active_functions)


def background_services_allowed(cfg: NodeConfig) -> bool:
    return not cfg.confined or AGENTS_FEATURE in cfg.features


@dataclass(frozen=True)
class FeatureRow:
    name: str
    kind: str  # "function" | "command" | "service"
    enabled: bool
    source: str  # "role" | "feature" | "always" | "" (off)
    commands: tuple[str, ...] = ()


def feature_rows(cfg: NodeConfig, commands: Mapping[str, Mapping[str, Any]]) -> list[FeatureRow]:
    """Everything that can be turned on, and whether it is."""
    rows: list[FeatureRow] = []
    owned: set[str] = set()
    commands = {n: i for n, i in commands.items() if n}
    for fn in FUNCTIONS:
        nouns = tuple(sorted(n for n, i in commands.items() if fn in _owners(i)))
        owned.update(nouns)
        if fn in cfg.functions:
            src = "role"
        elif fn in cfg.features:
            src = "feature"
        else:
            src = "" if cfg.confined else "always"
        rows.append(FeatureRow(fn, "function", bool(src), src, nouns))
    for noun in sorted(set(commands) - owned):
        if noun in ALWAYS_VISIBLE:
            src = "always"
        elif noun in cfg.features:
            src = "feature"
        else:
            src = "" if cfg.confined else "always"
        rows.append(FeatureRow(noun, "command", bool(src), src, (noun,)))
    agents = "feature" if AGENTS_FEATURE in cfg.features else ("" if cfg.confined else "always")
    rows.append(FeatureRow(AGENTS_FEATURE, "service", bool(agents), agents))
    return rows


@dataclass(frozen=True)
class RoleDef:
    name: str
    functions: tuple[str, ...]
    description: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)


def roles() -> dict[str, RoleDef]:
    """Named roles products publish through the ``axiom.node_roles`` group.

    Each entry point resolves to a callable returning
    ``{name: {"functions": [...], "description": "..."}}``. A broken provider is
    skipped: one product's mistake must not take every role away.
    """
    from importlib.metadata import entry_points

    out: dict[str, RoleDef] = {}
    for ep in entry_points(group="axiom.node_roles"):
        try:
            provided = ep.load()()
        except Exception:  # noqa: BLE001
            continue
        for name, spec in dict(provided or {}).items():
            try:
                out[name] = RoleDef(
                    name=name,
                    functions=_check_functions(spec.get("functions") or ()),
                    description=str(spec.get("description") or ""),
                    settings=dict(spec.get("settings") or {}),
                )
            except (UnknownFeature, AttributeError):
                continue
    return out


__all__ = [
    "AGENTS_FEATURE",
    "ALWAYS_VISIBLE",
    "CONFIG_ENV",
    "FUNCTIONS",
    "FeatureRow",
    "NodeConfig",
    "RoleDef",
    "RoleFunction",
    "UnknownFeature",
    "allowed",
    "apply",
    "background_services_allowed",
    "clear",
    "config_path",
    "disable",
    "enable",
    "feature_rows",
    "load",
    "roles",
    "save",
]
