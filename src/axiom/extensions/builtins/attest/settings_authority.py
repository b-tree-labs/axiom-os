# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A change to an authority setting is a signed record in the ``settings_authority`` log.

ADR-284 B5, prd-settings ST-15. ``axiom.infra.settings_ledger`` defines the seam and never imports
this extension; this fills it, as :mod:`.approvals` fills the decision seam. The ledger accepts a
change to an authority key only with the id of an ``AUTHORITY_CHANGE`` record, and asks
:class:`AttestAuthorityVerifier` on every read whether that record still signs exactly that
change.

The record binds itself to one change by ``change_digest``: the section, key, operation, value and
revision. The verifier recomputes the digest from the record's own fields, so a record whose digest
was copied from another change is refused, and compares it with the change being asked about, so a
record cannot authorise another value or the same value again at a later revision.

The signer's role is checked when the person signs (the log's type names ``site_authority``), not
again when the record is read: a signer who later loses the role does not unwind what they signed.
What is checked on every read is that the log's chain still verifies, so an edited record is not
evidence.

A node binds its ledger when it discovers its logbooks (:func:`bind`): the verifier is installed,
the sign hook is set, and the ledger becomes the one source authority keys are read from. The
command line cannot prove a fresh sign-in, so a change is proposed there and signed where that can
be proved, as for approvals; signing applies it (:func:`apply_record`). A change proposed against a
revision the key has since left is not applied, and the signed record stays unused.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from axiom.infra import settings_ledger

from . import service, signing

log = logging.getLogger(__name__)

LOGBOOK_FILE = Path(__file__).resolve().parent / "logbooks" / "settings_authority.toml"
LOGBOOK = "settings_authority"
TYPE = "AUTHORITY_CHANGE"
ROLE = "site_authority"

#: The one section this node's ledger declares, and the key in it that confers signing authority.
SECTION = "attest"
GATE_ROLES = "gate_roles"
_GATE_ROLES_SCHEMA = {"type": "object", "additionalProperties": {"type": "string"}}

#: A site id is a plain name: it becomes a directory name, and what a person signed at one site
#: must never be read as another's.
_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_ledgers: dict[str, settings_ledger.SettingsLedger] = {}


def _state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def node_ledger(site: str) -> settings_ledger.SettingsLedger:
    """The settings ledger of one site on this node: a file of its own under the node's state
    directory. A node can serve several sites, and the authority a person holds at one site is
    not authority at another, so each site's pairs are a ledger whose scope is that site."""
    if not isinstance(site, str) or not _SITE_ID.match(site) or ".." in site:
        raise ValueError(f"{site!r} is not a site id")
    led = _ledgers.get(site)
    if led is None:
        led = settings_ledger.SettingsLedger(
            _state_dir() / "settings" / site / "ledger.jsonl", scope=site
        )
        led.declare_section(SECTION, schema={GATE_ROLES: _GATE_ROLES_SCHEMA}, authority=(GATE_ROLES,))
        _ledgers[site] = led
    return led


def reset_node_ledger() -> None:
    _ledgers.clear()


def _authoritative_source(site_id: str) -> dict[str, Any]:
    """What ``site_settings.authoritative_value`` reads: the asked site's ledger, and nothing for
    a name that is not a site id."""
    try:
        return dict(node_ledger(site_id).as_site_settings_source()(site_id))
    except ValueError:
        return {}


def _canonical_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_change_draft(
    *,
    site_id: str,
    section: str,
    key: str,
    op: str,
    value: Any,
    revision: int,
    for_principal: str,
) -> str:
    """A draft of a person's signature on one change to an authority key. The fields show the
    change in full and the digest binds the signature to it."""
    digest = settings_ledger.change_digest(
        section=section, key=key, op=op, value=value, revision=revision, scope=site_id
    )
    verb = "Set" if op == "set" else "Remove"
    summary = f"{verb} {section}.{key}" + (f" to {_canonical_value(value)}" if op == "set" else "")
    return service.create_draft(
        site_id=site_id,
        logbook=LOGBOOK,
        entry_type=TYPE,
        meaning="approved",
        content={
            "title": summary,
            "fields": {
                "section": section,
                "key": key,
                "op": op,
                "value": _canonical_value(value),
                "revision": str(revision),
                "scope": site_id,
                "change_digest": digest,
                "summary": summary,
            },
        },
        origin="human",
        for_principal=for_principal,
    )


