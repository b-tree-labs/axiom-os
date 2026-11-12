# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Do the node's local models agree with each other? (ADR-140)

Three things name a local model on a node, and nothing held them together:
the pin in :mod:`axiom.llm.local_models` (what the code resolves a role to),
the unit that ensures models are pulled into Ollama (what the node has), and
the serving env files that tell each service which model to call. On one node
the unit pulled a model no role resolves to, the env named the pinned one, and
the pulled model did not fit on the GPU beside the big engine, so the "quick"
tier answered in twelve seconds. Nobody was told, because nothing compared them.

This module reads all three, plus what Ollama actually has, and says where they
disagree. Everything is a pure function over text and JSON, so a test can hand
it a unit file, an env file and a tags listing and ask what it would say.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from axiom.llm.local_models import EMBED, ENDPOINT, OLLAMA_ENSURE_MODELS, resolve

#: An Ollama tag: ``family:variant``. A gateway alias (``qwen``), a served-model
#: name (``site-llm-v0.31``) or a URL is not one and is not judged here.
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$")
_PULL = re.compile(r"^\s*ExecStart(?:Pre|Post)?=.*?\bollama\s+pull\s+(\S+)", re.M)
_ENV_MODEL = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*)\s*=\s*(\S*)", re.M)

#: Env files that are not live configuration.
_NOT_LIVE = (".bak", ".example", ".pre", ".orig", "~")


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    fix: str | None = None


@dataclass
class Alignment:
    pins: dict[str, str]
    ensured: dict[str, str]  # model -> unit file that pulls it
    declared: list[tuple[str, str, str]]  # (file, variable, model)
    installed: set[str] | None  # None: Ollama could not be asked
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pins": dict(self.pins),
            "ensured": dict(self.ensured),
            "declared": [list(d) for d in self.declared],
            "installed": sorted(self.installed) if self.installed is not None else None,
            "findings": [asdict(f) for f in self.findings],
        }


def norm(name: str) -> str:
    """``nomic-embed-text:latest`` is the model ``nomic-embed-text``."""
    n = name.strip().strip("\"'")
    return n[: -len(":latest")] if n.endswith(":latest") else n


def pinned(settings=None) -> dict[str, str]:
    """What each role resolves to on this deployment (settings win over defaults)."""
    return {
        "quick": norm(resolve("quick", settings)),
        "reasoning": norm(resolve("reasoning", settings)),
        "embed": norm(EMBED),
    }


