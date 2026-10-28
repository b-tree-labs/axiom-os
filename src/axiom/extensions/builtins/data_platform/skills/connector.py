# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi data connector add | list | show | test | remove`` — guided connector setup.

``register`` takes every value as a flag and checks nothing live, which is right
for a script that already knows them and wrong for a person setting up a feed for
the first time. ``connector add <kind>`` asks only for what it cannot work out,
reads a secret without echo straight into the vault, verifies against the live
source before it saves anything, then registers through ``data.register`` and
prints the declaration the site repository's ``connectors.toml`` carries.

Every question has a flag, so the same command runs unattended; ``--check``
re-verifies a connector already registered (the same checks ``test`` runs).

Kinds with a guided setup are listed in :data:`GUIDED_KINDS`; every other kind is
still registered with ``axi data register``. Nothing here returns, prints or logs
a secret: the vault holds the value and the connector holds a reference to it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..agents.plinth.connectors import ConnectorConfig, list_connectors, load_connector

#: Kinds ``connector add`` can set up step by step.
GUIDED_KINDS = ("box",)

#: Connector params that hold credential material, never shown even in ``show``.
_SENSITIVE_PARAM = re.compile(r"(secret|token|password|passwd|state_b64|private|key$)", re.I)

_FOLDER_ID = re.compile(r"^\d+$")


class _Stop(Exception):
    """A setup step that cannot go on: a cause and the fix, already worded."""

    def __init__(self, cause: str, fix: str = "", *, steps: list[dict] | None = None) -> None:
        super().__init__(cause)
        self.cause = cause
        self.fix = fix
        self.steps = steps or []


# ---------------------------------------------------------------- shared


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001 - a label, never a reason to fail
        return "axi"


def _io(params: dict[str, Any]) -> tuple[Callable | None, Callable | None, Callable[[str], None]]:
    """(ask, ask_secret, say). ``ask`` is None when nobody is at a terminal."""
    ask = params.get("_ask")
    ask_secret = params.get("_ask_secret")
    say = params.get("_say") or (lambda _line: None)
    return ask, ask_secret, say


def _vault(ctx: SkillContext):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    return ForeignCredentialStore(ctx.state_dir)


def _vault_ready(vault) -> None:
    """Open the vault's value store now, so a missing vault stops setup before any secret is typed."""
    try:
        _ = vault._values  # noqa: SLF001 - opening the store is the check
    except Exception as exc:  # noqa: BLE001
        cli = _brand_cli()
        raise _Stop(
            f"there is no vault on this machine to hold the Box secret ({exc})",
            f"point this machine at its vault once: `{cli} secrets vault --backend openbao "
            f"--url <vault address> --token-file <file holding the vault token>`, then run this again "
            f"(`{cli} secrets vault` with no flags shows what it finds now)",
        ) from exc


def _read_vault_json(vault, entry: str) -> dict[str, Any]:
    try:
        with vault.get(entry) as secret:
            raw = secret.value.decode("utf-8") if isinstance(secret.value, bytes) else str(secret.value)
        return json.loads(raw)
    except KeyError as exc:
        raise _Stop(f"the vault has no entry named {entry!r}",
                    f"run `{_brand_cli()} secrets list` to see the entries, or drop --app-ref "
                    "to be asked for the app's values") from exc
    except (ValueError, TypeError) as exc:
        raise _Stop(f"the vault entry {entry!r} is not a Box app (expected JSON with client_id, "
                    "client_secret, enterprise_id)", "store it again, or drop --app-ref") from exc


def _default_site(params: dict[str, Any], ctx: SkillContext) -> str | None:
    if params.get("site"):
        return str(params["site"]).strip()
    from axiom.infra.site_scope import deployment_sites

    served = deployment_sites()
    if served is not None and len(served) == 1:
        return next(iter(served))
    sites = {c.site for c in list_connectors(state_dir=ctx.state_dir) if c.site}
    if len(sites) == 1:
        return next(iter(sites))
    return None


