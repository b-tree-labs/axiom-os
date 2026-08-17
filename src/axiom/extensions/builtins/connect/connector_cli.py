# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""`axi connector <verb>` — the Registry Fabric command surface (ADR-074).

AEOS §4.3.1-conformant: noun = ``connector`` (the registry surface, distinct
from ``connect`` which manages credentialed connections), verbs are imperative
and map 1:1 to ``connector.*`` skills (ADR-056). Resources are positional args.

  axi connector list
  axi connector show <name>
  axi connector install <name> --channel … [--config-token …]
  axi connector enable <name>
  axi connector disable <name>
  axi connector status [<connection>]

Verb vocabulary is aligned with the platform (list / show / status / install).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .skills import registry_ops as reg

_MARK = {"available": "●", "planned": "○", "deprecated": "⊘"}


# --- enabled-state persistence (connect-layer; fabric stays pure) ----------

def _state_path():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "connectors" / "enabled.json"


def _load_state():
    from axiom.infra.connector_fabric import ConnectorState

    p = _state_path()
    enabled: set[str] = set()
    if p.exists():
        try:
            enabled = set(json.loads(p.read_text()).get("enabled", []))
        except Exception:  # noqa: BLE001
            enabled = set()
    return ConnectorState(enabled)


def _save_state(state) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"enabled": sorted(state.enabled())}, indent=2))


def _resolve_name(fabric, name: str) -> str | None:
    """Case/form-insensitive: full reverse-DNS, short name, or title."""
    if fabric.get(name):
        return name
    low = name.strip().lower()
    alias = f"ai.axiom.connector.{low}"
    if fabric.get(alias):
        return alias
    for d in fabric.catalog():
        if low in (d.name.lower(), d.title.lower()) or d.name.lower().endswith(f".{low}"):
            return d.name
    return None


def _suggest(fabric, name: str) -> list[str]:
    import difflib

    low = name.strip().lower()
    forms: dict[str, str] = {}
    for d in fabric.catalog():
        forms[d.name.lower()] = d.name
        forms[d.name.lower().rsplit(".", 1)[-1]] = d.name
        forms[d.title.lower()] = d.name
    out: list[str] = []
    for h in difflib.get_close_matches(low, list(forms), n=3, cutoff=0.6):
        if forms[h] not in out:
            out.append(forms[h])
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="axi connector", description="Manage connectors (Registry Fabric)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb", required=True)

    sub.add_parser("list", help="List connectors (availability + on/off).")
    sh = sub.add_parser("show", help="Show one connector: descriptor, secrets, setup links.")
    sh.add_argument("name")
    ins = sub.add_parser("install", help="Set up a connector (vendor app + connection).")
    ins.add_argument("name")
    ins.add_argument("--channel")
    ins.add_argument("--config-token")
    ins.add_argument("--site", help="install/site identity for the app presence (e.g. example-host → 'Axiom · example-host')")
    ins.add_argument("--owner", default="@cli:local")
    ins.add_argument("--secret-ref")
    ins.add_argument("--no-open", action="store_true", help="don't open the setup URL in a browser")
    en = sub.add_parser("enable", help="Switch a connector ON.")
    en.add_argument("name")
    di = sub.add_parser("disable", help="Switch a connector OFF.")
    di.add_argument("name")
    st = sub.add_parser("status", help="Connection health/status.")
    st.add_argument("name", nargs="?")
    cfg = sub.add_parser("configure", help="Finish setup: verify tokens, join channel, enable.")
    cfg.add_argument("name")
    cfg.add_argument("--channel", required=True)
    cfg.add_argument("--app-id", help="Slack app id, for precise per-app token deep-links")
    up = sub.add_parser("upgrade", help="Update a deployed connector in place (no teardown, same tokens).")
    up.add_argument("name")
    up.add_argument("--app-id", required=True, help="the deployed app id to update in place")
    up.add_argument("--config-token", help="Slack App Configuration token (xoxe-); prompted if omitted")
    up.add_argument("--site", help="install/site identity (e.g. example-host)")
    up.add_argument("--channel")
    return p


