# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reading a SKILL.md, so a written procedure becomes a capability.

ADR-063 goes one way: a ``SkillSpec`` is rendered to a SKILL.md so tools that
read that format can see what the platform offers. Nothing read one back, so a
project arriving with a directory of hand-written skill documents — the
common shape now, since every agent harness grows a folder of them — got
nothing from the platform. At best its skills became text in a corpus, quotable
and not runnable.

This is the other direction, and it is the same move the graph port makes: take
the declaration somebody already wrote and turn it into a registration, rather
than asking them to write a second one.

What a skill document actually is
---------------------------------

It has no code. It is procedural knowledge — instructions an agent follows, with
a declared set of tools it may use while following them. So the capability this
produces returns *the procedure*, and that is not a consolation prize: a harness
that had to be handed the file can now ask for it by name, on any surface, and
the answer carries the same identity and audit record as any other call.

What that changes, concretely:

**``allowed-tools`` stops being decoration.** ``SkillSpec.allowed_tools``
exists, is rendered into every generated SKILL.md, and is empty in every
declaration in this repository — no live consumer. An imported document that
declares its tools gives the field its first one, and turns a line of advisory
prose into a bounded exposure the platform can see.

**The document becomes addressable.** ``skills.get`` on any surface, rather
than a path somebody has to know and paste.

**It is versioned with the extension that ships it**, so an answer can say
which build of a procedure it followed.

Reading is deliberately forgiving in one direction and strict in the other. A
document missing its frontmatter is not a skill and is refused by name; a
document carrying extra frontmatter keys is fine, because the format belongs to
other tools too and they will add fields this does not know about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.skills import SkillResult, SkillSpec

__all__ = [
    "SkillDocument",
    "SkillDocumentError",
    "read_skill_md",
    "register_skill_md_dir",
    "spec_from_document",
]


class SkillDocumentError(ValueError):
    """A file was offered as a skill document and is not one.

    Raised rather than skipped. A directory of skill documents is imported by
    somebody who believes every file in it will register; dropping one quietly
    leaves them with a capability that does not exist and no reason to look.
    """


@dataclass
class SkillDocument:
    """What a SKILL.md declares."""

    name: str
    description: str = ""
    body: str = ""
    version: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    source_path: str = ""


def _inputs_from(raw) -> dict[str, str]:
    """Both spellings the format allows.

    Emitted as a list of ``{name, type}`` mappings; hand-written documents
    routinely use a plain ``name: type`` mapping instead. Refusing the second
    would reject most of what people actually have.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                out[str(item["name"])] = str(item.get("type", "str"))
            elif isinstance(item, str):
                out[item] = "str"
        return out
    return {}


def _tools_from(raw) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    if isinstance(raw, list):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    return ()


def read_skill_md(source: str | Path) -> SkillDocument:
    """Parse a SKILL.md into what it declares.

    Accepts a path or the text itself.
    """
    from axiom.memory.absorb.markdown_hierarchy import _split_frontmatter

    path = ""
    if isinstance(source, Path):
        path = str(source)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillDocumentError(f"cannot read {source}: {exc}") from exc
    else:
        text = source

    meta, body = _split_frontmatter(text)
    if not meta:
        raise SkillDocumentError(
            f"{path or 'the document'} has no YAML frontmatter, so it declares "
            "no skill. A SKILL.md opens with a --- block carrying at least a name."
        )

    name = str(meta.get("name") or "").strip()
    if not name:
        raise SkillDocumentError(
            f"{path or 'the document'} declares no 'name', which is the one field "
            "a capability cannot be registered without."
        )

    return SkillDocument(
        name=name,
        description=str(meta.get("description") or "").strip(),
        body=body.strip(),
        version=str(meta.get("version") or "").strip(),
        inputs=_inputs_from(meta.get("inputs")),
        allowed_tools=_tools_from(meta.get("allowed-tools", meta.get("allowed_tools"))),
        source_path=path,
    )


def spec_from_document(
    document: SkillDocument,
    *,
    namespace: str | None = None,
    surfaces: tuple[str, ...] = ("cli", "mcp", "agent_tool", "skill_md"),
) -> SkillSpec:
    """Turn a parsed document into a registrable capability.

    The function returns the procedure. A skill document has no code, and the
    useful thing to hand a caller is the instructions plus the tools they are
    permitted while following them.

    ``side_effects=False`` because reading a procedure changes nothing. What the
    procedure then tells an agent to do is gated where that happens, by the
    capabilities it calls — which is the right place, and is why declaring
    ``allowed-tools`` matters.
    """
    name = document.name
    if namespace and "." not in name:
        name = f"{namespace}.{name}"

    def run(params: dict, ctx) -> SkillResult:
        return SkillResult(
            ok=True,
            value={
                "name": name,
                "description": document.description,
                "instructions": document.body,
                "allowed_tools": list(document.allowed_tools),
                "version": document.version,
                "source": document.source_path,
            },
        )

    return SkillSpec(
        name=name,
        fn=run,
        description=document.description,
        long_description=document.body,
        inputs=document.inputs,
        allowed_tools=document.allowed_tools,
        side_effects=False,
        idempotent=True,
        surfaces=surfaces,
    )


def register_skill_md_dir(
    registry,
    directory: str | Path,
    *,
    namespace: str | None = None,
    surfaces: tuple[str, ...] = ("cli", "mcp", "agent_tool", "skill_md"),
) -> list[str]:
    """Register every skill document under ``directory``. Returns the names.

    Both layouts people use: ``<dir>/<name>/SKILL.md`` and ``<dir>/<name>.md``.
    A file that is not a skill document raises rather than being skipped —
    see :class:`SkillDocumentError`.
    """
    root = Path(directory)
    if not root.is_dir():
        raise SkillDocumentError(f"{root} is not a directory")

    candidates = sorted(
        {*root.glob("*/SKILL.md"), *root.glob("SKILL.md"), *root.glob("*.md")}
    )
    registered = []
    for candidate in candidates:
        spec = spec_from_document(
            read_skill_md(candidate), namespace=namespace, surfaces=surfaces
        )
        if registry.has(spec.name):
            continue
        registry.register_skill(spec, mutating=False)
        registered.append(spec.name)
    return registered
