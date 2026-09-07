# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""``data.enroll`` — turn a source manifest into an account on the lake.

A producer cannot push until three things exist here: a connector to land in,
a credential bound to a site, and a connector→site mapping so its rows can be
attributed. None of them can be self-served, because deciding who may write
into a lakehouse is the host's decision, not the requester's.

So enrollment is shaped like a signing request. The requester generates a
complete manifest — **data only**, reviewable as a diff — and the host runs
this once. Approving a new source is reading a file, not auditing code.

What it does, and deliberately does not:

- **Registers the connector**, carrying its site, so conformance can attribute
  its rows. Idempotent: re-enrolling an existing source updates the record
  rather than minting a second one.
- **Prints the credential command** rather than running it. Issuing a
  credential is a privileged act with its own audit trail, and it belongs on a
  line the host can read before pressing return. Everything up to that point is
  automatic; that step is deliberate.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.connectors import ConnectorConfig, load_connector, save_connector

#: What a row's value is. Anything modelled must say what modelled it.
SOURCE_CLASSES = ("measured", "predicted", "estimated")
MODELLED_CLASSES = ("predicted", "estimated")


def parse_manifest(text: str) -> tuple[dict[str, Any], list[str]]:
    """Read a manifest, returning (source table, errors).

    Only the fields the lake needs are validated here. The provider table is
    the edge's business — this never has to know how the data is read, which
    is what keeps enrollment format-agnostic.
    """
    errors: list[str] = []
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {}, [f"manifest is not valid TOML: {exc}"]

    source = dict(document.get("source") or {})
    if not source:
        return {}, ["no [source] table — this is not a source manifest"]

    for required in ("name", "site", "schema_ref"):
        if not str(source.get(required, "") or "").strip():
            errors.append(f"[source] is missing {required}")

    source_class = str(source.get("source_class", "measured") or "measured")
    if source_class not in SOURCE_CLASSES:
        errors.append(f"source_class {source_class!r} is not one of {', '.join(SOURCE_CLASSES)}")
    model_ref = str(source.get("model_ref", "") or "").strip()
    if source_class in MODELLED_CLASSES and not model_ref:
        errors.append(
            f"source_class is {source_class!r} but no model_ref is set — a prediction "
            "nobody can attribute to a model version cannot be reproduced or retired"
        )
    if source_class == "measured" and model_ref:
        errors.append("model_ref is set on a 'measured' source — a measurement has no model")
    return source, errors


def connector_name(source: dict[str, Any]) -> str:
    """Namespaced by site, so two facilities may use the same short name."""
    name = str(source["name"])
    site = str(source["site"])
    return name if name.startswith(site) else f"{site}-{name}"


def issue_command(source: dict[str, Any], connector: str) -> str:
    """The one privileged step, written out for the host to run."""
    site = str(source["site"])
    return (
        f"axi gate issue api-key --principal @svc-{site}:{site} "
        f"--site {site} --scope data --name '{connector} ingest'"
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Enroll a source. Required params: ``manifest``, ``bronze_root``."""
    manifest_path = str(params.get("manifest", "") or "")
    if not manifest_path:
        return SkillResult(ok=False, errors=["manifest is required (path to source.toml)"])
    bronze_root = str(params.get("bronze_root", "") or "")
    if not bronze_root:
        return SkillResult(ok=False, errors=["bronze_root is required"])

    path = Path(manifest_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillResult(ok=False, errors=[f"cannot read {path}: {exc}"])

    source, errors = parse_manifest(text)
    if errors:
        return SkillResult(ok=False, errors=errors)

    name = connector_name(source)
    site = str(source["site"])
    config = ConnectorConfig(
        name=name,
        kind="push",
        bronze_root=bronze_root,
        site=site,
        default_disposition=str(params.get("default_disposition", "allow")),
        default_tier=str(source.get("tier", "restricted") or "restricted"),
        params={
            "schema_ref": str(source["schema_ref"]),
            "source_class": str(source.get("source_class", "measured")),
            **({"model_ref": str(source["model_ref"])} if source.get("model_ref") else {}),
        },
    )

    try:
        existing = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing.site and existing.site != site:
        return SkillResult(
            ok=False,
            errors=[
                f"connector {name!r} is already enrolled to site {existing.site!r}; "
                f"this manifest says {site!r}. Re-attributing a connector would "
                "silently move data between tenants — unregister it deliberately first."
            ],
        )

    actor = params.get("actor")
    with _authz.action(
        verb="enroll",
        resource=f"data-platform://connector/{name}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        saved = save_connector(config, state_dir=ctx.state_dir)

    command = issue_command(source, name)
    actions = [
        f"{'re-enrolled' if existing else 'enrolled'} {name!r} for site {site!r} → {saved}",
        f"audit-receipt: {act.receipt_id}",
    ]
    if config.params.get("model_ref"):
        actions.append(
            f"model source: {config.params['source_class']} values attributed to "
            f"{config.params['model_ref']!r}"
        )
    return SkillResult(
        ok=True,
        value={
            "connector": name,
            "site": site,
            "schema_ref": config.params["schema_ref"],
            "source_class": config.params["source_class"],
            "model_ref": config.params.get("model_ref"),
            "path": str(saved),
            "issue_command": command,
            "reenrolled": existing is not None,
        },
        actions_taken=actions
        + [
            "next — issue the site-bound credential (privileged, run it yourself):",
            f"    {command}",
        ],
    )