def _resolve_or_die(fabric, name: str) -> str | None:
    full = _resolve_name(fabric, name)
    if full:
        return full
    msg = f"  Unknown connector {name!r}."
    sugg = _suggest(fabric, name)
    if sugg:
        msg += f" Did you mean: {', '.join(s.rsplit('.', 1)[-1] for s in sugg)}?"
    print(msg + "  (run `axi connector list`)", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    from axiom.infra.connector_fabric import default_fabric

    from .connectors import register_builtin_connectors

    args = build_parser().parse_args(argv)
    register_builtin_connectors(default_fabric())
    fabric = default_fabric()
    state = _load_state()
    params: dict[str, Any] = {"fabric": fabric, "state": state}

    if args.verb == "list":
        entries = reg.list_connectors(params).value["entries"]
        if args.json:
            print(json.dumps(entries, indent=2))
            return 0
        print("\n  Axiom connectors  (● available  ○ planned)\n")
        for e in sorted(entries, key=lambda x: (x["availability"] != "available", x["name"])):
            toggle = "[on] " if e["enabled"] else "[off]"
            print(f"  {_MARK.get(e['availability'], '?')} {toggle} {e['title']:<18} {e['name']}")
        print("\n  axi connector show <name> · install <name> · enable <name> · disable <name>\n")
        return 0

    if args.verb == "status":
        res = reg.status({**params, "name": args.name}, ctx=None)
        print(json.dumps(res.value, indent=2) if args.json else _fmt_status(res.value))
        return 0 if res.ok else 1

    full = _resolve_or_die(fabric, args.name)
    if not full:
        return 1

    if args.verb == "show":
        res = reg.show({**params, "name": full}, ctx=None)
        print(json.dumps(res.value, indent=2) if args.json else _fmt_show(res.value))
        return 0

    if args.verb == "enable":
        res = reg.enable({**params, "name": full}, ctx=None)
        if res.ok:
            _save_state(state)
            print(f"  ✓ enabled {full}")
        return 0 if res.ok else 1

    if args.verb == "disable":
        reg.disable({**params, "name": full}, ctx=None)
        _save_state(state)
        print(f"  ✓ disabled {full}")
        return 0

    if args.verb == "install":
        return _install(fabric, params, full, args)

    if args.verb == "configure":
        return _configure(fabric, params, full, args, state)

    if args.verb == "upgrade":
        return _upgrade(fabric, params, full, args)
    return 2


def _configure(fabric, params, full, args, state) -> int:
    """Finish a Slack-style connector: prompt the two tokens once, then the
    installer verifies, resolves + joins the channel, enables, and writes the
    run config — no manual /invite, id-hunting, or .env editing."""
    import getpass
    from pathlib import Path

    from .skills.slack_configure import configure as slack_configure

    d = fabric.get(full)
    app_id = getattr(args, "app_id", None)

    def _nav(ev) -> None:
        """Print the navigation aid for a credential (data-driven)."""
        if ev.where:
            print(f"    where: {ev.where}")
        if ev.url:
            url = ev.url.format(app_id=app_id) if app_id and "{app_id}" in ev.url else None
            if url:
                _present_link("    open", url, open_browser=False)
            else:
                print("    open: https://api.slack.com/apps → your app → that page")

    print(f"\n  Configuring {full} → #{args.channel}")
    if not app_id:
        print("  (tip: pass --app-id A0… for clickable per-token deep links)")
    secrets = [e for e in (d.env or []) if e.is_secret]
    tokens: dict[str, str] = {}
    try:
        for ev in secrets:
            print(f"\n  {ev.description}:")
            _nav(ev)
            tokens[ev.name] = getpass.getpass(f"    paste {ev.name}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled", file=sys.stderr)
        return 1
    bot = tokens.get("SLACK_BOT_TOKEN", "")
    app = tokens.get("SLACK_APP_TOKEN", "")
    res = slack_configure({"bot_token": bot, "app_token": app, "channel": args.channel}, ctx=None)
    if not res.ok:
        err = "; ".join(res.errors)
        print(f"  ✗ {err}", file=sys.stderr)
        # Installer-owned guidance: known error → precise remedy (from data);
        # unknown → escalate to AXI/LLM diagnosis.
        remedy = d.setup.remedy_for(err) if d.setup else None
        if remedy:
            print(f"\n  → How to fix: {remedy}\n", file=sys.stderr)
        else:
            print(f'\n  → Not a known error. Ask AXI to diagnose:\n'
                  f'      axi chat "connector {full} configure failed: {err[:160]}"\n', file=sys.stderr)
        return 1
    v = res.value
    print(f"  ✓ verified bot ({v['bot_user']}@{v['team']}); joined {v['channel_id']}")
    # Write the run config (demo) + enable. (Keystore is the production target.)
    repo_root = Path(__file__).resolve().parents[5]
    env_path = repo_root / "scripts" / "incident-comms-poc" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        f"SLACK_BOT_TOKEN={bot}\nSLACK_APP_TOKEN={app}\nSLACK_CHANNEL={v['channel_id']}\n"
    )
    state.enable(full)
    _save_state(state)
    print(f"  ✓ wrote {env_path}\n  ✓ enabled {full}\n")
    print("  Ready. Run the live incident:")
    print("    cd scripts/incident-comms-poc && set -a; . .env; set +a")
    print("    AXI_POC_APPLY=kubectl AXI_POC_KCTL_SSH=example-host AXI_POC_NEW_LIMIT_GIB=24 \\")
    print('      PYTHONPATH="$PWD/../../src" ../../.venv/bin/python run_poc.py\n')
    return 0


