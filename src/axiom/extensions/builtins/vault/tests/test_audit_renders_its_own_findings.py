# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi vault audit` must survive having something to report.

On the node it printed `audited 5 credential(s) over 14 days` and then died
with `command 'vault' failed: 'status'`.

The renderer was reading the wrong skill's row shape. `vault.reconcile` emits
findings keyed `status` / `stored` / `issuer`, because it compares what we
recorded against what the issuer says. `vault.audit` emits `name` / `level` /
`expires_at` / `detail`, because it only reads recorded expiry. The CLI's audit
branch used reconcile's keys.

What makes it worse than a typo is *when* it fires. `value["findings"]` carries
only the FLAGGED findings and `ok=not flagged`, so with every credential
healthy the loop body never runs and the command looks fine. The KeyError
arrives on the first expiring credential — the command works right up until it
has something to tell you, and then takes the warning with it. A green audit was
never evidence the renderer worked.
"""

from __future__ import annotations

import argparse

from axiom.extensions.builtins.vault import cli


def _args(**kw):
    base = {
        "subcommand": "audit",
        "json": False,
        "dry_run": False,
        "horizon_days": None,
        "workspace": None,
        "env_root": None,
        "host": None,
        "purpose": None,
        "within_days": 14,
        "name": None,
        "disposition": None,
        "review_by": None,
        "owner": None,
        "apply": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class _Result:
    """The shape `vault.audit` actually returns."""

    def __init__(self, findings):
        self.ok = not findings
        self.value = {
            "within_days": 14,
            "total": len(findings),
            "flagged": len(findings),
            "by_level": {},
            "findings": findings,
        }
        self.actions_taken = [f"audited {len(findings)} credential(s) over 14 days"]
        self.errors = []


EXPIRING = [
    {
        "name": "triga-db-readonly",
        "level": "expiring",
        "expires_at": "2027-09-17",
        "detail": "357 days",
    },
    {
        "name": "box-oauth",
        "level": "no_expiry",
        "expires_at": None,
        "detail": "nothing records when this dies",
    },
]


def _run_with(monkeypatch, findings, capsys):
    monkeypatch.setattr(cli, "_build_ctx", lambda: type("C", (), {"registry": None})())
    monkeypatch.setattr(
        "axiom.infra.skill_dispatch.invoke_capability",
        lambda *a, **k: _Result(findings),
    )
    rc = cli._capability("vault.audit", _args(), {"within_days": 14})
    return rc, capsys.readouterr().out


def test_audit_does_not_crash_when_a_credential_is_flagged(monkeypatch, capsys):
    """The bug, directly."""
    rc, out = _run_with(monkeypatch, EXPIRING, capsys)

    assert rc == 1, "flagged findings must be a non-zero exit"
    assert "triga-db-readonly" in out


def test_it_prints_the_level_not_a_reconcile_status(monkeypatch, capsys):
    _, out = _run_with(monkeypatch, EXPIRING, capsys)

    assert "expiring" in out
    assert "no_expiry" in out, "a credential with no recorded expiry is the worst case"


def test_a_missing_expiry_renders_rather_than_printing_none(monkeypatch, capsys):
    """`expires_at: None` is the `no_expiry` case and it is the one that killed a
    live token. It must read as absent, not as the string 'None'."""
    _, out = _run_with(monkeypatch, EXPIRING, capsys)

    assert "None" not in out, "absent expiry must be written, never rendered as None"


def test_a_clean_audit_still_passes(monkeypatch, capsys):
    """Negative control: the path that always worked must keep working."""
    rc, out = _run_with(monkeypatch, [], capsys)

    assert rc == 0
    assert "audited 0 credential(s)" in out


def test_the_renderer_tolerates_a_finding_missing_optional_keys(monkeypatch, capsys):
    """A finding is data from another extension's skill. Losing a key it never
    promised must not take the warning down with it — that is the whole defect
    class here."""
    _, out = _run_with(monkeypatch, [{"name": "bare", "level": "expired"}], capsys)

    assert "bare" in out
    assert "expired" in out