def _ask_value(params, key: str, question: str, *, flag: str, required: bool = True) -> str:
    value = str(params.get(key) or "").strip()
    if value:
        return value
    ask, _, _ = _io(params)
    if ask is not None:
        value = str(ask(question) or "").strip()
    if not value and required:
        raise _Stop(f"no {key.replace('_', ' ')} given", f"pass {flag}")
    return value


def _fail(stop: _Stop, value: dict[str, Any] | None = None) -> SkillResult:
    out = dict(value or {})
    out.setdefault("steps", stop.steps)
    out["cause"] = stop.cause
    out["fix"] = stop.fix
    out["text"] = _render_steps(out["steps"])
    msg = stop.cause + (f". Fix: {stop.fix}" if stop.fix else "")
    return SkillResult(ok=False, value=out, errors=[msg])


def _render_steps(steps: list[dict[str, Any]], start: int = 1) -> str:
    lines = []
    for i, s in enumerate(steps, start):
        mark = "ok  " if s["ok"] else "FAIL"
        lines.append(f"  [{mark}] {i}. {s['name']}: {s['detail']}")
        if not s["ok"] and s.get("fix"):
            who = " (a Box admin)" if s.get("who") == "admin" else ""
            lines.append(f"         fix{who}: {s['fix']}")
    return "\n".join(lines)


def _render_dry_run(v: dict[str, Any]) -> str:
    dr = v.get("dry_run") or {}
    rows = dr.get("would_land") or []
    totals = dr.get("totals") or {}
    if not rows:
        return "  Dry-run pull: nothing in the folder yet."
    from axiom.infra.cli_format import Column, table

    lines = ["  Dry-run pull (nothing fetched, nothing written):"]
    lines.extend(table(
        [(r["disposition"], r["path"] or r["name"], "-> " + str(r["lands_at"])) for r in rows],
        [Column(), Column(wrap=True), Column(wrap=True)],
        indent=4,
    ))
    more = totals.get("files", 0) - len(rows)
    if more > 0:
        lines.append(f"    ... and {more} more")
    return "\n".join(lines)


def declaration(name: str, site: str, schema_ref: str | None) -> str:
    """The block the site repository's ``connectors.toml`` declares for this connector."""
    lines = [f"[connector.{name}]", f'site = "{site}"']
    if schema_ref:
        lines.append(f'schema_ref = "{schema_ref}"')
    else:
        lines.append('# schema_ref = "<format>"   # the file format your site\'s reader declares; '
                     "add it before conform can turn these files into readings")
    return "\n".join(lines)


