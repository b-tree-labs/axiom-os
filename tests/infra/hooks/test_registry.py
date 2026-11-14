# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for HookRegistry — manifest + filesystem-drop discovery."""

from __future__ import annotations

import textwrap
from pathlib import Path

from axiom.extensions.contracts import parse_manifest
from axiom.infra.bus import EventBus
from axiom.infra.hooks import HookBus
from axiom.infra.hooks.registry import (
    HookRegistry,
    discover_filesystem_hooks,
    discover_manifest_hooks,
)


def _write_manifest(root: Path, name: str, body: str) -> Path:
    ext_dir = root / name
    ext_dir.mkdir(parents=True, exist_ok=True)
    manifest = ext_dir / "axiom-extension.toml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


# Module-level test hooks so importable entry strings resolve.
def _interceptor_for_test(ctx):
    from axiom.infra.hooks import allow

    _interceptor_for_test.calls.append(ctx.event)
    return allow()


_interceptor_for_test.calls = []  # type: ignore[attr-defined]


def _observer_for_test(subject, payload):
    _observer_for_test.calls.append((subject, dict(payload)))


_observer_for_test.calls = []  # type: ignore[attr-defined]


class TestManifestDiscovery:
    def test_discovers_interceptor_hook(self, tmp_path):
        manifest_path = _write_manifest(
            tmp_path,
            "ext_a",
            textwrap.dedent(
                """
                [extension]
                name = "ext_a"
                version = "0.1.0"
                description = "test"
                license = "Apache-2.0"
                aeos_version = "0.1.0"

                [[extension.provides]]
                kind = "hook"
                events = ["tool.pre_invoke"]
                entry = "tests.infra.hooks.test_registry:_interceptor_for_test"
                priority = 50
                fail_mode = "warn"
                description = "rate limiter"
                """,
            ).strip(),
        )

        ext = parse_manifest(manifest_path)
        specs, observers = discover_manifest_hooks([ext])
        assert len(specs) == 1
        assert specs[0].event == "tool.pre_invoke"
        assert specs[0].priority == 50
        assert specs[0].fail_mode == "warn"
        assert specs[0].source == "ext_a"
        assert observers == []

    def test_discovers_observer_hook(self, tmp_path):
        manifest_path = _write_manifest(
            tmp_path,
            "ext_b",
            textwrap.dedent(
                """
                [extension]
                name = "ext_b"
                version = "0.1.0"
                description = "test"
                license = "Apache-2.0"
                aeos_version = "0.1.0"

                [[extension.provides]]
                kind = "hook"
                events = ["tool.post_invoke"]
                entry = "tests.infra.hooks.test_registry:_observer_for_test"
                fail_mode = "warn"
                description = "audit log"
                """,
            ).strip(),
        )
        ext = parse_manifest(manifest_path)
        specs, observers = discover_manifest_hooks([ext])
        assert specs == []  # tool.post_invoke is observer-only
        assert len(observers) == 1
        sub_event, sub_pattern, _, _, _ = observers[0]
        assert sub_event == "tool.post_invoke"
        assert sub_pattern == "tool.post_invoke"

    def test_extension_namespaced_event_routes_to_observer(self, tmp_path):
        # Extensions own their own event namespaces per spec §4. A hook
        # declared against `tidy.pressure_critical` is a valid extension-
        # event observer; the registry routes it to EventBus.subscribe.
        manifest_path = _write_manifest(
            tmp_path,
            "ext_c",
            textwrap.dedent(
                """
                [extension]
                name = "ext_c"
                version = "0.1.0"
                description = "test"
                license = "Apache-2.0"
                aeos_version = "0.1.0"

                [[extension.provides]]
                kind = "hook"
                events = ["tidy.pressure_critical"]
                entry = "tests.infra.hooks.test_registry:_observer_for_test"
                """,
            ).strip(),
        )
        ext = parse_manifest(manifest_path)
        specs, observers = discover_manifest_hooks([ext])
        assert specs == []
        assert len(observers) == 1
        assert observers[0][0] == "tidy.pressure_critical"

    def test_pattern_event_routes_to_observer(self, tmp_path):
        # NATS-shape patterns (`cli.*`, `doctor.>`) are valid subscription
        # patterns; the registry passes them through to EventBus.subscribe.
        manifest_path = _write_manifest(
            tmp_path,
            "ext_pattern",
            textwrap.dedent(
                """
                [extension]
                name = "ext_pattern"
                version = "0.1.0"
                description = "test"
                license = "Apache-2.0"
                aeos_version = "0.1.0"

                [[extension.provides]]
                kind = "hook"
                events = ["cli.*"]
                entry = "tests.infra.hooks.test_registry:_observer_for_test"
                """,
            ).strip(),
        )
        # The schema accepts `*` in hook events (NATS-shape pattern); the
        # runtime parser doesn't enforce the schema by itself, so emulate
        # the post-parse path directly to confirm routing.
        from axiom.extensions.contracts import Extension as _Ext
        from axiom.extensions.contracts import HookDef

        # Read the manifest so the body parses without error (smoke check).
        parse_manifest(manifest_path)

        fake_ext = _Ext(
            name="ext_pattern",
            version="0.1.0",
            description="test",
            author="",
            root=tmp_path,
        )
        fake_ext.hooks.append(
            HookDef(
                events=["cli.*"],
                entry="tests.infra.hooks.test_registry:_observer_for_test",
            )
        )
        specs, observers = discover_manifest_hooks([fake_ext])
        assert specs == []
        assert len(observers) == 1
        assert observers[0][1] == "cli.*"


