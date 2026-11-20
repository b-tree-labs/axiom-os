# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The first-run checklist: five steps that take a new site from empty to useful (MVP-25-A6).

``sites.checklist`` with ``kind="first_run"`` returns these. Each step's state is
read from the source that holds the fact, never remembered by the page:

- ``connect_data``: the gold catalog lists a dataset, or the kit registry holds a kit.
  Serving reads gold only; silver is never consulted.
- ``invite_people``: an invitation was issued, or more than one principal holds a key.
- ``set_branding``: the web brand differs from the platform default.
- ``pick_landing``: the ``interface.landing`` setting is stored.
- ``first_chart``: the signed-in person has a saved chart.

A probe returns ``(done, detail)``. It reads only and never raises out of here: a
source that cannot be read leaves the step ``todo`` with a detail saying what
could not be read. Probes are injectable so tests touch no real store.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Probe = Callable[[], "tuple[bool, str]"]

#: id, label, action label, action prompt: in order, always all five.
STEPS: tuple[tuple[str, str, str, str], ...] = (
    ("connect_data", "Connect data", "+ Connect data", "Help me connect data with a data kit"),
    ("invite_people", "Invite people", "+ Invite", "Invite a colleague"),
    ("set_branding", "Set branding", "Set branding", "Help me set this site's name, logo and accent"),
    ("pick_landing", "Pick a landing page", "Pick a landing page", "Which page should people land on?"),
    ("first_chart", "First chart from chat", "Make a chart", "Make me a chart of the latest readings"),
)

#: The setting that records which page people land on.
LANDING_SETTING = "interface.landing"


def _count(value: Any, *path: str) -> int:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return len(value) if isinstance(value, list) else 0


def connect_data(*, ctx: Any = None) -> tuple[bool, str]:
    """Gold datasets served here, else kits in the registry. Gold only; never silver."""
    from axiom.extensions.builtins.data_platform._dsn import resolve_dsn
    from axiom.extensions.builtins.data_platform.skills import gold

    if ctx is None:
        import logging

        from axiom.infra.paths import get_user_state_dir
        from axiom.infra.skills import SkillContext, SkillRegistry

        ctx = SkillContext(
            registry=SkillRegistry(), state_dir=get_user_state_dir(), logger=logging.getLogger("axi.sites")
        )
    unread = ""
    result = gold.tables({}, ctx)
    if result.ok:
        n = _count(result.value, "data", "tables")
        if n:
            return True, f"{n} gold dataset(s) served"
    else:
        unread = "; ".join(result.errors)[:160]
    try:
        from axiom.extensions.builtins.data_platform.kit import host

        kits = host.review(resolve_dsn())
    except Exception as exc:  # noqa: BLE001 - reported as unread, not raised
        kits = []
        unread = unread or f"{type(exc).__name__}: {str(exc)[:120]}"
    if kits:
        return True, f"{len(kits)} data kit bundle(s) submitted"
    if unread:
        return False, f"could not read the data catalog: {unread}"
    return False, "no gold dataset is served and no data kit exists yet"


def invite_people() -> tuple[bool, str]:
    """An invitation on file, or more than one principal holding a key."""
    from axiom.extensions.builtins.webgate.skills._accounts import resolve_keys_path
    from axiom.extensions.builtins.webgate.skills.invite import resolve_invitations_path
    from axiom.webauth.api_keys import load_api_key_records
    from axiom.webauth.invitations import load_invitations

    path = resolve_invitations_path({})
    if path is None:
        return False, "could not read invitations: no invitations or keys file is configured"
    invites = load_invitations(path)
    if invites:
        return True, f"{len(invites)} invitation(s) issued"
    keys = resolve_keys_path({})
    people = {r.get("principal") for r in load_api_key_records(keys)} if keys and keys.exists() else set()
    if len(people) > 1:
        return True, f"{len(people)} people hold keys"
    return False, "no invitation has been issued and only one person holds a key"


def set_branding() -> tuple[bool, str]:
    """The web brand's name or accent differs from the platform default."""
    from axiom.infra import web_brand as wb

    brand = wb.resolve_web_brand()
    changed = [
        n
        for n, cur, dflt in (
            ("name", brand.product_name, wb.DEFAULT_PRODUCT_NAME),
            ("accent", brand.accent, wb.DEFAULT_ACCENT),
        )
        if cur != dflt
    ]
    if changed:
        return True, "branding set: " + ", ".join(changed)
    return False, "still the platform default name and accent"


def pick_landing() -> tuple[bool, str]:
    """A landing page is stored in the ``interface.landing`` setting."""
    from axiom.extensions.builtins.settings.store import SettingsStore

    value = SettingsStore().get(LANDING_SETTING)
    if value:
        return True, f"people land on {value}"
    return False, "no landing page is stored"


def first_chart(principal: str = "") -> tuple[bool, str]:
    """This person has at least one saved chart."""
    from axiom.extensions.builtins.charts import saved

    if not principal:
        return False, "could not read saved charts: sign in first"
    n = len(saved.listing(principal))
    if n:
        return True, f"{n} saved chart(s)"
    return False, "no saved chart yet"


def report(
    *,
    principal: str = "",
    ctx: Any = None,
    connect_data_probe: Probe | None = None,
    invite_probe: Probe | None = None,
    branding_probe: Probe | None = None,
    landing_probe: Probe | None = None,
    chart_probe: Probe | None = None,
) -> dict[str, Any]:
    """The five steps in order, each ``{id, label, state, detail, action}``, plus the tally."""
    probes: dict[str, Probe] = {
        "connect_data": connect_data_probe or (lambda: connect_data(ctx=ctx)),
        "invite_people": invite_probe or invite_people,
        "set_branding": branding_probe or set_branding,
        "pick_landing": landing_probe or pick_landing,
        "first_chart": chart_probe or (lambda: first_chart(principal)),
    }
    steps = []
    for sid, label, action_label, prompt in STEPS:
        try:
            done, detail = probes[sid]()
        except Exception as exc:  # noqa: BLE001 - an unreadable source is todo, never an error
            done, detail = False, f"could not read the source: {type(exc).__name__}: {str(exc)[:120]}"
        steps.append(
            {
                "id": sid,
                "label": label,
                "state": "done" if done else "todo",
                "detail": detail,
                "action": {"label": action_label, "prompt": prompt},
            }
        )
    n = sum(1 for s in steps if s["state"] == "done")
    return {"kind": "first_run", "steps": steps, "done": n, "total": len(steps), "complete": n == len(steps)}


def render(doc: dict[str, Any]) -> str:
    lines = [f"First run: {doc['done']} of {doc['total']} done"]
    for s in doc["steps"]:
        mark = "[x]" if s["state"] == "done" else "[ ]"
        lines.append(f"{mark} {s['label']}: {s['detail']}")
    return "\n".join(lines)


__all__ = ["LANDING_SETTING", "STEPS", "render", "report"]
