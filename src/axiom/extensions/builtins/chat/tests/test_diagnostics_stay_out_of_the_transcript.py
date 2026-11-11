# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Operator diagnostics go to the log, not into the conversation.

A new user's chat printed this between every question and its answer::

    prompt fragment 'model_corral_next_steps' from <ext> dropped: <ext> has
    already used its 500-token share of the system prompt

It is a true and useful message — for whoever maintains that extension. Chat
never configured logging, so Python's last-resort handler wrote every WARNING
from anywhere in the process straight onto the terminal the conversation was
drawn on, once per turn because the prompt is composed per turn.

The fix keeps the message and moves it: to a log file always, and to stderr
only when the operator asks with ``--verbose``.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.chat.cli import configure_chat_logging
from axiom.extensions.builtins.chat.prompt_fairness import (
    EXTENSION_TOKEN_BUDGET,
    enforce_contribution_limits,
)


@pytest.fixture
def bare_root():
    """Stand the process up the way `axi chat` starts: no root handlers.

    pytest attaches its own capture handler to the root logger for each test
    phase, which hides the last-resort handler this whole defect rides on.
    Call the returned function inside the test body to strip them, so the
    assertions see what a user sees. Handlers that are not pytest's own are
    put back afterwards; anything this test added is closed.
    """
    root = logging.getLogger()
    level = root.level
    stripped: list[logging.Handler] = []
    # Other tests in the same worker leave process-global logging state behind
    # (handlers or propagate=False on an ancestor, a disabled logger, a global
    # logging.disable). Clear it on the path from the emitting logger to the
    # root, and restore it afterwards.
    from axiom.extensions.builtins.chat import prompt_fairness

    path: list[logging.Logger] = []
    name = prompt_fairness.__name__
    while name:
        path.append(logging.getLogger(name))
        name = name.rpartition(".")[0]
    saved_path = [(lg, list(lg.handlers), lg.propagate, lg.disabled, lg.level) for lg in path]
    saved_disable = logging.root.manager.disable

    def strip() -> None:
        logging.disable(logging.NOTSET)
        for lg in path:
            for handler in list(lg.handlers):
                lg.removeHandler(handler)
            lg.propagate = True
            lg.disabled = False
            lg.setLevel(logging.NOTSET)
        for handler in list(root.handlers):
            root.removeHandler(handler)
            stripped.append(handler)

    yield strip
    for lg, handlers, propagate, disabled, lvl in saved_path:
        for handler in handlers:
            lg.addHandler(handler)
        lg.propagate, lg.disabled = propagate, disabled
        lg.setLevel(lvl)
    logging.disable(saved_disable)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in stripped:
        if not type(handler).__module__.startswith("_pytest"):
            root.addHandler(handler)
    root.setLevel(level)


def _overspend():
    """The real budget path: one source contributing far past its share."""
    big = "x" * (EXTENSION_TOKEN_BUDGET * 4 * 2)
    enforce_contribution_limits(
        [
            {"layer": "capabilities", "name": "first", "source": "demo.ext", "content": big},
            {"layer": "capabilities", "name": "second", "source": "demo.ext", "content": "y"},
        ]
    )


def test_unconfigured_it_lands_on_the_terminal(capfd, bare_root):
    bare_root()
    """Negative control: the mechanism the user hit, reproduced."""
    _overspend()
    _out, err = capfd.readouterr()
    assert "share of the system prompt" in err


def test_a_budget_drop_does_not_reach_the_terminal(tmp_path, capfd, bare_root):
    bare_root()
    configure_chat_logging(verbose=False, log_path=tmp_path / "chat.log")
    _overspend()
    out, err = capfd.readouterr()
    assert "share of the system prompt" not in out + err


def test_it_is_kept_in_the_log(tmp_path, capfd, bare_root):
    bare_root()
    log = tmp_path / "chat.log"
    configure_chat_logging(verbose=False, log_path=log)
    _overspend()
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "share of the system prompt" in log.read_text(encoding="utf-8")


def test_verbose_shows_it_on_stderr(tmp_path, capfd, bare_root):
    bare_root()
    configure_chat_logging(verbose=True, log_path=tmp_path / "chat.log")
    _overspend()
    _out, err = capfd.readouterr()
    assert "share of the system prompt" in err


def test_an_unwritable_log_location_does_not_stop_chat(tmp_path, capfd, bare_root):
    bare_root()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file where a directory should be")
    configure_chat_logging(verbose=False, log_path=blocker / "chat.log")
    _overspend()
    out, err = capfd.readouterr()
    assert "share of the system prompt" not in out + err