# ---------------------------------------------------------------- add


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Set up one connector of ``kind`` step by step, verify it live, register it."""
    kind = str(params.get("kind") or "").strip()
    if kind not in GUIDED_KINDS:
        return SkillResult(ok=False, errors=[
            f"no guided setup for kind {kind!r} (guided: {', '.join(GUIDED_KINDS)}); "
            f"register it with `{_brand_cli()} data register <name> {kind or '<kind>'} ...`",
        ])
    try:
        return _add_box(params, ctx)
    except _Stop as stop:
        return _fail(stop)


def _add_box(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..sources.box import setup as box
    from ..sources.box.ccg_auth import BoxCcgConfig

    ask, ask_secret, say = _io(params)
    cli = _brand_cli()

    site = _default_site(params, ctx)
    if not site:
        site = _ask_value(params, "site", "Site these files belong to (e.g. site-a): ", flag="--site")
    name = str(params.get("name") or f"{site}-box").strip()
    bronze_root = str(params.get("bronze_root") or (Path(ctx.state_dir) / "bronze"))

    try:
        existing: ConnectorConfig | None = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        existing = None
    if params.get("check"):
        if existing is None:
            raise _Stop(f"no connector named {name!r} to check",
                        f"run `{cli} data connector add box` without --check to set it up")
        return test({"name": name, "_say": params.get("_say")}, ctx)
    if existing is not None and not params.get("force"):
        raise _Stop(f"a connector named {name!r} is already registered",
                    f"`{cli} data connector test {name}` re-verifies it; pass --force to replace it, "
                    "or --name to set up another")

    say(f"Setting up Box connector {name!r} for site {site!r}; files land under {bronze_root}.")

    # 1. The vault, before anybody types a secret into a machine that cannot keep it.
    vault = _vault(ctx)
    _vault_ready(vault)
    app_ref = str(params.get("app_ref") or "").strip()
    if app_ref:
        blob = _read_vault_json(vault, app_ref)
        say(f"Using the Box app stored in the vault as {app_ref!r}.")
    else:
        client_id = _ask_value(params, "client_id", "Box app Client ID: ", flag="--client-id")
        secret = str(params.get("client_secret") or "")
        if not secret and ask_secret is not None:
            secret = str(ask_secret("Box app Client Secret (not shown as you type): ") or "")
        if not secret:
            raise _Stop("no client secret given",
                        "run interactively to be asked for it, or pipe it on stdin with "
                        "--client-secret-stdin (it is never accepted as an argument)")
        enterprise_id = _ask_value(params, "enterprise_id", "Box Enterprise ID: ", flag="--enterprise-id")
        blob = {"client_id": client_id, "client_secret": secret, "enterprise_id": enterprise_id}
    if not BoxCcgConfig.is_ccg_blob(blob) or not blob.get("client_secret"):
        raise _Stop("the Box app values are incomplete (need client id, client secret, enterprise id)",
                    "run this again and give all three")
    auth = box.auth_for(BoxCcgConfig.from_dict(blob))

    # 2. Sign in.
    api, step, account = box.sign_in(auth)
    steps = [step.as_dict()]
    say(_render_steps(steps))
    if api is None:
        raise _Stop(step.cause, step.fix, steps=steps)

    # 3. The folder: given, or picked from what the app can see.
    folder_id = str(params.get("folder_id") or "").strip()
    if not folder_id:
        try:
            folders = box.list_folders(api)
        except Exception as exc:  # noqa: BLE001
            cause, fix, _who = box.diagnose_folder(exc, "0", account)
            raise _Stop(cause, fix, steps=steps) from exc
        if not folders:
            raise _Stop(
                f"the app signed in as {account or 'its service account'} but can see no folders",
                f"in Box, share the folder with {account or 'the app service account'} "
                "(folder > Share > Invite People, Viewer), then run this again", steps=steps)
        if ask is None:
            listing = "; ".join(f"{f['id']} {f['name']}" for f in folders[:20])
            raise _Stop("no folder given", f"pass --folder-id; the app can see: {listing}", steps=steps)
        say("Folders the app can see:")
        for i, f in enumerate(folders, 1):
            say(f"  {i:>2}. {f['name']}  (id {f['id']})")
        pick = str(ask(f"Pick a folder [1-{len(folders)}] or paste a folder id: ") or "").strip()
        # A list position is a small number; a Box folder id is a long one.
        if pick.isdigit() and 1 <= int(pick) <= len(folders):
            folder_id = folders[int(pick) - 1]["id"]
        else:
            folder_id = pick
    if not _FOLDER_ID.match(folder_id):
        raise _Stop(f"{folder_id!r} is not a Box folder id",
                    "a folder id is the number at the end of the folder's Box address "
                    "(https://app.box.com/folder/<id>)", steps=steps)

    kind_params: dict[str, str] = {"folder_id": folder_id}
    from ..sources.box.source import normalize_exclude_prefixes, normalize_extensions

    excluded = normalize_exclude_prefixes(params.get("exclude_prefix") or [])
    if excluded:
        kind_params["exclude_prefixes"] = ",".join(excluded)
    exts = normalize_extensions(params.get("exclude_ext") or [])
    if exts:
        kind_params["exclude_extensions"] = ",".join(exts)
    if params.get("max_item_mb") is not None:
        kind_params["max_item_bytes"] = str(int(params["max_item_mb"]) * 1024 * 1024)
    if params.get("schema_ref"):
        kind_params["schema_ref"] = str(params["schema_ref"])
    disposition = str(params.get("default_disposition") or "quarantine")

    # 4. Folder, one file, dry-run pull.
    v = box.verify(auth, connector_name=name, folder_id=folder_id, bronze_root=bronze_root,
                   params=kind_params, default_disposition=disposition, api=api, account=account,
                   sign_in_step=step)
    report = v.as_dict()
    say(_render_steps(report["steps"][1:], start=2))
    if not v.ok:
        bad = next(s for s in v.steps if not s.ok)
        raise _Stop(bad.cause, bad.fix, steps=report["steps"])
    say(_render_dry_run(report))

    # 5. Only now does the secret go in the vault, and only the reference in the registry.
    entry = app_ref or f"box-app-{name}"
    created_entry = False
    if not app_ref:
        try:
            vault.set(entry, json.dumps(blob).encode("utf-8"),
                      notes=f"Box client-credentials app for data connector {name}")
            created_entry = True
        except Exception as exc:  # noqa: BLE001
            raise _Stop(f"the vault refused to store the Box app ({exc})",
                        f"check `{cli} secrets vault`, then run this again", steps=report["steps"]) from exc
    kind_params["jwt_secret_ref"] = f"foreign://{entry}"

    from . import register

    reg = register.run({
        "name": name, "kind": "box", "bronze_root": bronze_root, "site": site,
        "default_disposition": disposition, "kind_params": kind_params,
        "force": bool(params.get("force")), "actor": params.get("actor"),
    }, ctx)
    if not reg.ok:
        if created_entry:
            try:
                vault.remove(entry)
            except Exception:  # noqa: BLE001 - best effort; the error below is the news
                pass
        raise _Stop("; ".join(reg.errors), "fix the above and run this again", steps=report["steps"])

    decl = declaration(name, site, params.get("schema_ref"))
    text = "\n".join([
        f"Registered {name!r} ({reg.value.get('path')}). The Box app is in the vault as {entry!r};",
        "the connector holds only the reference.",
        "",
        "Add this to your site repository's connectors.toml:",
        "",
        *("  " + line for line in decl.splitlines()),
        "",
        f"next: `{cli} data connector test {name}` re-checks it any time.",
    ])
    if params.get("_say") is None:
        text = _render_steps(report["steps"]) + "\n" + _render_dry_run(report) + "\n\n" + text
    return SkillResult(
        ok=True,
        value={**report, "name": name, "site": site, "kind": "box", "vault_entry": entry,
               "credential_ref": kind_params["jwt_secret_ref"], "registry_path": reg.value.get("path"),
               "declaration": decl, "text": text},
        actions_taken=[a for a in reg.actions_taken if not a.startswith("audit-receipt")],
    )


# ---------------------------------------------------------------- test


def test(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Re-run the live verification for a registered connector."""
    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name the connector: connector test <name>"])
    try:
        config = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        return SkillResult(ok=False, errors=[
            f"no connector named {name!r}; `{_brand_cli()} data connector list` shows them"])
    if config.kind != "box":
        from . import preflight

        return preflight.run({"name": name}, ctx)
    try:
        return _test_box(config, params)
    except _Stop as stop:
        return _fail(stop, {"name": name, "kind": "box"})