class AttestAuthorityVerifier:
    """The attest side of ``axiom.infra.settings_ledger.AuthorityVerifier``."""

    def verify(
        self,
        *,
        section: str,
        key: str,
        op: str,
        value: Any,
        revision: int,
        evidence: str,
        scope: str = "",
    ) -> str | None:
        record = service.record(evidence)
        if record is None:
            return f"no signed record {evidence!r}"
        if record.get("logbook") != LOGBOOK or record.get("entry_type") != TYPE:
            return f"record {evidence!r} is not a settings authority change"
        site = str(record.get("site") or record.get("site_id") or "")
        if not scope or not site or site != scope:
            return f"record {evidence!r} was signed at {site or 'no site'}, not {scope or 'no site'}"
        fields = (record.get("content") or {}).get("fields") or {}
        asked = settings_ledger.change_digest(
            section=section, key=key, op=op, value=value, revision=revision, scope=scope
        )
        try:
            signed = settings_ledger.change_digest(
                section=str(fields["section"]),
                key=str(fields["key"]),
                op=str(fields["op"]),
                value=json.loads(str(fields["value"])),
                revision=int(fields["revision"]),
                scope=str(fields["scope"]),
            )
        except (KeyError, ValueError):
            return f"record {evidence!r} does not describe a change"
        if signed != fields.get("change_digest"):
            return f"record {evidence!r} does not match its own digest"
        if signed != asked:
            return f"record {evidence!r} signs a different change"
        report = service.verify_logbook(site, LOGBOOK, signing.public_keys())
        if not report.ok:
            return f"the {LOGBOOK} log does not verify, so record {evidence!r} is not evidence"
        return None


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    return (record.get("content") or {}).get("fields") or {}


def apply_record(record: dict[str, Any]) -> bool:
    """Apply a signed ``AUTHORITY_CHANGE`` to this node's ledger. Idempotent on the record's id.
    ``False`` when it is not applied: the key left the revision the change was proposed against,
    or the ledger refused it. A signed record never fails the signing it follows."""
    if record.get("entry_type") != TYPE:
        return False
    fields = _fields(record)
    evidence = str(record.get("attestation_id") or "")
    site = str(record.get("site") or record.get("site_id") or "")
    try:
        ledger = node_ledger(site)
    except ValueError:
        return False
    if fields.get("scope") != site:
        return False
    if any(e.evidence == evidence for e in ledger.entries()):
        return True
    try:
        section, key, op = str(fields["section"]), str(fields["key"]), str(fields["op"])
        revision = int(fields["revision"])
        value = json.loads(str(fields["value"]))
        writer = ledger.writer(section, actor=str(record.get("signer") or ""))
        if op == "set":
            writer.set(key, value, expected_revision=revision - 1, evidence=evidence)
        else:
            writer.unset(key, expected_revision=revision - 1, evidence=evidence)
    except settings_ledger.StaleRevision:
        log.warning("settings change %s not applied: %s moved since it was proposed", evidence, fields.get("key"))
        return False
    except Exception as exc:  # noqa: BLE001 - the signature stands; the change is simply not applied
        log.warning("settings change %s not applied: %s", evidence, exc)
        return False
    return True


def _after_sign(record: dict[str, Any]) -> None:
    apply_record(record)


def install() -> None:
    """Fill the ledger's authority seam and apply a change when it is signed. Idempotent."""
    settings_ledger.set_authority_verifier(AttestAuthorityVerifier())
    service.set_sign_hooks(LOGBOOK, after=_after_sign)


def uninstall() -> None:
    settings_ledger.reset_authority_verifier()
    service.clear_sign_hooks(LOGBOOK)


def bind() -> None:
    """Bind this node's ledger: the seam, the hook, and the authoritative source. Called when the
    node discovers its logbooks, so any process that reads a gate pair reads it from the ledger."""
    from . import site_settings

    install()
    if not site_settings.has_authoritative():
        site_settings.register_authoritative(_authoritative_source)


__all__ = [
    "GATE_ROLES",
    "SECTION",
    "LOGBOOK",
    "LOGBOOK_FILE",
    "ROLE",
    "TYPE",
    "AttestAuthorityVerifier",
    "apply_record",
    "bind",
    "create_change_draft",
    "install",
    "node_ledger",
    "reset_node_ledger",
    "uninstall",
]
