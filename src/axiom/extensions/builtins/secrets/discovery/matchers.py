# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What credential material looks like — a registry, not a hardcoded list.

Adding a vendor is `register_matcher(...)`, not a code change in the scanner.
Order matters only for reporting: a value may match several matchers and the
most specific one is reported, because "gitlab-pat" is a more actionable label
than "url-userinfo".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Matcher:
    name: str
    pattern: re.Pattern
    specificity: int = 0
    """Higher wins when several match. Vendor prefixes beat shape heuristics."""
    hint: str = ""


MATCHERS: dict[str, Matcher] = {}


def register_matcher(
    name: str, pattern: str, *, specificity: int = 0, hint: str = ""
) -> None:
    MATCHERS[name] = Matcher(
        name=name, pattern=re.compile(pattern), specificity=specificity, hint=hint
    )


# Vendor prefixes — specific, and each implies a known revocation path.
register_matcher("gitlab-pat", r"glpat-[A-Za-z0-9_\-]{20,}", specificity=90,
                 hint="GitLab personal/project/group token — rotate via the group or project access-tokens API")
register_matcher("github-pat", r"gh[pousr]_[A-Za-z0-9]{36,}", specificity=90,
                 hint="GitHub token — revoke in Developer settings, prefer a read-only deploy key for fetch-only consumers")
register_matcher("github-fine-grained", r"github_pat_[A-Za-z0-9_]{50,}", specificity=95)
register_matcher("slack-token", r"xox[baprs]-[A-Za-z0-9-]{10,}", specificity=90)
register_matcher("aws-access-key", r"AKIA[0-9A-Z]{16}", specificity=95)
register_matcher("openai-key", r"sk-[A-Za-z0-9]{32,}", specificity=85)
register_matcher("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", specificity=99,
                 hint="a private key in a scanned location — move to the store immediately")

# Shape heuristics — catch vendors nobody has registered yet.
register_matcher("url-userinfo", r"://[^/\s:@]+:[^/\s@]{8,}@", specificity=40,
                 hint="credential embedded in a URL — readable by anything that can print that URL")
register_matcher("bearer-literal", r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._\-]{20,}",
                 specificity=50)

# Self-generated secrets have no vendor prefix to key off, so shape is the only
# signal. Two conditions together, because either alone is noise: the variable
# NAME claims to hold a secret, and the VALUE is long enough to be one. Length
# alone would flag every PATH; a secret-ish name alone would flag
# `AXIOM_SECRETS_DEFAULT=openbao`. Lowest specificity — any vendor match wins,
# and the value is what gets captured, never the whole assignment.
register_matcher(
    "high-entropy-assignment",
    r"(?i)\b[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|PASSPHRASE|HMAC|APIKEY|TOKEN|_KEY|^KEY)"
    r"[A-Z0-9_]*\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-]{32,})[\"']?",
    specificity=30,
    hint="a self-generated secret outside the store — no vendor revocation path, "
         "so rotation is manual until it is registered",
)


def match_text(text: str) -> list[tuple[str, str]]:
    """Return ``(matcher_name, matched_value)`` for every distinct credential.

    Two forms of overlap are collapsed, because ONE credential must produce ONE
    finding — a report that lists the same token twice under different labels
    teaches operators to skim.

    1. Identical matched text under several matchers → keep the most specific.
       "gitlab-pat" is actionable; "url-userinfo" only says something is wrong.
    2. One match *containing* another (``://oauth2:glpat-…@`` contains
       ``glpat-…``) → keep the more specific, which is also the tighter match.
       Without this, every URL-embedded vendor token is reported twice.
    """
    hits: dict[str, tuple[int, str]] = {}
    for m in MATCHERS.values():
        for found in m.pattern.findall(text):
            value = found if isinstance(found, str) else found[0]
            prior = hits.get(value)
            if prior is None or m.specificity > prior[0]:
                hits[value] = (m.specificity, m.name)

    # Drop a match that merely wraps a more specific one.
    surviving = {
        value: meta
        for value, meta in hits.items()
        if not any(
            other != value and other in value and other_meta[0] > meta[0]
            for other, other_meta in hits.items()
        )
    }
    return [
        (name, value)
        for value, (_spec, name) in sorted(surviving.items(), key=lambda kv: -kv[1][0])
    ]


__all__ = ["MATCHERS", "Matcher", "match_text", "register_matcher"]
