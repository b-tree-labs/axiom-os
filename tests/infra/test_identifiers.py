# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for identifier generation helpers.

Axiom UX principle (feedback_auto_generated_ids.md): create flows
never force users to conjure IDs. Generate uuid + human-readable
slug by default; user can rename later.
"""

from __future__ import annotations

import re


class TestSlugify:
    def test_lowercase_and_hyphenate(self):
        from axiom.infra.identifiers import slugify

        assert slugify("NE Prague 2026") == "ne-prague-2026"

    def test_strip_punctuation(self):
        from axiom.infra.identifiers import slugify

        assert slugify("Thermo 101: Intro") == "thermo-101-intro"

    def test_collapse_whitespace(self):
        from axiom.infra.identifiers import slugify

        assert slugify("  Lots    of   spaces  ") == "lots-of-spaces"

    def test_empty_input_returns_placeholder(self):
        from axiom.infra.identifiers import slugify

        assert slugify("") == "untitled"

    def test_unicode_transliterated(self):
        from axiom.infra.identifiers import slugify

        # Basic ASCII fallback; diacritics dropped
        result = slugify("Café Müller")
        assert re.fullmatch(r"[a-z0-9-]+", result)
        assert "cafe" in result or "caf" in result


class TestGenerateId:
    def test_uuid_format(self):
        from axiom.infra.identifiers import generate_id

        new_id = generate_id()
        # uuid4 is 36 chars with dashes
        assert re.fullmatch(r"[0-9a-f-]+", new_id)
        assert len(new_id) == 36

    def test_unique_per_call(self):
        from axiom.infra.identifiers import generate_id

        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100


class TestShortSuffix:
    def test_from_uuid(self):
        from axiom.infra.identifiers import generate_id, short_suffix

        new_id = generate_id()
        suffix = short_suffix(new_id)
        assert len(suffix) == 6
        assert re.fullmatch(r"[0-9a-f]+", suffix)

    def test_suffix_deterministic_per_uuid(self):
        from axiom.infra.identifiers import short_suffix

        u = "12345678-1234-1234-1234-123456789abc"
        assert short_suffix(u) == short_suffix(u)


class TestDefaultSlug:
    def test_slug_from_title_with_suffix(self):
        from axiom.infra.identifiers import default_slug, generate_id

        new_id = generate_id()
        slug = default_slug(title="NE Prague 2026", uuid_str=new_id)
        # "ne-prague-2026-<6 hex>"
        assert slug.startswith("ne-prague-2026-")
        suffix = slug.rsplit("-", 1)[-1]
        assert len(suffix) == 6

    def test_slug_untitled_when_no_title(self):
        from axiom.infra.identifiers import default_slug, generate_id

        slug = default_slug(title=None, uuid_str=generate_id())
        assert slug.startswith("untitled-")


class TestCreateIdentity:
    def test_returns_uuid_plus_slug(self):
        from axiom.infra.identifiers import create_identity

        identity = create_identity(title="NE Prague 2026")
        assert "id" in identity
        assert "slug" in identity
        assert identity["slug"].startswith("ne-prague-2026-")
        assert len(identity["id"]) == 36

    def test_custom_slug_used_verbatim(self):
        from axiom.infra.identifiers import create_identity

        identity = create_identity(title="X", slug="my-custom-slug")
        assert identity["slug"] == "my-custom-slug"

    def test_custom_slug_slugified(self):
        from axiom.infra.identifiers import create_identity

        identity = create_identity(title="X", slug="Upper CASE Mess!")
        assert identity["slug"] == "upper-case-mess"
