# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `SkillRegistry` — the runtime invocation layer for
AEOS skills (ADR-056). Any agent can register; any caller can invoke;
bidirectional A2A drops out of the same primitive."""

from __future__ import annotations

import logging
from pathlib import Path


def _registry():
    from axiom.infra.skills import SkillRegistry

    return SkillRegistry()


def _ctx(reg, tmp_path: Path):
    from axiom.infra.skills import SkillContext

    return SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("skill-test"),
        user_prompt=None,
    )


# ---------- registration ---------------------------------------------------


def test_register_and_invoke_roundtrips_params_and_result(tmp_path: Path):
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def echo(params, ctx):
        return SkillResult(ok=True, value={"saw": params["msg"]})

    reg.register("test.echo", echo)
    result = reg.invoke("test.echo", {"msg": "hello"}, _ctx(reg, tmp_path))

    assert result.ok is True
    assert result.value == {"saw": "hello"}


def test_register_rejects_duplicate_names(tmp_path: Path):
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def fn(p, c):
        return SkillResult(ok=True)

    reg.register("test.dup", fn)
    try:
        reg.register("test.dup", fn)
    except ValueError as exc:
        assert "already registered" in str(exc)
        return
    raise AssertionError("re-registering must raise ValueError")


def test_register_requires_qualified_name(tmp_path: Path):
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def fn(p, c):
        return SkillResult(ok=True)

    # `test.echo` qualifies; `echo` alone doesn't.
    try:
        reg.register("echo", fn)
    except ValueError as exc:
        assert "qualified" in str(exc).lower() or "namespace" in str(exc).lower()
        return
    raise AssertionError("unqualified name must raise")


def test_invoke_unknown_skill_raises_KeyError(tmp_path: Path):
    reg = _registry()
    try:
        reg.invoke("test.missing", {}, _ctx(reg, tmp_path))
    except KeyError:
        return
    raise AssertionError("unknown skill must raise KeyError")


# ---------- listing + discovery -------------------------------------------


def test_list_returns_registered_skills_sorted(tmp_path: Path):
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def fn(p, c):
        return SkillResult(ok=True)

    reg.register("data.ingest", fn)
    reg.register("data.install", fn)
    reg.register("hygiene.prune", fn)
    assert reg.list() == ["data.ingest", "data.install", "hygiene.prune"]


def test_list_by_namespace_filters(tmp_path: Path):
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def fn(p, c):
        return SkillResult(ok=True)

    reg.register("data.ingest", fn)
    reg.register("data.install", fn)
    reg.register("hygiene.prune", fn)
    assert reg.list(namespace="data") == ["data.ingest", "data.install"]


# ---------- bidirectional A2A — the load-bearing feature ------------------


def test_skill_can_invoke_another_skill_through_ctx(tmp_path: Path):
    """The key ADR-056 promise: any skill can invoke any other skill
    through its context, with no extra ceremony."""
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def inner(params, ctx):
        return SkillResult(ok=True, value={"doubled": params["n"] * 2})

    def outer(params, ctx):
        # Bidirectional: outer invokes inner via the registry on ctx.
        inner_result = ctx.registry.invoke("test.inner", {"n": params["n"]}, ctx)
        return SkillResult(ok=True, value={"final": inner_result.value["doubled"] + 1})

    reg.register("test.inner", inner)
    reg.register("test.outer", outer)
    result = reg.invoke("test.outer", {"n": 5}, _ctx(reg, tmp_path))
    assert result.value == {"final": 11}


def test_skill_invocation_propagates_failure_via_result(tmp_path: Path):
    """A skill that fails returns `ok=False` with errors. Callers
    branch on .ok rather than catching exceptions — uniform handling."""
    from axiom.infra.skills import SkillResult

    reg = _registry()

    def fails(params, ctx):
        return SkillResult(ok=False, errors=["thing broke"])

    reg.register("test.fails", fails)
    result = reg.invoke("test.fails", {}, _ctx(reg, tmp_path))
    assert result.ok is False
    assert result.errors == ["thing broke"]


def test_skill_exception_becomes_failed_result(tmp_path: Path):
    """An uncaught exception inside a skill is captured as `ok=False`
    so callers don't crash on a poorly-written skill."""
    reg = _registry()

    def boom(params, ctx):
        raise RuntimeError("oops")

    reg.register("test.boom", boom)
    result = reg.invoke("test.boom", {}, _ctx(reg, tmp_path))
    assert result.ok is False
    assert any("oops" in e for e in result.errors)


# ---------- manifest discovery --------------------------------------------


def test_register_from_manifest_entry(tmp_path: Path):
    """When an axiom-extension.toml `kind=skill` block carries an
    `entry = "module:fn"`, the loader binds it. This is how skills
    get registered at extension-discovery time."""
    from axiom.infra.skills import SkillRegistry

    # The entry must match the (params, ctx) -> SkillResult shape, so
    # we point at a real stub function defined in axiom.infra.skills.
    reg2 = SkillRegistry()
    reg2.register_entry(
        "test.upper", "axiom.infra.skills:_test_upper_skill"
    )
    result = reg2.invoke("test.upper", {"s": "hello"}, _ctx(reg2, tmp_path))
    assert result.ok is True
    assert result.value == "HELLO"
