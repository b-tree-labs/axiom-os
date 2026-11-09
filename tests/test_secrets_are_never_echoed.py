# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A prompt that asks for a credential must not echo it.

`setup/renderer.prompt_secret` has used `getpass` since the setup wizard was
written. Five other prompts asked for credentials with a bare `input()`, so
the value appeared on screen as it was typed — which on a projector, over a
shoulder, or in a terminal transcript is the exposure itself.

It is not hypothetical. On 2026-10-01 a colleague ran `axi config` during an
onboarding session; the wizard printed his GitHub personal access token and
his GitLab PAT back to him as he pasted them, and both tokens then travelled
into a screenshot and a chat paste on their way to being diagnosed. Both had
to be revoked.

The mechanism was never missing. It was simply not used, which is the failure
this guard exists to make impossible to repeat: a secure prompt that five
call sites do not call is not a secure prompt.

The scan is deliberately narrow. It looks for `input(` whose own prompt text
names a credential, so it cannot be satisfied by renaming a variable, and it
does not fire on the many legitimate `input()` calls that ask for a path, a
choice or a name.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "axiom"

#: Words that make a prompt a credential prompt. `webhook` is here because a
#: Teams webhook URL is bearer-equivalent: anyone holding it can post as the
#: integration, so it is a secret that merely looks like a link.
SECRET_WORDS = ("key", "token", "secret", "password", "passphrase", "credential", "webhook")

#: `input(` with a string literal argument, captured so the prompt text can be
#: read. Multi-line calls are matched by allowing whitespace after the paren.
_INPUT_WITH_PROMPT = re.compile(r"""input\(\s*(?:f?["'])(?P<prompt>[^"']{0,200})""")

#: Exact prompt texts that name a credential but do not read one. Empty, and
#: meant to stay that way: an entry is a promise that those words will never
#: be a value somebody types.
ALLOWED: set[str] = set()


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = _INPUT_WITH_PROMPT.search(line)
            if not match:
                continue
            prompt = match.group("prompt")
            if prompt in ALLOWED:
                continue
            # Word boundaries: a prompt about an `api key` is a credential
            # prompt, a variable named `cmd_keys` inside an f-string is not.
            if any(re.search(rf"\b{word}", prompt.lower()) for word in SECRET_WORDS):
                rel = path.relative_to(SRC.parents[1])
                found.append(f"{rel}:{line_no}: {prompt.strip()}")
    return found


def test_no_credential_is_read_with_an_echoing_prompt():
    offenders = _offenders()
    assert not offenders, (
        "these prompts ask for a credential and echo it as it is typed; use "
        "`axiom.setup.renderer.prompt_secret`, which does not:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_can_fail(tmp_path, monkeypatch):
    """A negative control, so a scan that has stopped scanning cannot pass.

    Without this, a regex that matches nothing — a refactor to a different
    prompt helper, a rename of the source tree — would read as a clean repo.
    """
    planted = tmp_path / "axiom" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('value = input("  Paste GitHub key (Enter to skip): ")\n', encoding="utf-8")
    monkeypatch.setattr("tests.test_secrets_are_never_echoed.SRC", planted.parent)
    assert _offenders(), "the scan found nothing in a file that plainly offends"


def test_the_secure_prompt_does_not_echo():
    """`prompt_secret` is the thing the others must use, so its own behaviour
    is asserted rather than assumed."""
    import inspect

    from axiom.setup import renderer

    source = inspect.getsource(renderer.prompt_secret)
    assert "getpass" in source, "prompt_secret no longer uses getpass"
    assert "input(" not in source, "prompt_secret reads with input(), which echoes"
