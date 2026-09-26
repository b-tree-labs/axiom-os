# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Emit the canonical compound AEOS layout to disk.

Called by :class:`axiom.cli.ext.commands.init.InitProvider`. Kept as a plain
function so unit tests can exercise it directly without constructing a full
argparse namespace.
"""

from __future__ import annotations

from pathlib import Path

_COPY = (
    "# Copyright (c) 2026 The University of Texas at Austin\n"
    "# Copyright (c) 2026 B-Tree Labs\n"
    "# SPDX-License-Identifier: Apache-2.0\n"
)

# Capability-kind subdirectories mandated by AEOS §5.1. All seven are created
# empty; extension authors populate the ones they use and delete the rest via
# `git rm` during development.
_CAPABILITY_DIRS: tuple[str, ...] = (
    "agents",
    "tools",
    "commands",
    "services",
    "adapters",
    "skills",
    "hooks",
)


def create(
    ext_dir: Path,
    *,
    name: str,
    owner: str,
    license: str,
    description: str,
) -> None:
    """Materialize the canonical layout at ``ext_dir``."""
    ext_dir.mkdir(parents=True, exist_ok=False)

    _write_package_tree(ext_dir, name=name)
    _write_tests_tree(ext_dir, name=name)
    _write_docs_tree(ext_dir, name=name, description=description)

    # Top-level required files
    (ext_dir / "README.md").write_text(_readme(name, description), encoding="utf-8")
    (ext_dir / "CHANGELOG.md").write_text(_changelog(), encoding="utf-8")
    (ext_dir / "LICENSE").write_text(_apache_2_license(), encoding="utf-8")
    (ext_dir / "AGENTS.md").write_text(_agents_md(name), encoding="utf-8")
    (ext_dir / "pyproject.toml").write_text(
        _pyproject(name=name, description=description, license=license),
        encoding="utf-8",
    )
    (ext_dir / "axiom-extension.toml").write_text(
        _manifest(name=name, owner=owner, license=license, description=description),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Package tree: <ext>/<ext>/...
# ---------------------------------------------------------------------------


def _write_package_tree(ext_dir: Path, *, name: str) -> None:
    pkg = ext_dir / name
    pkg.mkdir()

    # Public API surface — empty by default; authors add exports as they go.
    (pkg / "__init__.py").write_text(_package_init(name), encoding="utf-8")

    # PEP 561 marker — shipped as per AEOS §5.1
    (pkg / "py.typed").write_text("", encoding="utf-8")

    # Seven capability-kind subdirectories
    for kind in _CAPABILITY_DIRS:
        sub = pkg / kind
        sub.mkdir()
        (sub / ".gitkeep").write_text("", encoding="utf-8")

    # Placeholder cmd so the manifest's required [[extension.provides]] block
    # resolves to a real callable — otherwise `axi ext scan` fails the
    # manifest/pyproject alignment check on a fresh scaffold. Replace this
    # (and its manifest + pyproject entries) before publishing.
    commands = pkg / "commands"
    (commands / "__init__.py").write_text(_COPY, encoding="utf-8")
    (commands / "placeholder.py").write_text(_placeholder_cmd(name), encoding="utf-8")
    # The .gitkeep is no longer load-bearing for the commands/ dir, but keep
    # it for consistency with the other six capability dirs.

    # Strictly private internals
    internal = pkg / "_internal"
    internal.mkdir()
    (internal / "__init__.py").write_text(_internal_init(), encoding="utf-8")


def _placeholder_cmd(name: str) -> str:
    return placeholder_module_body(name)


def placeholder_module_body(name: str) -> str:
    """Text for the ``commands/placeholder.py`` module.

    Exposed publicly so ``axi ext doctor --fix`` can regenerate a missing
    placeholder module without reaching into the template's internals.
    """
    return (
        _COPY
        + f'"""Placeholder cmd capability for {name}.\n\n'
        "Shipped by ``axi ext init`` so the manifest's required provides block\n"
        "resolves to a real callable from day one. Replace this module (and\n"
        "its manifest + pyproject entries) before publishing.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n\n"
        "def cli(argv: list[str] | None = None) -> int:\n"
        f'    """Placeholder cmd — replace me. Returns 0 and prints a hint."""\n'
        f'    print("{name}: placeholder cmd — replace with a real implementation.")\n'
        "    return 0\n"
    )


def _package_init(name: str) -> str:
    return (
        _COPY
        + f'"""{name} — an AEOS-conformant Axiom extension.\n\n'
        "Add public symbols to ``__all__`` as you implement capabilities.\n"
        "Anything not listed here is private per AEOS §7.3.\n"
        '"""\n\n'
        "__all__: list[str] = []\n"
    )


def _internal_init() -> str:
    return (
        _COPY
        + '"""Strictly private internals — not part of the public extension API."""\n'
    )