def ensured_by_units(unit_dirs: Iterable[Path]) -> dict[str, str]:
    """Models that systemd units pull into Ollama, with the unit that pulls each."""
    out: dict[str, str] = {}
    for d in unit_dirs:
        try:
            files = sorted(Path(d).glob("*.service"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for m in _PULL.finditer(text):
                out.setdefault(norm(m.group(1)), str(f))
    return out


def declared_in_env(env_dir: Path) -> list[tuple[str, str, str]]:
    """``*_MODEL=`` variables in live env files whose value is an Ollama tag."""
    out: list[tuple[str, str, str]] = []
    try:
        files = sorted(Path(env_dir).glob("*.env"))
    except OSError:
        return out
    for f in files:
        if any(marker in f.name for marker in _NOT_LIVE):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in _ENV_MODEL.finditer(text):
            value = norm(m.group(2))
            if "/" in value or not _TAG.match(value + (":x" if ":" not in value else "")):
                continue
            if ":" not in value:
                continue
            out.append((str(f), m.group(1), value))
    return out


def installed_models(tags: dict[str, Any]) -> set[str]:
    """The models an Ollama ``/api/tags`` listing says are present."""
    return {norm(m.get("name") or m.get("model") or "") for m in tags.get("models", []) if m}


def fetch_tags(ollama_url: str = ENDPOINT, timeout: float = 3.0) -> dict[str, Any]:
    with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def assess(
    pins: dict[str, str],
    ensured: dict[str, str],
    declared: list[tuple[str, str, str]],
    installed: set[str] | None,
) -> list[Finding]:
    findings: list[Finding] = []
    pin_set = set(pins.values())
    roles_of = {m: ", ".join(r for r, mm in pins.items() if mm == m) for m in pin_set}
    pin_text = ", ".join(f"{r}={m}" for r, m in pins.items())

    if installed is None:
        findings.append(
            Finding(
                "info",
                "ollama-unreachable",
                "Ollama could not be asked which models it has; the installed side was not checked",
            )
        )
    else:
        for role, model in pins.items():
            if model not in installed:
                findings.append(
                    Finding(
                        "error",
                        "pin-not-installed",
                        f"the {role} role resolves to {model}, which Ollama does not have",
                        f"ollama pull {model}",
                    )
                )

    if not ensured:
        findings.append(
            Finding(
                "info",
                "no-ensure-unit",
                "no systemd unit pulls Ollama models on this node; the pins are not ensured at boot",
            )
        )
    else:
        for model in sorted(pin_set - set(ensured)):
            findings.append(
                Finding(
                    "warning",
                    "ensure-unit-missing-pin",
                    f"no unit pulls {model} (the {roles_of[model]} pin); the node has it only if somebody pulled it by hand",
                    f"add `ollama pull {model}` to the unit that ensures models (local_models.OLLAMA_ENSURE_MODELS)",
                )
            )
        for model in sorted(set(ensured) - pin_set):
            findings.append(
                Finding(
                    "warning",
                    "ensure-unit-pulls-unpinned",
                    f"{Path(ensured[model]).name} pulls {model}, which no role resolves to ({pin_text})",
                    f"remove `ollama pull {model}` from {ensured[model]}, or move the pin (ADR-140 addendum) so the roles follow",
                )
            )

    for file, var, model in declared:
        if model in pin_set:
            continue
        where = f"{Path(file).name}:{var}={model}"
        if installed is not None and model not in installed:
            findings.append(
                Finding(
                    "error",
                    "env-model-not-installed",
                    f"{where} names a model Ollama does not have; that service fails at first call",
                    f"set {var} to a pinned model ({pin_text}) or `ollama pull {model}`",
                )
            )
        else:
            findings.append(
                Finding(
                    "warning",
                    "env-model-off-pin",
                    f"{where} is not a pinned role model ({pin_text})",
                    f"a deployment override belongs in settings (routing.ollama_model), or align {var} with the pin",
                )
            )

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f.severity])
    return findings


def check(
    *,
    settings=None,
    unit_dirs: Iterable[Path] | None = None,
    env_dir: Path | None = None,
    ollama_url: str = ENDPOINT,
    tags: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
) -> Alignment:
    """Read the three sources and Ollama, and say where they disagree.

    ``tags`` may be a listing, a callable that returns one, or ``None`` to ask
    ``ollama_url``. An Ollama that cannot be reached is reported, not guessed at.
    """
    home = Path.home()
    unit_dirs = (
        list(unit_dirs)
        if unit_dirs is not None
        else [home / ".config/systemd/user", Path("/etc/systemd/system")]
    )
    env_dir = env_dir if env_dir is not None else home / ".config/axiom-serving"
    installed: set[str] | None
    try:
        listing = (
            tags() if callable(tags) else (tags if tags is not None else fetch_tags(ollama_url))
        )
        installed = installed_models(listing)
    except Exception:  # noqa: BLE001 - an unreachable daemon is a finding, not a crash
        installed = None
    pins = pinned(settings)
    ensured = ensured_by_units(unit_dirs)
    declared = declared_in_env(env_dir)
    a = Alignment(pins=pins, ensured=ensured, declared=declared, installed=installed)
    a.findings = assess(pins, ensured, declared, installed)
    return a


__all__ = [
    "Alignment",
    "Finding",
    "OLLAMA_ENSURE_MODELS",
    "assess",
    "check",
    "declared_in_env",
    "ensured_by_units",
    "fetch_tags",
    "installed_models",
    "norm",
    "pinned",
]
