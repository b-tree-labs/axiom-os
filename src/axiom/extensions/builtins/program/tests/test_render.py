# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.render`` — the data file becomes one static status page.

The page is the data file made readable, nothing more: every item id and
every link present, nothing injected, stdlib only.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.program.skills import render


def _render(data_file: Path, out: Path, ctx):
    return render.run({"data": str(data_file), "out": str(out)}, ctx)


class TestThePage:
    def test_one_page_lands_in_the_output_dir(self, data_file, tmp_path, ctx):
        out = tmp_path / "site"
        result = _render(data_file, out, ctx)
        assert result.ok
        page = Path(result.value["page"])
        assert page == out / "status.html"
        assert page.exists()

    def test_every_item_id_is_on_the_page(self, data_file, tmp_path, ctx):
        result = _render(data_file, tmp_path / "site", ctx)
        html = Path(result.value["page"]).read_text(encoding="utf-8")
        for item_id in ("i-one", "i-two", "i-three", "i-four"):
            assert f'id="item-{item_id}"' in html, item_id
            assert item_id in html

    def test_every_label_and_owner_is_on_the_page(self, data_file, tmp_path, ctx):
        result = _render(data_file, tmp_path / "site", ctx)
        html = Path(result.value["page"]).read_text(encoding="utf-8")
        for text in (
            "First increment",
            "Second increment",
            "Demo day",
            "Fixed window",
            "@casey:example-org",
            "@dana:example-org",
        ):
            assert text in html, text

    def test_every_link_is_on_the_page(self, data_file, tmp_path, ctx):
        """URL fields render as anchors; tracker issues render as the
        host-qualified reference the data file implies."""
        result = _render(data_file, tmp_path / "site", ctx)
        html = Path(result.value["page"]).read_text(encoding="utf-8")
        assert 'href="https://docs.example.org/design"' in html
        assert "tracker.example.org/7#42" in html
        assert "tracker.example.org/7#57" in html

    def test_markup_in_data_is_escaped_not_executed(self, write_data, data_dict, tmp_path, ctx):
        data_dict["schedule"][0]["label"] = "<script>alert('x')</script>"
        result = _render(write_data(data_dict), tmp_path / "site", ctx)
        html = Path(result.value["page"]).read_text(encoding="utf-8")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_rendering_twice_overwrites_cleanly(self, data_file, tmp_path, ctx):
        out = tmp_path / "site"
        first = _render(data_file, out, ctx)
        second = _render(data_file, out, ctx)
        assert first.ok and second.ok
        assert first.value["page"] == second.value["page"]


class TestRefusals:
    def test_a_missing_data_file_is_a_refusal(self, tmp_path, ctx):
        result = render.run(
            {"data": str(tmp_path / "absent.json"), "out": str(tmp_path / "site")}, ctx
        )
        assert not result.ok
        assert not (tmp_path / "site" / "status.html").exists()

    def test_an_invalid_data_file_renders_nothing(self, write_data, data_dict, tmp_path, ctx):
        data_dict["schedule"][0]["pct"] = 999
        result = render.run(
            {"data": str(write_data(data_dict)), "out": str(tmp_path / "site")}, ctx
        )
        assert not result.ok
        assert not (tmp_path / "site" / "status.html").exists()