class TestFilesystemDiscovery:
    def test_user_dir_interceptor(self, tmp_path):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        (hook_dir / "tool.pre_invoke.py").write_text(
            textwrap.dedent(
                """
                from axiom.infra.hooks import allow

                def hook(ctx):
                    return allow()
                """,
            ),
            encoding="utf-8",
        )
        specs, observers = discover_filesystem_hooks(
            user_dir=hook_dir,
            project_dir=None,
            trust_project=False,
        )
        assert len(specs) == 1
        assert specs[0].event == "tool.pre_invoke"
        assert specs[0].source == "user"
        assert observers == []

    def test_user_dir_observer(self, tmp_path):
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        (hook_dir / "tool.post_invoke.py").write_text(
            textwrap.dedent(
                """
                def observer(subject, data):
                    pass
                """,
            ),
            encoding="utf-8",
        )
        specs, observers = discover_filesystem_hooks(
            user_dir=hook_dir,
            project_dir=None,
            trust_project=False,
        )
        assert specs == []
        assert len(observers) == 1
        sub_event, _, _, _, src = observers[0]
        assert sub_event == "tool.post_invoke"
        assert src == "user"

    def test_project_dir_requires_trust(self, tmp_path):
        hook_dir = tmp_path / ".axiom" / "hooks"
        hook_dir.mkdir(parents=True)
        (hook_dir / "tool.pre_invoke.py").write_text(
            "from axiom.infra.hooks import allow\n"
            "def hook(ctx):\n"
            "    return allow()\n",
            encoding="utf-8",
        )

        # Untrusted: skipped
        specs, observers = discover_filesystem_hooks(
            user_dir=None,
            project_dir=hook_dir,
            trust_project=False,
        )
        assert specs == []
        assert observers == []

        # Trusted: discovered
        specs, observers = discover_filesystem_hooks(
            user_dir=None,
            project_dir=hook_dir,
            trust_project=True,
        )
        assert len(specs) == 1
        assert specs[0].source == "project"


class TestRegistryWiring:
    def test_register_routes_to_correct_primitive(self, tmp_path):
        manifest_path = _write_manifest(
            tmp_path,
            "ext_routing",
            textwrap.dedent(
                """
                [extension]
                name = "ext_routing"
                version = "0.1.0"
                description = "test"
                license = "Apache-2.0"
                aeos_version = "0.1.0"

                [[extension.provides]]
                kind = "hook"
                events = ["tool.pre_invoke"]
                entry = "tests.infra.hooks.test_registry:_interceptor_for_test"

                [[extension.provides]]
                kind = "hook"
                events = ["tool.post_invoke"]
                entry = "tests.infra.hooks.test_registry:_observer_for_test"
                """,
            ).strip(),
        )
        ext = parse_manifest(manifest_path)
        hookbus = HookBus()
        eventbus = EventBus()

        registry = HookRegistry(hookbus=hookbus, eventbus=eventbus)
        summary = registry.register_extensions([ext])
        assert summary.interceptors == 1
        assert summary.observers == 1
        assert hookbus.hooks_for("tool.pre_invoke")
        assert eventbus.subscribers_for("tool.post_invoke")

    def test_summary_string(self):
        hookbus = HookBus()
        eventbus = EventBus()
        registry = HookRegistry(hookbus=hookbus, eventbus=eventbus)
        summary = registry.register_extensions([])
        assert "0 hooks" in str(summary)
