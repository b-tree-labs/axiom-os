# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Prompt text a human can edit without a deploy, without owning the prompt.

A prompt is code, and code is the source of truth here: the text that ships is
the text in the repository, reviewed and versioned like everything else. That is
not negotiable, because a prompt edited only in a web console is a production
behaviour change with no diff, no review and no rollback.

What is missing without this module is the other half — trying a wording change
against real traffic without cutting a release, and being able to see which
wording was live when a result was recorded.

So: the repository holds the text; a managed copy is *registered* so it is
visible, versioned and linkable from a trace; and when live fetch is switched on
the managed copy may override a named fragment for as long as the experiment
runs. Registration is the normal state. Overriding is the exception, and it is
opt-in twice — once by configuration, once per fragment.

Three rules make that safe rather than frightening.

*The code text is never skipped.* Every lookup takes the repository text as its
fallback and returns it on any miss, any error, any timeout. A tracing backend
being unreachable can slow a turn; it can never change one, and it can never
fail one.

*Off costs nothing.* With live fetch disabled — the default — this does no I/O
at all and returns the fallback unchanged. A feature that taxes every turn for a
capability used during an experiment is not worth having.

*A local override still wins.* In the cascade a managed prompt sits below the
operator's own override: platform default, shipped example, extension
contribution, site config, managed, then local. The person at the machine beats
the person in the console, because they are the one who can see what broke.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Prefix so every managed prompt groups under one folder in the console
#: instead of scattering among whatever else the deployment tracks.
NAME_PREFIX = "axiom/"

_LIVE_ENV = "AXIOM_MANAGED_PROMPTS_LIVE"


@dataclass(frozen=True)
class ManagedPrompt:
    """One prompt as the console holds it."""

    name: str
    text: str
    version: str = ""
    label: str = ""


def live_fetch_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether a managed copy may override code on this deployment.

    Default off. Turning it on is a deliberate act with a blast radius, so it is
    an explicit switch rather than something that follows from having tracing
    configured — plenty of deployments want traces and not remote prompt edits.
    """
    source = env if env is not None else os.environ
    return str(source.get(_LIVE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def qualified_name(name: str) -> str:
    return name if name.startswith(NAME_PREFIX) else f"{NAME_PREFIX}{name}"


def get_managed_prompt(
    name: str,
    fallback: str,
    *,
    client: Any | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """The managed text for *name*, or *fallback* — never an exception.

    ``fallback`` is the repository text and is what ships. It is a required
    argument rather than an optional one so there is no call shape that can
    return nothing: a caller cannot accidentally ask for a prompt and receive
    silence.
    """
    if not live_fetch_enabled(env):
        return fallback
    if client is None:
        return fallback

    try:
        managed = client.get_prompt(qualified_name(name))
    except Exception:  # noqa: BLE001 - a prompt console must not break a turn
        log.warning(
            "managed prompt %r could not be fetched; using the shipped text. "
            "The turn is unaffected.", name, exc_info=True,
        )
        return fallback

    text = getattr(managed, "text", None) if managed is not None else None
    if not text:
        # A registered-but-empty prompt is far more likely to be a mistake in
        # the console than an intentional instruction to say nothing.
        log.warning(
            "managed prompt %r returned no text; using the shipped text", name
        )
        return fallback
    return text


def register_prompts(
    prompts: dict[str, str],
    *,
    client: Any | None = None,
    label: str = "production",
) -> list[str]:
    """Publish the shipped text so it is visible, versioned and diffable.

    Registration is what makes an experiment possible later: a prompt the
    console has never seen cannot be compared, labelled or rolled back. It
    changes nothing about what runs — the repository text is pushed *to* the
    console, not pulled from it.

    Returns the names registered. A failure is reported and skipped rather than
    raised: this runs post-deploy, and a release must not fail because an
    observability backend was briefly unreachable.
    """
    if client is None:
        return []

    registered: list[str] = []
    for name, text in sorted(prompts.items()):
        try:
            client.create_prompt(
                name=qualified_name(name), prompt=text, labels=[label]
            )
        except Exception:  # noqa: BLE001 - a release must not hinge on this
            log.warning(
                "could not register prompt %r; it will not be visible in the "
                "console until the next run", name, exc_info=True,
            )
            continue
        registered.append(qualified_name(name))
    return registered


__all__ = [
    "NAME_PREFIX",
    "ManagedPrompt",
    "get_managed_prompt",
    "live_fetch_enabled",
    "qualified_name",
    "register_prompts",
]
