# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SkillRegistry`` — runtime invocation layer for AEOS skills (ADR-056).

The registry is the channel agents + CLI verbs share. A skill is a
plain Python function::

    def my_skill(params: dict, ctx: SkillContext) -> SkillResult: ...

Registration is name → callable, namespaced under the providing
extension's CLI noun (``data.install``, ``hygiene.prune``). Invocation
is uniform across callers — CLI verbs become thin wrappers, agent
personas reach the same surface, and any skill can invoke any other
skill via ``ctx.registry.invoke(...)``.

Layering (see ADR-056):

- **Agent personas** (LLM characters: TIDY, PLINTH, AXI, …) reason
  about *when* / *why* and invoke skills.
- **CLI nouns + verbs** (`axi data`, `axi hygiene`, …) are the
  deterministic surface — pure dispatchers over skills.
- **Skills** are the executable unit. Deterministic by default; may
  themselves invoke an LLM persona for irregularities.
"""

from __future__ import annotations

import importlib
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axiom.infra.principal import PrincipalContext, open_principal


@dataclass
class SkillResult:
    """The uniform return shape of a skill.

    Callers branch on ``.ok`` rather than catching exceptions —
    handling the success and failure paths the same way for every
    skill makes the agent-persona reasoning loop predictable.
    """

    ok: bool = True
    value: Any = None
    errors: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """CLI verbs return this from ``main()`` — 0 on success, 1 on failure."""
        return 0 if self.ok else 1


@dataclass(frozen=True)
class SkillContext:
    """The runtime handle passed to every skill invocation.

    Skills NEVER reach for globals or environment except through this
    context — that's what makes them composable in agent-reasoning
    loops + testable as pure functions.
    """

    registry: "SkillRegistry"
    state_dir: Path
    logger: logging.Logger
    user_prompt: Callable[[str], str] | None = None
    """``(prompt) -> response`` when an interactive caller is present;
    ``None`` for headless/CI invocation. Skills MUST handle ``None``
    by returning ``ok=False`` with a clear error rather than blocking."""

    principal: PrincipalContext = field(default_factory=open_principal)
    """Who is acting (ADR-074, AEOS-ID-1). NEVER None — defaults to the ``open``
    posture (an unproven, OS-derived principal: today's free-wheeling default).
    Skills doing consequential work SHOULD check ``principal.assured`` /
    ``principal.posture`` and may trigger step-up (later milestones)."""


SkillFn = Callable[[dict[str, Any], SkillContext], SkillResult]


@dataclass(frozen=True)
class SkillSpec:
    """Declarative metadata for a registered skill (ADR-063).

    Carries the fields the SKILL.md generator emits and that the AEOS
    manifest's ``[[extension.provides]]`` block needs (``entry`` + ``path``
    derive mechanically from ``fn`` + ``name``). Old call sites that
    pass ``register(name, fn)`` keep working — ``SkillSpec`` is the
    optional richer form.

    Fields:

    - ``name`` — qualified skill name (e.g. ``press.draft``).
    - ``fn`` — the callable ``(params, ctx) -> SkillResult``.
    - ``description`` — short one-liner; the SKILL.md frontmatter
      ``description`` and the AEOS ``description`` come from this.
    - ``long_description`` — optional prose for the SKILL.md body. When
      empty, the generator emits a stub pointing back at ``fn``.
    - ``inputs`` — name → shape map (``{"source": "Path"}``). Shape-only;
      not enforced at runtime, only used for documentation generation.
    - ``allowed_tools`` — tools the skill may invoke (Anthropic SKILL.md
      ``allowed-tools`` field). Empty tuple means unconstrained.
    """

    name: str
    fn: SkillFn
    description: str = ""
    long_description: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    # Capability projection (ADR-072 / AEOS §4.9). The READ/WRITE → approval
    # decision lives HERE, on the capability, and is honored identically by
    # every surface (CLI confirm, MCP side_effects, chat approval gate) — not
    # defaulted per surface. ``None`` = undeclared → the projector treats it
    # as WRITE (confirm-gated) conservatively until the capability declares.
    side_effects: bool | None = None
    idempotent: bool | None = None
    # Surface exposure (ADR-072 §4.9.4 / ADR-073). Which projections this
    # capability opts into: subset of {"cli", "mcp", "agent_tool", "skill_md"}.
    # ``None`` = undeclared. The MCP server exposes a capability as a tool iff
    # "mcp" is present — the bounded-exposure guard against tool-explosion.
    surfaces: tuple[str, ...] | None = None


class _LazySkill:
    """Wraps a dotted entry path; resolves on first call."""

    def __init__(self, entry: str) -> None:
        self._entry = entry
        self._resolved: SkillFn | None = None

    def __call__(self, params, ctx):  # type: ignore[no-untyped-def]
        if self._resolved is None:
            mod_path, _, attr = self._entry.partition(":")
            if not attr:
                raise ValueError(
                    f"skill entry must be 'module.path:function', got {self._entry!r}"
                )
            mod = importlib.import_module(mod_path)
            self._resolved = getattr(mod, attr)
        return self._resolved(params, ctx)


class SkillRegistry:
    """A namespaced index of skill functions.

    Names MUST be qualified (``namespace.name``) so cross-extension
    discovery doesn't collide. The namespace conventionally matches
    the providing extension's CLI noun.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillFn] = {}
        self._specs: dict[str, SkillSpec] = {}

    # ---- registration ---------------------------------------------------

    def register(self, name: str, fn: SkillFn) -> None:
        """Register a callable skill function under ``name``.

        The legacy form. Prefer :meth:`register_skill` for new code so
        the ADR-063 SKILL.md generator can pick up the metadata.
        """
        self._guard_name(name)
        if name in self._skills:
            raise ValueError(f"skill {name!r} is already registered")
        self._skills[name] = fn

    def register_skill(self, spec: SkillSpec) -> None:
        """Register a skill via :class:`SkillSpec` (ADR-063)."""
        self._guard_name(spec.name)
        if spec.name in self._skills:
            raise ValueError(f"skill {spec.name!r} is already registered")
        self._skills[spec.name] = spec.fn
        self._specs[spec.name] = spec

    def spec(self, name: str) -> SkillSpec | None:
        """Return the declarative spec for ``name``, or ``None`` if the
        skill was registered with the legacy ``register(name, fn)``."""
        return self._specs.get(name)

    def specs(self) -> dict[str, SkillSpec]:
        """All registered specs, keyed by qualified name."""
        return dict(self._specs)

    def register_entry(self, name: str, entry: str) -> None:
        """Register a skill by dotted entry path (``'module:function'``).

        Resolved lazily on first invoke — so manifest-driven discovery
        can register hundreds of skills without importing their modules
        until they're actually called.
        """
        self._guard_name(name)
        if name in self._skills:
            raise ValueError(f"skill {name!r} is already registered")
        self._skills[name] = _LazySkill(entry)

    # ---- invocation -----------------------------------------------------

    def invoke(self, name: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        """Invoke a registered skill. Wraps exceptions as failed results.

        Skills are expected to return ``SkillResult`` themselves; an
        uncaught exception becomes ``ok=False`` with the traceback in
        ``errors`` so a poorly-written skill never crashes the caller
        (agent reasoning loop or CLI dispatcher).
        """
        if name not in self._skills:
            raise KeyError(f"no skill registered as {name!r}")
        fn = self._skills[name]
        try:
            result = fn(params, ctx)
        except Exception as exc:  # broad — wrapping is the point
            ctx.logger.exception("skill %s raised", name)
            return SkillResult(
                ok=False,
                errors=[f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=4)],
            )
        if not isinstance(result, SkillResult):
            return SkillResult(
                ok=False,
                errors=[
                    f"skill {name!r} returned {type(result).__name__}, expected SkillResult"
                ],
            )
        return result

    # ---- discovery ------------------------------------------------------

    def list(self, namespace: str | None = None) -> list[str]:
        """Return registered skill names, sorted. Filter by namespace prefix."""
        names = sorted(self._skills)
        if namespace is None:
            return names
        prefix = namespace + "."
        return [n for n in names if n.startswith(prefix)]

    def has(self, name: str) -> bool:
        return name in self._skills

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _guard_name(name: str) -> None:
        if "." not in name:
            raise ValueError(
                f"skill name {name!r} must be qualified (namespace.name); "
                "unqualified names collide across extensions"
            )
        if not name.replace(".", "_").replace("-", "_").isidentifier():
            raise ValueError(f"skill name {name!r} is not a valid identifier")


# ---------- module-level test fixture (for test_skills.py) ----------------


def _test_upper_skill(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """A no-op skill used by `register_entry` round-trip tests.

    Lives in the module the registry expects to import — keeps the
    test fixture small without an extra test-data module on disk.
    """
    return SkillResult(ok=True, value=params["s"].upper())


# ---------- process-local default registry --------------------------------


_default: SkillRegistry | None = None


def default_registry() -> SkillRegistry:
    """Return the process-local default registry.

    CLI bootstrap registers extensions into this. Tests should build
    their own via ``SkillRegistry()`` to stay isolated.
    """
    global _default
    if _default is None:
        _default = SkillRegistry()
    return _default


__all__ = [
    "SkillContext",
    "SkillFn",
    "SkillRegistry",
    "SkillResult",
    "SkillSpec",
    "default_registry",
]
