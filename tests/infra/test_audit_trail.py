# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Universal action audit (S1 mechanism): every mutating skill invocation
leaves one HMAC-chained record in ``<state_dir>/audit/actions.jsonl``.

Driven through ``SkillRegistry.invoke`` the way every CLI verb, MCP tool,
and agent persona reaches skills (ADR-056) — the interceptor is the seam,
not any one caller.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from axiom.infra import audit_trail
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult


def _registry() -> SkillRegistry:
    reg = SkillRegistry()

    def ok_skill(params, ctx):
        return SkillResult(ok=True, value="done")

    def failing_skill(params, ctx):
        return SkillResult(ok=False, errors=["boom", "again"])

    def raising_skill(params, ctx):
        raise RuntimeError("kaput")

    def read_only(params, ctx):
        return SkillResult(ok=True, value="peek")

    reg.register("t.mutate", ok_skill)  # legacy form: audited by default
    reg.register("t.fail", failing_skill)
    reg.register("t.raise", raising_skill)
    reg.register("t.peek", read_only, mutating=False)
    return reg


def _ctx(reg: SkillRegistry, tmp_path: Path, actor: str | None = None) -> SkillContext:
    kwargs = {} if actor is None else {"actor": actor}
    return SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("audit-test"),
        **kwargs,
    )


def _path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "actions.jsonl"


# ---------- the chain grows ------------------------------------------------


def test_chain_grows_one_record_per_invoke(tmp_path: Path):
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    reg.invoke("t.mutate", {"a": 1}, ctx)
    reg.invoke("t.mutate", {"a": 2}, ctx)

    records = audit_trail.read_chain(_path(tmp_path))
    assert [r["seq"] for r in records] == [1, 2]
    assert all(r["skill"] == "t.mutate" for r in records)
    assert all(r["outcome"] == "ok" and r["errors_count"] == 0 for r in records)
    assert records[0]["prev_hash"] == audit_trail.GENESIS
    assert records[1]["prev_hash"] == records[0]["hash"]
    # distinct params → distinct digests
    assert records[0]["params_digest"] != records[1]["params_digest"]
    # ts is UTC ISO with millisecond precision
    assert "T" in records[0]["ts"] and "+00:00" in records[0]["ts"]


def test_error_outcomes_are_recorded(tmp_path: Path):
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    r1 = reg.invoke("t.fail", {}, ctx)
    r2 = reg.invoke("t.raise", {}, ctx)
    assert not r1.ok and not r2.ok

    records = audit_trail.read_chain(_path(tmp_path))
    assert [r["outcome"] for r in records] == ["error", "error"]
    assert records[0]["errors_count"] == 2
    assert records[1]["errors_count"] >= 1


# ---------- verification + tamper-evidence ---------------------------------


def test_verify_chain_detects_tampering(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY", "test-chain-key")
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    for i in range(3):
        reg.invoke("t.mutate", {"i": i}, ctx)

    path = _path(tmp_path)
    assert audit_trail.verify_chain(path, "test-chain-key") is True
    # the wrong key must not verify either
    assert audit_trail.verify_chain(path, "some-other-key") is False

    # flip a byte in the middle record → the chain breaks
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["outcome"] = "ok" if rec["outcome"] != "ok" else "error"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert audit_trail.verify_chain(path, "test-chain-key") is False


def test_unkeyed_chain_still_chains_and_verifies(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    reg.invoke("t.mutate", {}, ctx)
    reg.invoke("t.mutate", {}, ctx)
    # no key configured → the documented "unkeyed" sentinel keys the HMAC
    assert audit_trail.verify_chain(_path(tmp_path)) is True
    assert audit_trail.verify_chain(_path(tmp_path), "unkeyed") is True


# ---------- redaction ------------------------------------------------------


def test_sensitive_params_are_redacted_before_digesting(tmp_path: Path):
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    secret = "SENSITIVE-VALUE-123"  # noqa: S105 — test fixture
    reg.invoke("t.mutate", {"api_token": secret, "target": "docs"}, ctx)

    raw = _path(tmp_path).read_text(encoding="utf-8")
    assert secret not in raw

    # the digest is over the REDACTED params — a different secret digests the same,
    # and the unredacted params digest differently
    same = audit_trail.params_digest({"api_token": "OTHER", "target": "docs"})
    recorded = audit_trail.read_chain(_path(tmp_path))[0]["params_digest"]
    assert recorded == same

    import hashlib

    plain = json.dumps(
        {"api_token": secret, "target": "docs"}, sort_keys=True, separators=(",", ":")
    )
    assert recorded != hashlib.sha256(plain.encode()).hexdigest()


def test_redaction_covers_nested_secretish_keys():
    a = audit_trail.params_digest({"outer": {"password": "one", "n": 1}})
    b = audit_trail.params_digest({"outer": {"password": "two", "n": 1}})
    c = audit_trail.params_digest({"outer": {"password": "one", "n": 2}})
    assert a == b
    assert a != c


# ---------- principal ------------------------------------------------------


def test_principal_defaults_to_local_cli(tmp_path: Path):
    reg = _registry()
    reg.invoke("t.mutate", {}, _ctx(reg, tmp_path))
    assert audit_trail.read_chain(_path(tmp_path))[0]["principal"] == "@cli:local"


def test_explicit_actor_is_recorded(tmp_path: Path):
    reg = _registry()
    reg.invoke("t.mutate", {}, _ctx(reg, tmp_path, actor="@agent:main"))
    assert audit_trail.read_chain(_path(tmp_path))[0]["principal"] == "@agent:main"


# ---------- opt-out for read-only verbs -------------------------------------


def test_non_mutating_skills_are_not_audited(tmp_path: Path):
    reg = _registry()
    ctx = _ctx(reg, tmp_path)
    r = reg.invoke("t.peek", {}, ctx)
    assert r.ok
    assert not _path(tmp_path).exists()
    # …and a mutating invoke afterwards starts the chain at seq 1
    reg.invoke("t.mutate", {}, ctx)
    assert [r["seq"] for r in audit_trail.read_chain(_path(tmp_path))] == [1]


# ---------- failure isolation ----------------------------------------------


def test_audit_write_failure_never_fails_the_skill(tmp_path: Path, monkeypatch):
    reg = _registry()
    ctx = _ctx(reg, tmp_path)

    def exploding_append(path, record):
        raise OSError("disk on fire")

    monkeypatch.setattr(audit_trail, "locked_append_jsonl", exploding_append)
    before = audit_trail.emit_failure_count()
    result = reg.invoke("t.mutate", {"a": 1}, ctx)
    assert result.ok  # the skill outcome is untouched
    assert audit_trail.emit_failure_count() == before + 1
    assert not _path(tmp_path).exists()
