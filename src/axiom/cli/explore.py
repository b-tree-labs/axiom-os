# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``explore``: browse the command line itself, nouns to verbs to flags.

Each item shows its one-line description, a live example built from this site's
own values, and the plain-English way to ask ``chat`` the same thing. Arrow keys
move; Right or Enter on a noun or verb goes in, Left comes back; on an example,
Enter offers to run it or copy it.

Three layers, so the part worth testing is not the part that draws:

- :func:`screen` is the **model**: one level of the command space as data. It
  reads the same document Tab completion reads (:mod:`axiom.cli.command_space`)
  and the same value cache, so explore cannot show a verb Tab does not know.
- :func:`render_listing` is the **non-interactive** form: what ``explore``
  prints when stdout is not a terminal (a pipe, a CI job, an agent), and with
  ``--list``.
- :func:`browse` is the **interactive** form, on prompt-toolkit (already the
  chat surface's dependency). Without it, the listing is printed instead.

The model is also a skill, ``explore.screen`` (ADR-056), so an assistant can ask
what is possible at any point of the command line and get the same answer.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from axiom.cli import command_space as cs

#: The CLI exits with what ``main()`` returns (see ``axiom_cli`` core dispatch).
EXIT_WITH_RETURN_CODE = True


@dataclass(frozen=True)
class Item:
    """One row on a screen: a noun, a verb or a flag."""

    kind: str  # noun | verb | flag
    name: str
    path: tuple[str, ...]
    summary: str
    example: str | None = None
    ask: str = ""
    has_children: bool = False
    detail: str = ""
    #: The verb's declared risk. Only a ``read`` example may be run from the
    #: browser; any other is offered to copy, so Enter never changes anything.
    risk: str = ""

    @property
    def runnable(self) -> bool:
        return bool(self.example) and self.risk == "read"


@dataclass
class Screen:
    path: tuple[str, ...]
    title: str
    summary: str
    items: list[Item] = field(default_factory=list)
    ask: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path), "title": self.title, "summary": self.summary,
            "ask": self.ask, "note": self.note,
            "items": [{**asdict(i), "path": list(i.path)} for i in self.items],
        }


def _ask(cli: str, phrase: str) -> str:
    return f'{cli} chat --ask "{phrase}"'


def _flag_example(verb: cs.Node, arg: cs.Arg, lookup, cli: str) -> str | None:
    """The shortest runnable line for ``verb`` plus this one flag, so flags never stack."""
    base = cs.example_for(verb, lookup, optional=0)
    if base is None:
        return None
    if arg.name in base.argv or not arg.flag:
        return base.line(cli)
    if not arg.takes_value:
        return cs.Example((*base.argv, arg.name), base.filled, base.positional).line(cli)
    value = arg.choices[0] if arg.choices else None
    if value is None and arg.values:
        found = lookup(arg.values, dict(base.filled).get("site"))
        value = found[0] if found else None
    if value is None:
        return None
    return cs.Example((*base.argv, arg.name, value), base.filled, base.positional).line(cli)


def screen(
    path: Sequence[str] = (),
    *,
    space: cs.Node | None = None,
    cache: Any = None,
    cli: str | None = None,
) -> Screen:
    """One level of the command space at ``path``, with examples from known values only."""
    from axiom.cli import completion_values
    from axiom.cli.next_steps import cli_name, lookup_from

    cli = cli or cli_name()
    space = space if space is not None else cs.load(build=True)
    cache = cache if cache is not None else completion_values.load()
    lookup = lookup_from(cache)
    path = tuple(path)
    node = space.find(path)
    if node is None:
        parent = space.find(path[:-1]) if path else None
        near = sorted(parent.children)[:12] if parent else []
        note = f"no {' '.join(path)} here" + (f"; under {' '.join(path[:-1]) or cli}: {', '.join(near)}" if near else "")
        return Screen(path, " ".join([cli, *path]), "", note=note)

    title = " ".join([cli, *path])
    items: list[Item] = []
    kind = "noun" if not path else "verb"
    for name in sorted(node.children):
        child = node.children[name]
        example = cs.example_for(child, lookup) if not child.children else None
        items.append(Item(
            kind=kind, name=name, path=child.path, summary=child.summary,
            example=example.line(cli) if example else None,
            ask=_ask(cli, cs.chat_phrase(child, example)),
            has_children=bool(child.children) or (kind == "verb" and bool(child.args)),
            detail=child.help,
            risk=child.risk,
        ))
    if not node.children and path:
        own = cs.example_for(node, lookup)
        for arg in node.args:
            items.append(Item(
                kind="flag", name=", ".join(arg.names), path=(*path, arg.name),
                summary=(cs.first_sentence(arg.help) or " or ".join(
                    completion_values_label(v) for v in arg.values)) + (
                    f" (one of: {', '.join(arg.choices[:6])})" if arg.choices else ""),
                example=_flag_example(node, arg, lookup, cli),
                ask=_ask(cli, cs.chat_phrase(node, own)),
                detail=arg.help,
                risk=node.risk,
            ))
    example = cs.example_for(node, lookup) if path else None
    note = ""
    if cache.empty:
        note = (f"examples need this site's values, and none are cached yet: run `{cli} completion refresh` "
                "(or wait a moment; a refresh has started)")
    return Screen(path, title, node.summary, items, _ask(cli, cs.chat_phrase(node, example)), note)


