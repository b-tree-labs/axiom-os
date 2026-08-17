# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext completion <shell>`` — emit shell-completion registration.

We lean on ``argcomplete`` for the actual completion engine (already wired
in :mod:`axiom.axiom_cli`). This verb prints the one-liner the user pastes
into their shell rc file so ``axi`` tab-completions light up.

Supported shells: ``zsh``, ``bash``, ``fish``. Any other value exits 2.
"""

from __future__ import annotations

import argparse

from axiom.cli.ext._output import console, error
from axiom.cli.ext.provider import CliContext

_SUPPORTED_SHELLS: frozenset[str] = frozenset({"bash", "zsh", "fish"})


_ZSH_SNIPPET = """\
# Add this to ~/.zshrc (once) to enable `axi` tab-completion:
autoload -U bashcompinit
bashcompinit
eval "$(register-python-argcomplete axi)"
"""


_BASH_SNIPPET = """\
# Add this to ~/.bashrc (once) to enable `axi` tab-completion:
eval "$(register-python-argcomplete axi)"
"""


_FISH_SNIPPET = """\
# Add this to ~/.config/fish/completions/axi.fish to enable tab-completion:
register-python-argcomplete --shell fish axi | source
"""


def snippet_for(shell: str) -> str:
    """Return the registration snippet for ``shell`` or raise ``KeyError``."""
    return {
        "zsh": _ZSH_SNIPPET,
        "bash": _BASH_SNIPPET,
        "fish": _FISH_SNIPPET,
    }[shell]


class CompletionProvider:
    """Built-in provider for ``axi ext completion <shell>``."""

    verb = "completion"
    description = "Emit shell-completion registration (bash, zsh, fish)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "shell",
            choices=sorted(_SUPPORTED_SHELLS),
            help="Target shell",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        try:
            snippet = snippet_for(args.shell)
        except KeyError:
            error(
                f"axi ext completion: unsupported shell {args.shell!r}",
                hint=f"supported: {', '.join(sorted(_SUPPORTED_SHELLS))}",
            )
            return 2
        # Emit the snippet verbatim via the console so the output is capture-
        # stable. ``markup=False`` keeps the ``$(...)`` literal.
        console().print(snippet, markup=False, highlight=False)
        return 0


__all__ = ["CompletionProvider", "snippet_for"]