def _test_box(config: ConnectorConfig, params: dict[str, Any]) -> SkillResult:
    from ..sources.box import setup as box
    from ..sources.box.provider import BoxSourceProvider

    _, _, say = _io(params)
    ref = config.params.get("jwt_secret_ref")
    if not ref:
        raise _Stop(f"connector {config.name!r} has no Box app in the vault (it was registered "
                    "with a browser session or by hand)",
                    f"set it up with `{_brand_cli()} data connector add box --name {config.name} --force`")
    try:
        auth = BoxSourceProvider()._resolve_jwt_auth(config)  # noqa: SLF001 - the pull's own resolution
    except Exception as exc:  # noqa: BLE001
        raise _Stop(f"could not read the Box app from the vault ({ref}): {exc}",
                    f"check `{_brand_cli()} secrets vault` and `{_brand_cli()} secrets list`; "
                    "re-run `connector add box --force` to store the app again") from exc
    v = box.verify(auth, connector_name=config.name, folder_id=config.params.get("folder_id", ""),
                   bronze_root=config.bronze_root, params=config.params,
                   default_disposition=config.default_disposition, default_tier=config.default_tier,
                   provenance_rules_file=config.provenance_rules_file)
    report = v.as_dict()
    text = _render_steps(report["steps"])
    if v.ok:
        text += "\n" + _render_dry_run(report)
    value = {**report, "name": config.name, "kind": "box", "text": text}
    if params.get("_say"):
        say(text)
        value["text"] = ""
    if not v.ok:
        bad = next(s for s in v.steps if not s.ok)
        return SkillResult(ok=False, value=value, errors=[f"{bad.cause}. Fix: {bad.fix}"])
    return SkillResult(ok=True, value=value)