# ---------------------------------------------------------------------------
# Tests tree
# ---------------------------------------------------------------------------


def _write_tests_tree(ext_dir: Path, *, name: str) -> None:
    tests = ext_dir / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(_conftest(), encoding="utf-8")

    unit = tests / "unit_tests"
    unit.mkdir()
    (unit / "__init__.py").write_text(_COPY, encoding="utf-8")
    (unit / "test_standard.py").write_text(_test_standard(name), encoding="utf-8")

    integ = tests / "integration_tests"
    integ.mkdir()
    (integ / "__init__.py").write_text(_COPY, encoding="utf-8")
    (integ / ".gitkeep").write_text("", encoding="utf-8")

    fixtures = tests / "fixtures"
    fixtures.mkdir()
    (fixtures / ".gitkeep").write_text("", encoding="utf-8")


def _conftest() -> str:
    return (
        _COPY
        + '"""Test config for this extension.\n\n'
        "``axiom-tests`` is a ``pytest11`` plugin — installing it in the active\n"
        "environment is sufficient for its fixtures to be available here. This\n"
        "file exists so extension authors have an obvious place to add local\n"
        "fixtures as the suite grows.\n"
        '"""\n'
    )


def _test_standard(name: str) -> str:
    return (
        _COPY
        + '"""Standard AEOS conformance tests for this extension.\n\n'
        "Inherits from ``axiom_tests.unit_tests.ExtensionStandardTests`` which\n"
        "validates the manifest, required files, and public API declarations.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "from axiom_tests.unit_tests import ExtensionStandardTests\n\n\n"
        f"class Test{_camel(name)}Standard(ExtensionStandardTests):\n"
        "    @pytest.fixture\n"
        "    def extension_manifest_path(self) -> Path:\n"
        "        return Path(__file__).parent.parent.parent / \"axiom-extension.toml\"\n"
    )


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


# ---------------------------------------------------------------------------
# Docs tree
# ---------------------------------------------------------------------------


