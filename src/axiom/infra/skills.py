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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra import audit_trail
from axiom.infra.principal import PrincipalContext, open_principal
from axiom.infra.skill_declarations import NextStep, Precondition, Ref


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
    #: Chaining layer (ADR-185). All four are empty unless the dispatch wrapper
    #: or the skill itself fills them, and an empty set adds nothing to any
    #: envelope. ``unmet`` is a structured refusal (``{id, why, fix}``);
    #: ``next`` is follow-ups worth offering; ``produced`` is the typed refs
    #: this result carries; ``chain`` is the call's place in a sequence.
    #: A skill may append its own dynamic ``next`` entries (a mapping with
    #: ``skill``/``command`` and ``why``); the wrapper validates them.
    unmet: list[dict[str, Any]] = field(default_factory=list)
    next: list[Any] = field(default_factory=list)
    produced: list[dict[str, Any]] = field(default_factory=list)
    chain: dict[str, Any] | None = None

    def chain_envelope(self) -> dict[str, Any] | None:
        """The additive, versioned ``chain`` key for a result envelope.

        ``None`` when there is nothing to say, so a result with no chaining
        information serialises exactly as it did before the layer existed.
        """
        from axiom.infra.skill_declarations import ENVELOPE_VERSION

        body: dict[str, Any] = {}
        if self.unmet:
            body["unmet"] = list(self.unmet)
        if self.next:
            body["next"] = [n for n in self.next if isinstance(n, dict)]
        if self.produced:
            body["produced"] = list(self.produced)
        if self.chain:
            body.update(self.chain)
        if not body:
            return None
        return {"v": ENVELOPE_VERSION, **body}

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

    registry: SkillRegistry
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

    actor: str | None = None
    """Who to attribute this invocation to in the universal action audit
    (``@<agent>:<context>`` for agent dispatchers, ``@user:<context>`` for
    MCP servers acting for a person). ``None`` — every existing construction
    — is recorded as the local-CLI default ``@cli:local``."""

    surface: str | None = None
    """The transport this invocation arrived on (``"cli"``, ``"mcp"``,
    ``"chat"``, ``"web"``, …), stamped by ``invoke_capability`` from its own
    ``surface`` argument — never from anything a caller passed in params.
    ``None`` means the skill was called without the dispatch chokepoint
    (tests, direct wiring) and a budget-aware skill MUST treat that as the
    least-trusted class (ADR-157: anonymous gets the smallest budget)."""

    chain: dict[str, Any] | None = None
    """Where this call sits in a sequence (ADR-185): ``chain_id``, ``step_id``
    and ``parent_step``, stamped by ``invoke_capability`` from the caller's
    reserved ``_chain`` argument and written into the action audit record.
    ``None`` for an unchained call, which then records exactly what it always did."""


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
    # Identity gate (ADR-114 §1). Matrix-style ``@name:context`` patterns the
    # caller must match to invoke this capability on a projected surface. ``None``
    # = undeclared → the projector applies the owner-scoped default; the surface
    # never leaves a projected tool with no principal bound.
    allowed_principals: tuple[str, ...] | None = None
    # Serving cost (ADR-157). What kind of read this capability is —
    # ``"lookup"``, ``"aggregate"``, ``"series"`` or ``"scan"`` — so the
    # serving guard can budget it per caller class. ``None`` = undeclared;
    # a serving-tier verb without a declaration is REFUSED by the guard, so
    # the absence is loud at the first call rather than unbounded forever.
    cost_class: str | None = None
    # Chaining layer (ADR-185). Every field is optional and ``None`` means "has
    # not said"; the debt lint reads the difference between that and an
    # explicit empty tuple ("says there is nothing").
    #
    # ``risk``: read | reversible-write | destructive. A destructive capability
    # refuses to run without an approved plan hash (plan / apply).
    risk: str | None = None
    # Whether the capability reaches outside the platform's own boundary (the
    # network, a third party). Projects onto MCP ``openWorldHint``; ``None`` =
    # has not said, and the hint stays unset.
    open_world: bool | None = None
    # Named preconditions, each carrying the exact fix; evaluated at call time.
    requires: tuple[Precondition, ...] | None = None
    # Typed references the result carries, and how to use each.
    produces: tuple[Ref, ...] | None = None
    # Declared follow-ups, validated against the registry.
    next_steps: tuple[NextStep, ...] | None = None
    # Recipes this capability is a step of (checked against the recipe catalog).
    recipes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from axiom.infra.skill_declarations import RISKS

        if self.risk is not None and self.risk not in RISKS:
            raise ValueError(f"{self.name}: risk must be one of {RISKS}, got {self.risk!r}")
        for attr, kind in (
            ("requires", Precondition),
            ("produces", Ref),
            ("next_steps", NextStep),
        ):
            value = getattr(self, attr)
            if value is None:
                continue
            if not isinstance(value, tuple) or not all(isinstance(v, kind) for v in value):
                raise TypeError(f"{self.name}: {attr} must be a tuple of {kind.__name__}")
        ids = [p.id for p in (self.requires or ())]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.name}: duplicate precondition ids in requires")

    @property
    def effective_risk(self) -> str | None:
        """The declared risk, else the one ``side_effects=False`` implies."""
        if self.risk is not None:
            return self.risk
        if self.side_effects is False:
            return "read"
        return None