def completion_values_label(kind: str) -> str:
    from axiom.cli.completion_values import KINDS

    return KINDS.get(kind, kind)


# --------------------------------------------------------------------------- non-interactive


def render_listing(scr: Screen) -> str:
    """The screen as plain text: what a pipe, a CI job or an agent sees."""
    out = [scr.title + (f": {scr.summary}" if scr.summary else "")]
    if scr.note:
        out.append(f"({scr.note})")
    if scr.ask:
        out.append(f"ask: {scr.ask}")
    if not scr.items:
        return "\n".join(out)
    out.append("")
    width = min(max((len(i.name) for i in scr.items), default=8) + 2, 28)
    for item in scr.items:
        more = " ›" if item.has_children else ""
        out.append(f"  {(item.name + more).ljust(width)}{item.summary}")
        if item.example:
            caution = "" if item.risk == "read" else "   (changes things: read its --help first)"
            out.append(f"  {' ' * width}$ {item.example}{caution}")
    deeper = next((i for i in scr.items if i.has_children), None)
    if deeper is not None:
        cli = scr.title.split()[0]
        out += ["", f"go deeper: {cli} explore {' '.join(deeper.path)}"]
    return "\n".join(out)


# --------------------------------------------------------------------------- interactive


def _copy(text: str) -> bool:
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"], ["clip"]):
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text, text=True, check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def browse(start: Sequence[str] = (), *, cli: str | None = None) -> int:  # pragma: no cover - drawn UI
    """The interactive browser. Returns the exit status of a command it ran, else 0."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from axiom.cli.next_steps import cli_name
    from axiom.infra.cli_format import elide

    cli = cli or cli_name()
    space = cs.load(build=True)
    state: dict[str, Any] = {"path": tuple(start), "i": 0, "msg": "", "run": None, "confirm": False}
    stack: list[int] = []

    def current() -> Screen:
        return screen(state["path"], space=space, cli=cli)

    def _visible_rows() -> int:
        try:
            from prompt_toolkit.application.current import get_app

            return max(3, get_app().output.get_size().rows - 9)
        except Exception:  # noqa: BLE001
            return 20

    def header():
        scr = current()
        out: list[tuple[str, str]] = [("bold", f" {scr.title}"), ("", f"  {elide(scr.summary, 90)}" if scr.summary else "")]
        if scr.note:
            out.append(("italic", f"\n {elide(scr.note, 110)}"))
        return FormattedText(out)

    def listing():
        scr = current()
        if not scr.items:
            return FormattedText([("", "  (nothing here)")])
        rows = _visible_rows()
        top = min(max(0, state["i"] - rows // 2), max(0, len(scr.items) - rows))
        out: list[tuple[str, str]] = []
        for n, item in enumerate(scr.items[top:top + rows], start=top):
            mark = "›" if item.has_children else " "
            name = elide(item.name, 24).ljust(24)
            out.append(("reverse" if n == state["i"] else "", f" {name}{mark} {elide(item.summary, 80)}\n"))
        return FormattedText(out)

    def detail():
        scr = current()
        if not scr.items:
            return FormattedText([])
        item = scr.items[min(state["i"], len(scr.items) - 1)]
        example = item.example or "(needs a value this site has not reported yet)"
        if item.example and not item.runnable:
            example += "   (changes things: copy it, read its --help, then run it)"
        return FormattedText([("bold", " example  "), ("", example + "\n"),
                              ("bold", " ask chat "), ("", item.ask)])

    def footer():
        if state["confirm"]:
            out = [("bold", " Enter/r run it   c copy it   Esc back")]
        else:
            out = [("", " ↑↓ move   → or Enter open   ← back   Enter on an example: run or copy   q quit")]
        if state["msg"]:
            out.append(("italic", f"   {state['msg']}"))
        return FormattedText(out)

    kb = KeyBindings()

    def items():
        return current().items

    @kb.add("up")
    def _up(event):
        state["i"] = max(0, state["i"] - 1)
        state["confirm"] = False

    @kb.add("down")
    def _down(event):
        state["i"] = min(max(0, len(items()) - 1), state["i"] + 1)
        state["confirm"] = False

    def _open() -> bool:
        its = items()
        if not its:
            return False
        item = its[state["i"]]
        if item.kind == "flag" or not item.has_children:
            return False
        stack.append(state["i"])
        state.update(path=item.path, i=0, msg="")
        return True

    @kb.add("right")
    def _right(event):
        _open()

    @kb.add("left")
    @kb.add("backspace")
    def _left(event):
        if state["confirm"]:
            state["confirm"] = False
        elif state["path"]:
            state["path"] = state["path"][:-1]
            state["i"] = stack.pop() if stack else 0

    @kb.add("escape")
    def _esc(event):
        state["confirm"] = False

    @kb.add("enter")
    @kb.add("r")
    def _enter(event):
        its = items()
        if not its:
            return
        item = its[state["i"]]
        if not state["confirm"]:
            if event.key_sequence[0].key != "r" and _open():
                return
            if item.runnable:
                state["confirm"] = True
            elif item.example:
                state["msg"] = "this one changes things, so it is not run from here: press c to copy it"
            else:
                state["msg"] = "no example yet: it needs a value this site has not reported"
            return
        if not item.runnable:
            state["confirm"] = False
            return
        state["run"] = item.example
        event.app.exit()

    @kb.add("c")
    def _c(event):
        its = items()
        if its and its[state["i"]].example:
            ok = _copy(its[state["i"]].example)
            state["msg"] = "copied" if ok else f"no clipboard tool here; the line is: {its[state['i']].example}"
            state["confirm"] = False

    @kb.add("q")
    @kb.add("c-c")
    def _q(event):
        event.app.exit()

    app = Application(
        layout=Layout(HSplit([
            Window(FormattedTextControl(header), height=2),
            Window(FormattedTextControl(listing)),
            Window(height=1, char="─"),
            Window(FormattedTextControl(detail), height=3, wrap_lines=True),
            Window(FormattedTextControl(footer), height=1),
        ])),
        key_bindings=kb, full_screen=True)
    app.run()
    line = state["run"]
    if not line:
        return 0
    print(f"$ {line}", flush=True)
    return subprocess.call(shlex.split(line))


# --------------------------------------------------------------------------- skill + CLI


def screen_skill(params: dict[str, Any], ctx: Any = None):
    """``explore.screen``: one level of the command space, as data."""
    from axiom.infra.skills import SkillResult

    raw = params.get("path") or []
    path = raw.split() if isinstance(raw, str) else [str(p) for p in raw]
    return SkillResult(ok=True, value=screen(path).to_dict())


def bind(registry) -> None:
    from axiom.infra.skills import SkillSpec

    if not registry.has("explore.screen"):
        registry.register_skill(SkillSpec(
            name="explore.screen",
            fn=screen_skill,
            description="What can be typed at one point of the command line: each noun, verb or flag "
                        "with its description, a live example from this site's values, and the chat phrasing.",
            inputs={"path": "list — e.g. ['data', 'show']; empty for the top level"},
            side_effects=False, idempotent=True, risk="read",
            surfaces=("cli", "mcp", "agent_tool"),
        ), mutating=False)


def _parser() -> argparse.ArgumentParser:
    from axiom.cli.next_steps import cli_name

    p = argparse.ArgumentParser(
        prog=f"{cli_name()} explore",
        description="Browse the command line: nouns, then verbs, then flags, each with a live example.",
    )
    p.add_argument("path", nargs="*", help="start here, e.g. `data` or `data show`")
    p.add_argument("--list", action="store_true", help="print the listing instead of opening the browser")
    p.add_argument("--json", action="store_true", help="print the screen as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    registry = SkillRegistry()
    bind(registry)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not (args.list or args.json)
    if interactive:
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            interactive = False
            print("(the interactive browser needs prompt-toolkit, from the `chat` extra; "
                  "showing the listing)\n", file=sys.stderr)
    if interactive:
        return browse(args.path)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.explore"), user_prompt=None)
    result = invoke_capability(registry, "explore.screen", {"path": args.path}, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return result.exit_code
    data = result.value
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    scr = Screen(tuple(data["path"]), data["title"], data["summary"],
                 [Item(**{**i, "path": tuple(i["path"])}) for i in data["items"]], data["ask"], data["note"])
    print(render_listing(scr))
    return 0 if data["items"] or not data["note"] else 1


__all__ = ["Item", "Screen", "bind", "browse", "main", "render_listing", "screen", "screen_skill"]


if __name__ == "__main__":
    raise SystemExit(main())