def _write_docs_tree(ext_dir: Path, *, name: str, description: str) -> None:
    docs = ext_dir / "docs"
    docs.mkdir()
    for sub in ("prds", "specs", "decisions", "working", "reference"):
        (docs / sub).mkdir()
        (docs / sub / ".gitkeep").write_text("", encoding="utf-8")
    (docs / "overview.md").write_text(
        f"# {name}\n\n{description}\n\n"
        "## Overview\n\n"
        "Document the purpose, scope, and capabilities of this extension here.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Top-level files
# ---------------------------------------------------------------------------


def _readme(name: str, description: str) -> str:
    return (
        f"# {name}\n\n"
        f"{description}\n\n"
        "This extension conforms to the Agent Extension Open Standard (AEOS) 0.1.\n\n"
        "## Getting started\n\n"
        "```bash\n"
        "pip install -e .\n"
        "axi ext lint          # Bronze conformance check\n"
        "axi ext test          # Run the standard tests\n"
        "```\n\n"
        "## Layout\n\n"
        "This extension uses the canonical compound layout from AEOS §5.1.\n"
        f"The Python package lives at ``{name}/`` with capability-kind\n"
        "subdirectories for agents, tools, commands, services, adapters, skills,\n"
        "and hooks. Populate the directories you use and delete the rest.\n"
    )


def _changelog() -> str:
    return (
        "# Changelog\n\n"
        "All notable changes to this extension are documented here.\n\n"
        "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
        "and this extension adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
        "## [Unreleased]\n\n"
        "### Added\n"
        "- Nothing yet.\n"
    )


def _agents_md(name: str) -> str:
    return (
        f"# {name} — coding-agent guidance\n\n"
        "This extension follows AEOS 0.1. When editing:\n\n"
        "- Put new capabilities under the appropriate subdirectory\n"
        f"  (``{name}/agents/``, ``{name}/tools/``, etc.) and declare them in\n"
        "  ``axiom-extension.toml``.\n"
        "- Keep the public surface narrow: only items listed in\n"
        f"  ``{name}/__init__.py``'s ``__all__`` may be imported by other\n"
        "  extensions.\n"
        "- Run ``axi ext lint`` before committing structural changes.\n\n"
        "## Operational output paths (runtime artifacts)\n\n"
        f"When this extension's agent writes operational output — heartbeat\n"
        "JSON, health reports, debug dumps, cron logs — resolve the path via\n"
        "``axiom.infra.paths.get_agent_output_dir(agent_name)``. That returns\n"
        "``<project_root>/runtime/agent-output/<agent_name>/`` and the\n"
        "consuming repo's root ``.gitignore`` should include\n"
        "``runtime/agent-output/`` so the single line covers every agent.\n\n"
        "Picking a bespoke path under ``runtime/`` is a known failure mode —\n"
        "the consumer's ``.gitignore`` will not include it, and the operational\n"
        "output accumulates as untracked file noise until somebody audits.\n"
    )


def _pyproject(*, name: str, description: str, license: str) -> str:
    return (
        f'# Copyright (c) 2026 The University of Texas at Austin\n'
        f'# Copyright (c) 2026 B-Tree Labs\n'
        f'# SPDX-License-Identifier: {license}\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        f'description = "{description}"\n'
        'readme = "README.md"\n'
        f'license = "{license}"\n'
        'requires-python = ">=3.11"\n\n'
        "# Capability entry points — add one entry per [[extension.provides]]\n"
        "# block in axiom-extension.toml. Keep the two files in sync; `axi ext\n"
        "# validate` verifies the mapping.\n"
        '[project.entry-points."axiom.agents"]\n\n'
        '[project.entry-points."axiom.tools"]\n\n'
        '[project.entry-points."axiom.commands"]\n'
        f'{name} = "{name}.commands.placeholder:cli"\n\n'
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{name}"]\n'
    )


def _manifest(*, name: str, owner: str, license: str, description: str) -> str:
    # NOTE: every AEOS manifest must declare at least one [[extension.provides]]
    # block (§6.2). The scaffold emits a placeholder `cmd` capability bound to
    # a `cli` symbol in the package's public API; replace it with whatever you
    # actually ship as soon as you add real capabilities.
    return (
        "# AEOS Manifest — axiom-extension.toml\n"
        "# See: docs/specs/spec-aeos-0.1.md §6 for the full schema.\n\n"
        "[extension]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        f'description = "{description}"\n'
        f'owner = "{owner}"\n'
        f'license = "{license}"\n'
        'aeos_version = "0.1.0"\n\n'
        "[extension.compatibility]\n"
        'python = ">= 3.11"\n'
        'axiom = ">= 0.10"\n\n'
        "# Replace this placeholder with real capabilities as you add them.\n"
        "# The scaffold emits a minimal ``cmd`` entry so the manifest satisfies\n"
        "# AEOS §6.2 (at least one provides block) from day one.\n"
        "#\n"
        "# `tier` + `intent_groups` drive progressive disclosure in `axi --help`\n"
        "# and `neut --help` (axiom/cli/help_engine.py, prd-axi-cli.md\n"
        "# §Progressive Disclosure). Defaults below give the placeholder maximum\n"
        "# visibility during early development:\n"
        "#   tier=\"starter\"           — visible at every user's default\n"
        "#                              competency ceiling.\n"
        "#   intent_groups=[\"start\"] — `start` is the universal end-user floor\n"
        "#                              every role inherits, so every role sees\n"
        "#                              this command.\n"
        "# Narrow both as the extension's audience clarifies:\n"
        "#   - Operator-only?         intent_groups = [\"operate\"]\n"
        "#   - Researcher + operator? intent_groups = [\"research\", \"operate\"]\n"
        "#   - Power-user surface?    tier = \"core\" (or \"advanced\")\n"
        "#   - Internal tooling?      tier = \"internal\"\n"
        "[[extension.provides]]\n"
        'kind = "cmd"\n'
        f'noun = "{name}"\n'
        f'entry = "{name}.commands.placeholder:cli"\n'
        f'description = "Placeholder CLI for {name}; replace before publishing."\n'
        'tier = "starter"\n'
        'intent_groups = ["start"]\n\n'
        "# MCP exposure (spec-builtin-mcp-server.md §6.3, §7).\n"
        "# `axi ext lint` requires every extension to either declare an\n"
        "# [extension.mcp] block or carry a `# mcp: not-applicable -- <reason>`\n"
        "# comment. The scaffold opts in by default; flip `enabled = false` to\n"
        "# opt out, or replace this whole block with the comment form.\n"
        "[extension.mcp]\n"
        "enabled = true\n"
    )


# ---------------------------------------------------------------------------
# Apache-2.0 license text (short canonical form)
# ---------------------------------------------------------------------------


def _apache_2_license() -> str:
    # Include a trimmed notice header — the full license body is embedded
    # verbatim so the LICENSE file is a faithful reproduction of Apache-2.0.
    return _APACHE_2_0_LICENSE


_APACHE_2_0_LICENSE = """\
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for describing the origin of the Work and
      reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Support. While redistributing the Work or
      Derivative Works thereof, You may choose to offer, and charge a
      fee for, acceptance of support, warranty, indemnity, or other
      liability obligations and/or rights consistent with this License.
      However, in accepting such obligations, You may act only on Your
      own behalf and on Your sole responsibility, not on behalf of any
      other Contributor, and only if You agree to indemnify, defend,
      and hold each Contributor harmless for any liability incurred by,
      or claims asserted against, such Contributor by reason of your
      accepting any such warranty or support.

   END OF TERMS AND CONDITIONS

   Copyright 2026 The University of Texas at Austin and B-Tree Labs

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""


__all__ = ["create", "placeholder_module_body"]