# ---------------------------------------------------------------- list / show / remove


def _credential_label(config: ConnectorConfig) -> str:
    ref = config.params.get("jwt_secret_ref") or config.credential_ref
    if ref:
        return f"reference {ref}"
    if config.params.get("session_state_b64") or config.params.get("session_dir"):
        return "browser session (set)"
    return "none"


def _target(config: ConnectorConfig) -> str:
    p = config.params
    for key in ("folder_id", "path", "url", "table", "edge"):
        if p.get(key) and not _SENSITIVE_PARAM.search(key):
            return f"{key}={p[key]}"
    return ""


def list_all(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    rows = [
        {"name": c.name, "kind": c.kind, "site": c.site or "(none)", "target": _target(c),
         "credential": _credential_label(c)}
        for c in list_connectors(state_dir=ctx.state_dir)
    ]
    if not rows:
        text = f"No connectors yet. `{_brand_cli()} data connector add box` sets one up."
    else:
        from axiom.infra.cli_format import Column, table

        text = "\n".join(table(
            [(r["name"], r["kind"], "site=" + r["site"], r["target"], r["credential"]) for r in rows],
            [Column(), Column(), Column(), Column(wrap=True), Column(wrap=True)],
        ))
    return SkillResult(ok=True, value={"connectors": rows, "text": text})


def show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    try:
        c = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        return SkillResult(ok=False, errors=[
            f"no connector named {name!r}; `{_brand_cli()} data connector list` shows them"])
    safe_params = {k: ("(set, not shown)" if _SENSITIVE_PARAM.search(k) else v) for k, v in c.params.items()}
    fields = {
        "name": c.name, "kind": c.kind, "site": c.site, "bronze_root": c.bronze_root,
        "default_disposition": c.default_disposition, "default_tier": c.default_tier,
        "credential_ref": c.credential_ref, "provenance_rules_file": c.provenance_rules_file,
        "params": safe_params,
    }
    lines = [f"  {k}: {v}" for k, v in fields.items() if k != "params" and v is not None]
    lines += [f"  params.{k}: {v}" for k, v in safe_params.items()]
    if c.site:
        lines += ["", "  connectors.toml declaration:",
                  *("    " + x for x in declaration(c.name, c.site, c.params.get("schema_ref")).splitlines())]
    return SkillResult(ok=True, value={**fields, "text": "\n".join(lines)})


def remove(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    try:
        c = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        return SkillResult(ok=False, errors=[f"no connector named {name!r}"])
    from . import unregister

    res = unregister.run({"name": name}, ctx)
    if not res.ok:
        return res
    actions = list(res.actions_taken)
    ref = c.params.get("jwt_secret_ref") or ""
    entry = ref[len("foreign://"):] if ref.startswith("foreign://") else ""
    if params.get("drop_vault_entry") and entry:
        try:
            _vault(ctx).remove(entry)
            actions.append(f"removed the vault entry {entry!r}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"the vault entry {entry!r} was not removed: {exc}")
    elif entry:
        actions.append(f"the vault entry {entry!r} is kept; pass --forget-secret to remove it too")
    return SkillResult(ok=True, value={"name": name, "removed": True}, actions_taken=actions)


__all__ = ["GUIDED_KINDS", "add", "declaration", "list_all", "remove", "show", "test"]