def shorten_url(url: str, *, timeout: float = 5.0) -> str:
    """Best-effort short link (TinyURL). Returns the original on any failure
    or for already-short URLs. The Slack manifest carries no secrets."""
    if len(url) < 120:
        return url
    import urllib.parse
    import urllib.request

    try:
        api = "https://tinyurl.com/api-create.php?url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(api, timeout=timeout) as resp:
            short = resp.read().decode().strip()
        return short if short.startswith("http") else url
    except Exception:  # noqa: BLE001 — networkless / API down → just use the long URL
        return url


def _present_link(label: str, url: str, *, open_browser: bool) -> None:
    short = shorten_url(url)
    print(f"  ➡  {label}: {short}")
    if open_browser:
        try:
            import webbrowser

            if webbrowser.open(short):
                print("     (opened in your browser)")
        except Exception:  # noqa: BLE001
            pass


def _install(fabric, params, full, args) -> int:
    name = args.name
    secret_ref = args.secret_ref or f"env://{name.upper()}_TOKEN"
    d = fabric.get(full)
    if d.setup and d.setup.install_kind == "app_manifest":
        from .skills.slack_install import slack_install

        if not args.channel:
            print("  install needs --channel <name>", file=sys.stderr)
            return 2
        open_browser = not getattr(args, "no_open", False)
        config_token = args.config_token

        # PRE-STEP: no token + interactive + the connector declares a credential
        # prompt → guide to the page (data-driven), then WAIT for them to paste
        # it here. All copy comes from the descriptor's SetupSpec, not this code.
        s = d.setup
        if not config_token and s.prompt and sys.stdin.isatty():
            for line in s.instructions:
                print(f"\n  {line}" if line is s.instructions[0] else f"    {line}")
            cred_url = (s.urls or {}).get(s.credential_url_label or "")
            if cred_url:
                _present_link("    open this", cred_url, open_browser=open_browser)
            import getpass

            try:
                config_token = getpass.getpass(f"  {s.prompt}").strip() or None
            except (EOFError, KeyboardInterrupt):
                config_token = None

        res = slack_install(
            {**params, "channel": args.channel, "owner": args.owner, "secret_ref": secret_ref,
             "config_token": config_token, "site": args.site}, ctx=None,
        )
        if not res.ok:
            print("  " + "; ".join(res.errors), file=sys.stderr)
            return 1
        v = res.value
        print(f"\n  Installing {full} → #{args.channel}\n")
        if "app_id" in v:
            print(f"  ✓ created app {v['app_id']}")
            _present_link("1. install to workspace (click Allow)", v["install_url"], open_browser=open_browser)
            for i, (label, url) in enumerate(v.get("token_urls", {}).items(), start=2):
                print(f"  {i}. {label}: {url}")
            print(f"\n  Then: paste tokens into the {secret_ref} binding + `axi connector enable {name}`.\n")
        else:
            # No token (Enter pressed, or non-interactive): the 2-click fallback.
            _present_link("  create the app (review → Create)", v["create_url"], open_browser=open_browser)
            print(f"\n  After the app exists: install + invite the bot, paste tokens, `axi connector enable {name}`.\n")
        return 0
    res = reg.install(
        {**params, "connector": full, "name": f"{name}-default", "owner": args.owner, "secret_ref": secret_ref},
        ctx=None,
    )
    print(("  ✓ " + json.dumps(res.value)) if res.ok else ("  " + "; ".join(res.errors)))
    return 0 if res.ok else 1