class _LazySkill:
    """Wraps a dotted entry path; resolves on first call."""

    def __init__(self, entry: str) -> None:
        self._entry = entry
        self._resolved: SkillFn | None = None

    def __call__(self, params, ctx):  # type: ignore[no-untyped-def]
        if self._resolved is None:
            mod_path, _, attr = self._entry.partition(":")
            if not attr:
                raise ValueError(f"skill entry must be 'module.path:function', got {self._entry!r}")
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
        self._mutating: dict[str, bool] = {}

    # ---- registration ---------------------------------------------------

    def register(self, name: str, fn: SkillFn, *, mutating: bool = True) -> None:
        """Register a callable skill function under ``name``.

        The legacy form. Prefer :meth:`register_skill` for new code so
        the ADR-063 SKILL.md generator can pick up the metadata.

        ``mutating=False`` declares a read-only verb: its invocations skip
        the universal action audit. The default is ``True`` — audit
        everything unless a skill explicitly declares itself read-only.
        """
        self._guard_name(name)
        if name in self._skills:
            raise ValueError(f"skill {name!r} is already registered")
        self._skills[name] = fn
        self._mutating[name] = mutating

    def register_skill(self, spec: SkillSpec, *, mutating: bool = True) -> None:
        """Register a skill via :class:`SkillSpec` (ADR-063)."""
        self._guard_name(spec.name)
        if spec.name in self._skills:
            raise ValueError(f"skill {spec.name!r} is already registered")
        self._skills[spec.name] = spec.fn
        self._specs[spec.name] = spec
        self._mutating[spec.name] = mutating

    def spec(self, name: str) -> SkillSpec | None:
        """Return the declarative spec for ``name``, or ``None`` if the
        skill was registered with the legacy ``register(name, fn)``."""
        return self._specs.get(name)

    def specs(self) -> dict[str, SkillSpec]:
        """All registered specs, keyed by qualified name."""
        return dict(self._specs)

    def adopt(self, other: SkillRegistry, name: str) -> bool:
        """Take one capability from ``other``, spec and mutating flag intact.

        A capability belongs to the extension that wrote it, and a second
        copy would be a second thing to keep in step. This borrows the
        registration instead — which is what a surface needs when it must
        RUN somebody else's verb (a case's declared fix) without becoming
        a fork of it. Returns False when ``other`` does not have it, so
        the caller can say which one is missing rather than raising deep
        inside a button press.
        """
        fn = other._skills.get(name)
        if fn is None or name in self._skills:
            return name in self._skills
        self._skills[name] = fn
        self._mutating[name] = other._mutating.get(name, True)
        borrowed = other._specs.get(name)
        if borrowed is not None:
            self._specs[name] = borrowed
        return True

    def register_entry(self, name: str, entry: str, *, mutating: bool = True) -> None:
        """Register a skill by dotted entry path (``'module:function'``).

        Resolved lazily on first invoke — so manifest-driven discovery
        can register hundreds of skills without importing their modules
        until they're actually called.
        """
        self._guard_name(name)
        if name in self._skills:
            raise ValueError(f"skill {name!r} is already registered")
        self._skills[name] = _LazySkill(entry)
        self._mutating[name] = mutating

    # ---- invocation -----------------------------------------------------

    def invoke(self, name: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        """Invoke a registered skill. Wraps exceptions as failed results.

        Skills are expected to return ``SkillResult`` themselves; an
        uncaught exception becomes ``ok=False`` with the traceback in
        ``errors`` so a poorly-written skill never crashes the caller
        (agent reasoning loop or CLI dispatcher).

        Every invocation of a mutating skill (the default; see
        :meth:`register`'s ``mutating`` flag) is journaled AFTER execution
        into the universal action audit (``axiom.infra.audit_trail``). An
        audit-write failure never affects the returned result.
        """
        if name not in self._skills:
            raise KeyError(f"no skill registered as {name!r}")
        fn = self._skills[name]
        try:
            result = fn(params, ctx)
        except Exception as exc:  # broad — wrapping is the point
            ctx.logger.exception("skill %s raised", name)
            result = SkillResult(
                ok=False,
                errors=[f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=4)],
            )
        else:
            if not isinstance(result, SkillResult):
                result = SkillResult(
                    ok=False,
                    errors=[
                        f"skill {name!r} returned {type(result).__name__}, expected SkillResult"
                    ],
                )
        if self._mutating.get(name, True):
            audit_trail.emit(
                ctx,
                skill=name,
                params=params,
                outcome="ok" if result.ok else "error",
                errors_count=len(result.errors),
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


def ensure_context(ctx: SkillContext | None) -> SkillContext:
    """Return ``ctx``, or build the default one when a caller passed ``None``.

    Skills are declared ``ctx: SkillContext | None = None`` by convention, and
    most of them then dereference ``ctx.state_dir`` unconditionally. The
    signature therefore says ``None`` is acceptable while the body says it is
    not, and the gap is only discovered by whoever runs the verb first.

    It cost a live credential. ``axi vault renew`` — the unattended pass that
    rotates a token BEFORE it expires — died on
    ``'NoneType' object has no attribute 'state_dir'``, and the deref sat behind
    a "nothing due" short-circuit, so the verb looked healthy for as long as
    nothing needed rotating. By the time something did, the credential it was
    meant to save had already expired.

    Resolving here rather than guarding at each of the twenty-odd dereference
    sites is deliberate: a guard has to be remembered at every new one, and this
    cannot be forgotten by a skill that simply uses ``ctx``.
    """
    if ctx is not None:
        return ctx
    import logging

    from axiom.infra.paths import get_user_state_dir

    return SkillContext(
        registry=default_registry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.skills"),
        # No interactive caller is implied by a missing context, and skills are
        # required to handle None by refusing rather than blocking on input.
        user_prompt=None,
    )


__all__ = [
    "ensure_context",
    "SkillContext",
    "SkillFn",
    "SkillRegistry",
    "SkillResult",
    "SkillSpec",
    "default_registry",
]
