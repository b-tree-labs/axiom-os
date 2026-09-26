# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Extension lifecycle logic for `neut ext activate` (ADR-005 rung 4) and
`neut ext validate --env` (rung 5) — pure, fake-injectable core + the real
in-env actuators/probes. Rehomed into Axiom's ext CLI (was a neutron builtin
that collided with `neut ext`; ADR-005 reconciliation, option a)."""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol



# --- activation (rung 4) ---
@dataclass
class Step:
    name: str
    present: bool
    fix: str
    repairable: bool = True


@dataclass
class ActivationResult:
    ok: bool
    env: str
    kind: str
    name: str
    steps: list[Step]
    messages: list[str] = field(default_factory=list)


class ConnectorRegistry(Protocol):
    def is_registered(self, connector: str) -> bool: ...
    def register(self, connector: str, schema_ref: str) -> None: ...
    def has_mapping(self, connector: str) -> bool: ...
    def add_mapping(self, connector: str, site: str) -> None: ...


class Systemd(Protocol):
    def is_armed(self, unit: str) -> bool: ...
    def arm(self, unit: str) -> None: ...


class Raci(Protocol):
    def check(self, action: str) -> str: ...  # "approve" | "consulted" | "informed"


class Herald(Protocol):
    def publish(self, *, intent: str, summary: str, priority: str = "normal") -> None: ...


def read_manifest(ext_dir: str | Path) -> tuple[str, str | None]:
    m = tomllib.loads((Path(ext_dir) / "axiom-extension.toml").read_text())
    ext = m.get("extension", {})
    return ext.get("name", ""), ext.get("kind")


def _required_steps(kind: str, name: str, registry: ConnectorRegistry, systemd: Systemd, site: str) -> list[Step]:
    connector, schema_ref = name, f"{name}/v1"
    steps: list[Step] = []
    if kind in ("conform", "ingestion"):
        steps.append(Step("connector-registered", registry.is_registered(connector),
                          f"axi data register {connector} push --bronze-root ~/.axi/bronze --schema-ref {schema_ref}"))
        steps.append(Step("conform-mapping", registry.has_mapping(connector),
                          f'add "{connector}": "{site}" to SITE_BY_CONNECTOR in conform_signals.py',
                          repairable=False))  # a controlled edit to site config — guided, never silent
    if kind in ("alerting", "analytics"):
        steps.append(Step("timer-armed", systemd.is_armed(f"{name}.timer"),
                          f"systemctl --user enable --now {name}.timer"))
    return steps


def activate(ext_dir: str | Path, env: str, *, registry: ConnectorRegistry, systemd: Systemd,
             raci: Raci, herald: Herald, site: str = "ut-triga", check_only: bool = False) -> ActivationResult:
    name, kind = read_manifest(ext_dir)
    if not kind:
        raise ValueError("extension manifest has no [extension].kind — scaffold with `neut ext new`")
    steps = _required_steps(kind, name, registry, systemd, site)
    res = ActivationResult(ok=False, env=env, kind=kind, name=name, steps=steps)
    missing = [s for s in steps if not s.present]

    if env == "prod":
        mode = raci.check("extension.promote")
        if mode == "approve":
            res.messages.append(f"RACI=approve for extension.promote — prod activation of {name} needs human "
                                "approval first (`axi approve`); NOT proceeding. Nothing changed.")
            return res
        if mode == "consulted":
            res.messages.append("RACI=consulted — proceeding; surfacing to a human via HERALD.")
        herald.publish(intent="extension.activate", summary=f"activating {name} ({kind}) in prod", priority="high")

    if check_only:
        res.ok = not missing
        if missing:
            res.messages.append(f"PRE-FLIGHT FAILED — {name} ({kind}) is missing {len(missing)} activation step(s):")
            res.messages += [f"  x {s.name} — fix: {s.fix}" for s in missing]
        else:
            res.messages.append(f"pre-flight clean — {name} ({kind}) has every activation step")
        return res

    for s in missing:
        if not s.repairable:
            res.messages.append(f"  x {s.name} NOT auto-repairable — fix: {s.fix}")
            continue
        if s.name == "connector-registered":
            registry.register(name, f"{name}/v1")
            res.messages.append(f"  + registered connector {name}")
        elif s.name == "conform-mapping":
            registry.add_mapping(name, site)
            res.messages.append(f"  + mapped {name} -> {site}")
        elif s.name == "timer-armed":
            systemd.arm(f"{name}.timer")
            res.messages.append(f"  + armed {name}.timer")

    steps2 = _required_steps(kind, name, registry, systemd, site)
    res.steps = steps2
    still = [s for s in steps2 if not s.present]
    res.ok = not still
    if still:
        res.messages.append(f"ACTIVATION INCOMPLETE — {name} still missing:")
        res.messages += [f"  x {s.name} — fix: {s.fix}" for s in still]
    else:
        res.messages.append(f"activated {name} ({kind}) in {env} — all steps present")
    return res


# --- validation smoke (rung 5) ---
@dataclass
class ValidationResult:
    ok: bool
    env: str
    kind: str
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, passed, detail)
    messages: list[str] = field(default_factory=list)


class Prober(Protocol):
    def conform_funnel(self, schema_ref: str) -> dict: ...      # {"unknown_schema": {...}, "rows_out": int}
    def deadletter_count(self, connector: str) -> int: ...
    def parity_mismatches(self, connector: str) -> int: ...
    def replay_alert_count(self, ext_dir: str) -> int: ...
    def verb_returns(self, name: str) -> bool: ...


def _read(ext_dir):
    m = tomllib.loads((Path(ext_dir) / "axiom-extension.toml").read_text())
    e = m.get("extension", {})
    return e.get("name", ""), e.get("kind")


def validate(ext_dir: str | Path, env: str, *, prober: Prober, herald: Herald) -> ValidationResult:
    name, kind = _read(ext_dir)
    res = ValidationResult(ok=False, env=env, kind=kind or "?", name=name)
    if kind in ("conform",):
        f = prober.conform_funnel(f"{name}/v1")
        unk = f.get("unknown_schema") or {}
        res.checks.append(("schema-recognized", not unk, f"unknown_schema={unk}"))
        res.checks.append(("rows-conformed", int(f.get("rows_out", 0)) > 0, f"rows_out={f.get('rows_out', 0)}"))
    elif kind == "ingestion":
        dl = prober.deadletter_count(name)
        res.checks.append(("zero-deadletter", dl == 0, f"deadletter={dl}"))
        pm = prober.parity_mismatches(name)
        res.checks.append(("parity-clean", pm == 0, f"mismatches={pm}"))
    elif kind == "alerting":
        n = prober.replay_alert_count(str(ext_dir))
        res.checks.append(("replay-fires", n > 0, f"alerts_on_replay={n}"))
    elif kind == "analytics":
        ok = prober.verb_returns(name)
        res.checks.append(("verb-returns", bool(ok), f"returns={ok}"))
    else:
        res.checks.append(("known-kind", False, f"unknown kind {kind!r}"))

    res.ok = bool(res.checks) and all(passed for _, passed, _ in res.checks)
    verdict = "PASSED" if res.ok else "FAILED"
    res.messages.append(f"{name} ({kind}) validation {verdict} in {env}:")
    res.messages += [f"  {'ok ' if p else 'X  '}{n} — {d}" for n, p, d in res.checks]
    herald.publish(intent="extension.validate",
                   summary=f"{name} ({kind}) validation {verdict} in {env}",
                   priority="normal" if res.ok else "high")
    return res


# --- real in-env actuators/probes ---
class RealRegistry:
    def __init__(self, bronze_root: str = "~/.axi/bronze",
                 connectors_dir: str = "~/.axi/plinth/connectors",
                 conform_signals: str | None = None) -> None:
        self.bronze_root = os.path.expanduser(bronze_root)
        self.cdir = Path(os.path.expanduser(connectors_dir))
        self.conform_signals = conform_signals or os.path.expanduser("~/.config/axiom-serving/conform_signals.py")

    def is_registered(self, connector: str) -> bool:
        return (self.cdir / f"{connector}.toml").exists()

    def register(self, connector: str, schema_ref: str) -> None:
        subprocess.run(["axi", "data", "register", "--bronze-root", self.bronze_root,
                        "--rag-dsn-env", "DP1_RAG_DSN", "--default-disposition", "allow",
                        "--default-tier", "restricted", connector, "push", "--schema-ref", schema_ref],
                       check=True)

    def has_mapping(self, connector: str) -> bool:
        p = Path(self.conform_signals)
        return p.exists() and f'"{connector}"' in p.read_text()

    def add_mapping(self, connector: str, site: str) -> None:  # guided, never auto (step is repairable=False)
        raise NotImplementedError(f'add "{connector}": "{site}" to SITE_BY_CONNECTOR in {self.conform_signals}')


class RealSystemd:
    def is_armed(self, unit: str) -> bool:
        try:
            return subprocess.run(["systemctl", "--user", "is-enabled", unit],
                                  capture_output=True, text=True).returncode == 0
        except Exception:
            return False

    def arm(self, unit: str) -> None:
        subprocess.run(["systemctl", "--user", "enable", "--now", unit], check=True)


class RealRaci:
    def check(self, action: str) -> str:
        try:
            from axiom.infra.raci import check_raci
            return check_raci(action)
        except Exception:
            return "approve"  # fail-safe: the most conservative lane


class RealHerald:
    def publish(self, *, intent: str, summary: str, priority: str = "normal") -> None:
        try:
            from axiom.extensions.builtins.data_platform._herald import publish_event
            publish_event(intent=intent, summary=summary, priority=priority)
        except Exception as e:  # noqa: BLE001
            print(f"  (HERALD unavailable: {type(e).__name__} — would publish [{priority}] {summary})")


class RealProber:
    """Validation probes against the env DSN. `neut ext validate` is meant to run
    in the target env; off-env these raise a clear, loud error rather than a false pass."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL") or os.environ.get("AXIOM_DB_URL")

    def _need_dsn(self) -> str:
        if not self.dsn:
            raise RuntimeError("no DSN — run `neut ext validate` in the target env (set DATABASE_URL)")
        return self.dsn

    def _psql(self, sql: str) -> str:
        import subprocess
        r = subprocess.run(["psql", self._need_dsn(), "-Atc", sql], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed: {r.stderr.strip()[:200]}")
        return r.stdout.strip()

    def conform_funnel(self, schema_ref: str) -> dict:
        # rows present in silver for this schema_ref = it conformed; 0 = unknown/failed
        n = int(self._psql(f"SELECT count(*) FROM silver.signals WHERE schema_ref = '{schema_ref}'") or 0)
        return {"unknown_schema": {} if n > 0 else {schema_ref: "no rows"}, "rows_out": n}

    def deadletter_count(self, connector: str) -> int:
        raise RuntimeError("deadletter probe is env-specific; run in target env")

    def parity_mismatches(self, connector: str) -> int:
        raise RuntimeError("parity probe is env-specific; run in target env")

    def replay_alert_count(self, ext_dir: str) -> int:
        raise RuntimeError("replay probe runs the monitor's check(now=past); run in target env")

    def verb_returns(self, name: str) -> bool:
        raise RuntimeError("verb probe is env-specific; run in target env")


def real_deps() -> dict:
    return {"registry": RealRegistry(), "systemd": RealSystemd(),
            "raci": RealRaci(), "herald": RealHerald(), "prober": RealProber()}