def _upgrade(fabric, params, full, args) -> int:
    d = fabric.get(full)
    if not (d.setup and d.setup.install_kind == "app_manifest"):
        print(f"  upgrade not supported for install_kind={d.setup.install_kind if d.setup else None!r}",
              file=sys.stderr)
        return 2
    from .skills.slack_update import slack_update

    s = d.setup
    if s.app_id_hint:
        print(f"\n  {s.app_id_hint}")
    config_token = args.config_token
    if not config_token and sys.stdin.isatty():
        for line in (s.instructions or []):
            print(f"\n  {line}" if line is (s.instructions or [None])[0] else f"    {line}")
        cred_url = (s.urls or {}).get(s.credential_url_label or "")
        if cred_url:
            _present_link("    open this", cred_url, open_browser=not getattr(args, "no_open", False))
        import getpass

        prompt = s.update_prompt or s.prompt or "Paste App Configuration token (xoxe-): "
        try:
            config_token = getpass.getpass(f"  {prompt}").strip() or None
        except (EOFError, KeyboardInterrupt):
            config_token = None

    res = slack_update(
        {**params, "config_token": config_token, "app_id": args.app_id,
         "site": args.site, "channel": args.channel,
         "reconsent_url": s.reconsent_url, "reconsent_note": s.reconsent_note}, ctx=None,
    )
    if not res.ok:
        print("  " + "; ".join(res.errors), file=sys.stderr)
        return 1
    v = res.value
    change = v["change"]
    print(f"\n  Upgrading {full} (app {args.app_id}) — change: {change['kind']}")
    if change["added_scopes"]:
        print(f"    + scopes: {', '.join(change['added_scopes'])}")
    if change["added_events"]:
        print(f"    + events: {', '.join(change['added_events'])}")
    if change["name_changed"]:
        print("    + display name updated")
    if not v["applied"]:
        print(f"  ✓ {v['message']}\n")
        return 0
    print("  ✓ applied in place — same app id, tokens unchanged")
    if v.get("reconsent_url"):
        _present_link("  one re-consent (click Reinstall — token stays valid)", v["reconsent_url"],
                      open_browser=not getattr(args, "no_open", False))
    print(f"\n  {v['next_steps']}\n")
    return 0


def _fmt_show(v: dict) -> str:
    d = v["descriptor"]
    lines = [f"\n  {d['name']}  ({v['availability']}, {'on' if v['enabled'] else 'off'})", f"  {d['title']} — {d['description']}"]
    if v["required_secrets"]:
        lines.append(f"  requires: {', '.join(v['required_secrets'])}")
    if v.get("setup"):
        lines.append(f"  setup: {v['setup']['summary']}")
        for label, url in v["setup"]["urls"].items():
            lines.append(f"   ↳ {label}: {url}")
    return "\n".join(lines) + "\n"


def _fmt_status(v: dict) -> str:
    conns = v["connections"]
    if not conns:
        return "  No connections yet. `axi connector install <name>` to add one.\n"
    return "\n".join(f"  {c['status']:<8} {c['name']}  ({c['connector']})" for c in conns) + "\n"


__all__ = ["main", "build_parser"]
