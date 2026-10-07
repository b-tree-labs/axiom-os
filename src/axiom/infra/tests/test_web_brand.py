# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One node, one product name, on every web surface it serves.

THE DEFECT THIS EXISTS TO FIX: a node started with ``AXIOM_BRAND_NAME`` set
served its application surface under that name and its sign-in page under
"Axiom". The two surfaces resolved their brand from different places — the app
mount read the environment, the gate router took a constructor argument nobody
passed — so the FIRST page a person ever saw carried the wrong product name,
and the right one appeared only after they had signed in.

The fix is one resolver both surfaces call. These tests are written against
that resolver and against each surface's use of it, because a shared helper
that one caller quietly stops calling is the same bug wearing a hat.
"""

from axiom.infra.web_brand import DEFAULT_ACCENT, WebBrand, resolve_web_brand


def test_defaults_to_the_platform_when_nothing_is_set():
    brand = resolve_web_brand(env={})
    assert brand.product_name == "Axiom"
    assert brand.accent == DEFAULT_ACCENT


def test_the_environment_names_the_product():
    brand = resolve_web_brand(env={"AXIOM_BRAND_NAME": "Acme OS"})
    assert brand.product_name == "Acme OS"
    # An unset accent keeps the platform's, rather than blanking it.
    assert brand.accent == DEFAULT_ACCENT


def test_the_environment_sets_the_accent():
    brand = resolve_web_brand(env={"AXIOM_BRAND_ACCENT": "#123456"})
    assert brand.accent == "#123456"


def test_an_empty_value_is_not_a_name():
    """`AXIOM_BRAND_NAME=` in a unit file is an unset knob, not a nameless
    product. Treating "" as a name renders a card with a blank heading and an
    empty initial tile, which reads as a broken page rather than a default."""
    brand = resolve_web_brand(env={"AXIOM_BRAND_NAME": "  ", "AXIOM_BRAND_ACCENT": ""})
    assert brand.product_name == "Axiom"
    assert brand.accent == DEFAULT_ACCENT


def test_resolution_happens_per_call_not_at_import(monkeypatch):
    """A module-level constant would freeze whatever was set when the first
    import happened, which in a test run is another test's environment and in
    production is whatever uvicorn inherited before the unit file was read."""
    monkeypatch.setenv("AXIOM_BRAND_NAME", "First")
    assert resolve_web_brand().product_name == "First"
    monkeypatch.setenv("AXIOM_BRAND_NAME", "Second")
    assert resolve_web_brand().product_name == "Second"


def test_reading_the_real_environment_is_the_default():
    """No argument means `os.environ` — the production path."""
    assert isinstance(resolve_web_brand(), WebBrand)
