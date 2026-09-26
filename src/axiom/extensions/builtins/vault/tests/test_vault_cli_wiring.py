# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's rotation path has to actually run, and has to be a capability.

`axi vault renew` is the verb that rotates a credential before it expires, and
`axi vault steward` is the unattended pass that calls it. Both crashed on
`'NoneType' object has no attribute 'state_dir'` — the CLI called the skill with
`ctx=None` while the skill dereferences `ctx.state_dir`.

Nothing caught it because the verb was never on the dispatch chokepoint: it was
a CLI-private function, not a registered skill, so it had no authority gate, no
audit record and no telemetry. An expired token then sat there needing a human,
which is precisely the state `renew` exists to prevent.
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

import pytest


def _args(**kw):
    base = {"subcommand": "renew", "json": False, "dry_run": True,
            "horizon_days": None, "workspace": None, "env_root": None,
            "host": None, "purpose": None, "within_days": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_renew_does_not_crash_without_a_caller_supplied_context(tmp_path, monkeypatch):
    """The bug, directly. A CLI verb has no ambient context to hand down — it
    builds one, exactly as every other extension's CLI does."""
    from axiom.extensions.builtins.vault import cli

    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    rc = cli._renew(_args())
    assert rc in (0, 1), "renew must return an exit code, not raise"


def test_steward_completes_its_rotation_pass(tmp_path, monkeypatch):
    """`steward` is the UNATTENDED pass — sweep then renew. It reported the
    sweep clean and then died, so the half that rotates never ran and the half
    that prints reassurance did."""
    from axiom.extensions.builtins.vault import cli

    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    rc = cli._steward(_args(subcommand="steward"))
    assert rc in (0, 1)


def test_renew_is_a_registered_capability():
    """ADR-056: every CLI verb maps 1:1 to a registered skill function. `renew`
    was registered nowhere, so it was invisible to MCP, chat and agents — and
    to the capability series that would have shown nobody could run it."""
    from axiom.extensions.builtins.vault import skills

    registry = skills.bind_default()
    assert registry.has("vault.renew"), "vault.renew is not a capability"
    assert registry.has("vault.sweep")


def test_no_cli_handler_hands_a_skill_a_null_context():
    """The bug CLASS, not the instance.

    A skill signature reading `ctx: SkillContext | None = None` says None is
    acceptable; every one of these skills then dereferences `ctx.state_dir`
    unconditionally. Passing a literal None is therefore a crash waiting for
    whichever verb someone runs first. Every other extension's CLI builds a
    context — this asserts none of them stops.
    """
    import axiom.extensions.builtins as builtins_pkg

    offenders = []
    root = Path(builtins_pkg.__file__).parent
    for path in sorted(root.glob("*/cli.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ".run(" in stripped and "None)" in stripped.replace(" ", ""):
                offenders.append(f"{path.relative_to(root)}:{number}: {stripped}")
    assert not offenders, "CLI handlers passing a null skill context:\n" + "\n".join(offenders)


def test_the_guard_can_fail():
    """Negative control: the detector above must actually detect. A guard that
    cannot fire is the reason this shipped in the first place."""
    sample = "    result = renew_skill.run(params, None)"
    assert ".run(" in sample and "None)" in sample.replace(" ", "")


def test_vault_verbs_go_through_the_dispatch_chokepoint():
    """Authority gate, action audit and capability telemetry all hang off
    invoke_capability. A verb that calls its skill directly has none of them,
    which is why a broken rotation pass left no trace anywhere."""
    from axiom.extensions.builtins.vault import cli

    source = inspect.getsource(cli)
    assert "invoke_capability" in source, (
        "vault's CLI bypasses the dispatch chokepoint: no authority gate, "
        "no audit record, no telemetry"
    )


@pytest.mark.parametrize("verb", ["list", "issue", "revoke"])
def test_still_unimplemented_verbs_say_so_and_fail(verb):
    """`audit` is implemented now. The ones that remain stubs are allowed to be
    — reporting success is not. An audit that prints and exits 0 is how an
    expired credential stays invisible, which is exactly what happened."""
    from axiom.extensions.builtins.vault import cli

    rc = cli.main_with_args(_args(subcommand=verb))
    assert rc != 0, f"'{verb}' is unimplemented but exits 0"


# --- the finding that was counted and then dropped ---------------------------


class _Audit:
    """Stands in for the secrets expiry audit."""

    def __init__(self, findings):
        self._findings = findings

    def run(self, params, ctx):
        from axiom.infra.skills import SkillResult

        return SkillResult(ok=True, value={"findings": self._findings})


def _renew_with(findings, monkeypatch, _metadata=None, **params):
    from axiom.extensions.builtins.secrets.skills import audit as audit_skill
    from axiom.extensions.builtins.vault.skills import renew as renew_skill

    monkeypatch.setattr(audit_skill, "run", _Audit(findings).run)
    return renew_skill.run(
        {"dry_run": True, "_metadata": _metadata or {}, **params}, None
    )


def test_a_credential_with_no_recorded_expiry_is_escalated(monkeypatch):
    """The defect that let a live token die unnoticed.

    `no_expiry` was outside the actionable set, so such a credential was counted
    in "audited N" and then dropped. The pass reported "nothing due" and ok=True
    while six credentials sat permanently unauditable — including the one that
    later expired and locked us out.

    A credential with no recorded expiry is not fine. It is strictly WORSE than
    one expiring tomorrow, because nothing can ever flag it. It cannot be
    auto-rotated either (there is no due date to rotate against), so it is an
    escalation: a human has to record the expiry.
    """
    result = _renew_with(
        [{"name": "some-token", "level": "no_expiry", "expires_at": None}], monkeypatch
    )
    assert result.ok is False, "a permanently unauditable credential read as healthy"
    assert any("some-token" in e for e in result.errors)
    assert result.value["escalated"], "no_expiry produced no escalation"


def test_the_escalation_names_the_fix(monkeypatch):
    """An escalation nobody can act on is a log line. It has to say what to do.

    This used to assert `--expires-at`, because every undated credential got the
    same advice. That advice was wrong for most of them — a PyPI token has no
    expiry to record — so the remedy is now whatever the credential's
    disposition makes answerable. For one that has declared nothing, that is:
    declare something.
    """
    result = _renew_with(
        [{"name": "some-token", "level": "no_expiry", "expires_at": None}], monkeypatch
    )
    joined = " ".join(result.errors)
    assert "disposition" in joined
    assert any(d in joined for d in
               ("self_rotatable", "human_rotatable", "externally_owned",
                "non_expiring"))


def test_a_healthy_credential_still_reports_nothing_due(monkeypatch):
    """Negative control. If every level escalated, the heartbeat would be
    permanently red and would stop being read — the same end state as silence."""
    result = _renew_with(
        [{"name": "fine", "level": "ok", "expires_at": "2027-01-01"}], monkeypatch
    )
    assert result.ok is True
    assert "nothing due" in " ".join(result.actions_taken)


def test_no_expiry_is_not_auto_rotated(monkeypatch):
    """There is no due date to rotate against, so rotating would be guessing at
    a lifetime. It escalates instead — and must not appear as rotated."""
    result = _renew_with(
        [{"name": "some-token", "level": "no_expiry", "expires_at": None}], monkeypatch
    )
    assert result.value["rotated"] == []


# --- renew asks the question the disposition makes answerable ------------------


def test_renew_asks_for_a_disposition_not_an_expiry(monkeypatch):
    """`no_expiry` used to produce one piece of advice for every credential:
    "record an expiry". For a PyPI token there is none to record, and for
    somebody else's service account it is not ours to set. The finding repeated
    forever and could not be cleared, which is how people learn to filter the
    report."""
    result = _renew_with(
        [{"name": "pypi-token", "level": "no_expiry", "expires_at": None}], monkeypatch
    )
    joined = " ".join(result.errors)
    assert "disposition" in joined
    assert "non_expiring" in joined


def test_renew_is_clean_once_a_disposition_and_review_date_exist(monkeypatch):
    """The finding has to be CLEARABLE, or nothing changes."""
    result = _renew_with(
        [{"name": "pypi-token", "level": "no_expiry", "expires_at": None}],
        monkeypatch,
        _metadata={"pypi-token": {"name": "pypi-token",
                                  "disposition": "non_expiring",
                                  "review_by": "2027-06-01"}},
    )
    assert result.ok is True, result.errors


def test_renew_never_proposes_rotating_someone_elses_credential(monkeypatch):
    """Suggesting a rotation we have no authority to perform invites breaking a
    system we do not run."""
    result = _renew_with(
        [{"name": "netl-cred", "level": "no_expiry", "expires_at": None}],
        monkeypatch,
        _metadata={"netl-cred": {"name": "netl-cred",
                                 "disposition": "externally_owned"}},
    )
    joined = " ".join(result.errors)
    assert "owner" in joined
    assert "secrets rotate netl-cred" not in joined
