# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every surface follows one serving mode, and a bound web app forwards shared reads (ADR-207).

One process plays both installs. The node is the composed serving app with its data read mount,
its document search mount and its ``/api/v1`` behind the real authz hook, and a key minted the way
the node mints one. The laptop holds no medallion: it has a ``site-bindings.json`` naming the node
and that key, and its own web app (``/api/v1``) answers the browser. Every HTTP call the laptop
makes goes to the node's app; nothing connects anywhere else.

The rules tested here, by number in the ADR:

1. chat, documents, the CLI data read, MCP and the web app report the same mode (parity);
2. a node that cannot be reached is a refusal naming it, and nothing local is shown;
3. test data is marked on the mode report, every catalog and figure response, and the chat stamp;
4. only reads are forwarded, from the explicit list;
5. the key never reaches the browser, a response, or a log line.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform import read_api, read_target
from axiom.extensions.builtins.data_platform.services.read_mount import (
    mount_spec as data_read_mount,
)
from axiom.extensions.builtins.data_platform.skills import gold
from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.mounts import rag_search_mount_spec
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry
from axiom.extensions.builtins.webapp import bound_web
from axiom.extensions.builtins.webapp.api.routers import build_api_router
from axiom.governance import Decision, Verdict
from axiom.infra import serving_mode
from axiom.infra.skills import SkillResult
from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

SITE = "alpha-site"
FACE = "https://node.example.org"
NODE_HOST = "node.example.org"
TOKEN_REF = "openbao://kv/data/alpha-reader-key"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

#: Which install is running right now: the node holds the medallion, the laptop does not.
# How many requests are inside the node right now. A count, not a flag: the lane asks the node for
# the catalogue's three columns from three threads at once, and a flag cleared by the first to
# finish made the others read as "this machine holds the medallion" (#1479, CI only: it takes
# overlapping requests, which a slow runner produces and a fast laptop mostly does not).
_SIDE = {"inside": 0}
_SIDE_LOCK = threading.Lock()


def _on_the_node() -> bool:
    return _SIDE["inside"] > 0


# --------------------------------------------------------------------------- the node


@pytest.fixture
def node(tmp_path, monkeypatch):
    """The composed node app behind the real authz hook, and the key it issued to a reader."""
    for name in ("AXIOM_HOME_SITES", "AXIOM_FED_DEV_ACCEPT", "DATABASE_URL", "AXIOM_SITE",
                 "AXIOM_SAMPLE_DATA", "AXIOM_VIEW_AS", "AXIOM_RAG_REMOTE_SIGNATURE",
                 "AXIOM_RAG_REMOTE_NODE_ID"):
        monkeypatch.delenv(name, raising=False)
    keys_file = tmp_path / "node-keys.json"
    token, record = mint_api_key(principal="@reader", scopes=["*:read"], name="laptop", site=SITE)
    append_api_key_record(keys_file, record)

    registry = RouterRegistry()
    registry.register(data_read_mount())
    registry.register(rag_search_mount_spec(lambda: _NoDocuments(), embed_fn=lambda _q: None))
    registry.register(MountSpec(prefix="/api/v1", router=build_api_router(), extension="webapp",
                                requires_authz=True, profiles=("server",)))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys_file)),
        decide_fn=lambda _env: Verdict.from_decision(Decision.PERMIT, "test permits", "t-1"),
    )
    app = compose_app(registry=registry, include_builtins=False,
                      middleware=MiddlewareConfig(authz=hook), auto_authz=False)

    asked: list[tuple[str, str]] = []

    def aggregate(params, ctx):
        asked.append(("data.aggregate", ctx.principal.handle))
        column = params.get("column")
        value = {"rows": 1440, "first_ts": "2026-10-26T12:00:00Z",
                 "last_ts": "2026-10-27T12:00:00Z"}.get(column)
        return SkillResult(ok=True, value={"data": {"groups": [
            {"site": SITE, "feed": "plant", "channel": "flow-a", "unit": "L/s", "value": value},
        ]}})

    def series(params, ctx):
        asked.append(("data.series", ctx.principal.handle))
        points = [{"t": (NOW - timedelta(minutes=m)).isoformat(), "value": 10.0 + m}
                  for m in range(0, 60, 5)]
        return SkillResult(ok=True, value={"data": {"series": [
            {"key": {"feed": "plant", "channel": "flow-a"}, "points": points}]}})

    monkeypatch.setattr(gold, "aggregate", aggregate)
    monkeypatch.setattr(gold, "series", series)
    return SimpleNamespace(client=TestClient(app), key=token, asked=asked, up={"up": True})


class _NoDocuments:
    def search(self, *a, **k):
        return []


# --------------------------------------------------------------------------- the lane


class _DoorLane:
    """A minimal figure lane over the door, in the shape ``NodeFigureSource`` answers.

    The real lane (``scidisplay.node_figure_source``) is built from the binding in production. This
    one reads the same two verbs through the same :class:`bound_web.BoundDoor`, so what is tested is
    the forwarding, the allowlist, the key and the refusals, not the drawing.
    """

    def __init__(self, door):
        self.door = door

    def _catalogue(self):
        out = {}
        for column in ("rows", "first_ts", "last_ts"):
            value = self.door.query("data.aggregate", {"table": "signal_catalogue", "column": column,
                                                       "fn": "sum", "group_by": ["site", "feed"]})
            for g in value["data"]["groups"]:
                out.setdefault((g["site"], g["feed"], g["channel"], g["unit"]), {})[column] = g["value"]
        return out

    def sites(self):
        return [{"id": k[0], "label": k[0], "rows": v["rows"]} for k, v in self._catalogue().items()]

    def channels(self, site):
        return [{"feed": k[1], "channel": k[2], "unit": k[3], "rows": v["rows"],
                 "first": v["first_ts"], "last": v["last_ts"]}
                for k, v in self._catalogue().items() if k[0] == site]

    def revision(self, site):
        return max(v["last_ts"] for k, v in self._catalogue().items() if k[0] == site)

    def resolve(self, site, tokens, feed=""):
        return [SimpleNamespace(site=site, feed=k[1], channel=k[2])
                for k in self._catalogue() if k[0] == site and k[2] in tokens]

    def read(self, site, chans, start, end, bucket):
        value = self.door.query("data.series", {"table": "signals", "column": "value"})
        out = {}
        for group in value["data"]["series"]:
            key = (group["key"]["feed"], group["key"]["channel"])
            out[key] = [(datetime.fromisoformat(p["t"]), p["value"]) for p in group["points"]]
        return out

    def dates(self, site, tokens, feed=""):
        return [{"date": "2026-10-27", "rows": 12}]

    def figure(self, site, tokens, **kw):
        got = self.read(site, self.resolve(site, tokens), None, None, 300)
        return {"svg": "<svg/>", "window": {"from": "a", "to": "b"},
                "readout": {"series": [{"name": c, "points": len(p)} for (_f, c), p in got.items()]},
                "notes": []}


# --------------------------------------------------------------------------- the laptop


def _bind(state) -> None:
    (state / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": SITE,
        "bindings": {SITE: {"site": SITE, "face_url": FACE, "token_ref": TOKEN_REF}},
    }), encoding="utf-8")


class _Response:
    def __init__(self, raw: bytes, status: int = 200) -> None:
        self._raw, self.status = raw, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._raw


@pytest.fixture
def laptop(node, tmp_path, monkeypatch):
    state = tmp_path / "laptop-state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    _bind(state)
    monkeypatch.setattr(read_target, "holds_medallion", _on_the_node)
    monkeypatch.setattr(read_api, "holds_medallion", _on_the_node)
    # The vault hands back the key the binding's token_ref names.
    monkeypatch.setattr(read_target, "_key", lambda _face: node.key)

    calls: list[str] = []

    def _on_node(fn):
        if not node.up["up"]:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with _SIDE_LOCK:
            _SIDE["inside"] += 1
        try:
            return fn()
        finally:
            with _SIDE_LOCK:
                _SIDE["inside"] -= 1

    def urlopen(request, timeout=None):  # the data door's transport
        assert request.full_url.startswith(FACE), request.full_url
        calls.append(request.full_url)
        response = _on_node(lambda: node.client.post(
            request.full_url[len(FACE):], content=request.data, headers=dict(request.header_items())))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "refused", {},
                                         io.BytesIO(response.content))
        return _Response(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def http_get(url, headers, timeout):  # the mode probe's transport
        assert url.startswith(FACE), url
        calls.append(url)
        response = _on_node(lambda: node.client.get(url[len(FACE):], headers=headers))
        return response.status_code, response.json()

    monkeypatch.setattr(serving_mode, "_http_get", http_get)

    def requests_get(url, headers=None, timeout=None):  # the chat model probe
        assert url.startswith(FACE), url
        return SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "node-model"}]})

    import requests

    monkeypatch.setattr(requests, "get", requests_get)

    # This machine's own catalog database: any opening is recorded, so a test can prove none was.
    opened: list[str] = []

    @contextmanager
    def session_scope():
        opened.append("local database")
        yield object()

    from axiom.extensions.builtins.webapp.catalog import store

    monkeypatch.setattr(store, "session_scope", session_scope)
    monkeypatch.setattr(store, "read_site_summaries", lambda _s: [
        {"site": "local-sample", "channels": 1, "rows": 1, "first": "", "last": ""}])
    monkeypatch.setattr(store, "read_channels", lambda _s, _site: [])
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore",
        lambda: SimpleNamespace(get=lambda key, default=None: default),
    )

    bound_web.set_lane_factory(_DoorLane)
    serving_mode.PROBES.clear()
    app = FastAPI()
    app.include_router(build_api_router())
    yield SimpleNamespace(state=state, web=TestClient(app), calls=calls, opened=opened)
    bound_web.set_lane_factory(None)
    serving_mode.PROBES.clear()


def _unbind(laptop) -> None:
    (laptop.state / read_target.STATE_FILENAME).unlink()
    serving_mode.PROBES.clear()


# --------------------------------------------------------------------------- 1. parity


def _surface_modes(laptop) -> dict[str, str]:
    """What each surface says the mode is, in its own vocabulary, mapped onto the four modes."""
    from axiom.extensions.builtins.chat import bound_model, where
    from axiom.extensions.builtins.mcp import platform_primitives
    from axiom.rag.remote_store import RemoteRetrievalStore
    from axiom.rag.store_factory import resolve_store_url

    resolved = serving_mode.resolve_mode().mode
    test = resolved == "test"
    cli = "bound" if read_target.resolve_read_target().where == "binding" else ("test" if test else "local")
    documents = "bound" if resolve_store_url().site else ("test" if test else "local")
    mcp = ("bound" if isinstance(platform_primitives._resolve_rag_store(), RemoteRetrievalStore)
           else ("test" if test else "local"))
    model = "bound" if bound_model.resolve_bound_model() is not None else ("test" if test else "local")
    web = laptop.web.get("/api/v1/binding").json()["mode"]
    sites = laptop.web.get("/api/v1/sites").json()
    charts = "bound" if (sites.get("served") or {}).get("mode") == "bound" else (
        "test" if sites.get("test_data") else "local")
    return {
        "resolver": resolved,
        "cli data read": cli,
        "chat sources": where.sources_of().mode,
        "chat model": model,
        "documents": documents,
        "mcp documents": mcp,
        "web mode report": web,
        "web charts": charts,
    }


@pytest.mark.parametrize("config", ["bound", "local", "test"])
def test_every_surface_reports_the_same_mode(laptop, monkeypatch, config):
    if config == "local":
        _unbind(laptop)
    if config == "test":
        monkeypatch.setenv("AXIOM_SAMPLE_DATA", "1")
    modes = _surface_modes(laptop)
    disagree = {surface: mode for surface, mode in modes.items() if mode != config}
    assert not disagree, f"surfaces disagree with {config!r}: {disagree}"


def test_a_local_store_setting_does_not_split_a_bound_install(laptop, monkeypatch):
    """The mixed mode the resolver removes: data from the site, documents from this machine."""
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore",
        lambda: SimpleNamespace(get=lambda key, default=None: (
            "postgresql://u@localhost/rag" if key == "rag.database_url" else default)),
    )
    modes = _surface_modes(laptop)
    assert set(modes.values()) == {"bound"}, modes


def test_bound_reads_reach_the_node_through_its_gate_with_the_keys_principal(laptop, node):
    body = laptop.web.get(f"/api/v1/sites/{SITE}/channels").json()
    assert body["served"] == {"mode": "bound", "site": SITE, "node": NODE_HOST, "key_site": SITE}
    assert body["channels"][0]["channel"] == "flow-a"
    # The node's gate resolved the binding's key to its principal: the read ran as the key.
    assert node.asked and all(who.startswith("@reader") for _verb, who in node.asked), node.asked
    assert laptop.opened == []  # the laptop's own database was never opened


# --------------------------------------------------------------------------- 2. fail closed


def test_an_unreachable_node_is_a_refusal_and_nothing_local_is_shown(laptop, node):
    node.up["up"] = False
    for path in ("/api/v1/sites", f"/api/v1/sites/{SITE}/channels",
                 f"/api/v1/sites/{SITE}/figure?channels=flow-a",
                 f"/api/v1/sites/{SITE}/series?feed=plant&channels=flow-a&bucket_s=300"):
        response = laptop.web.get(path)
        assert response.status_code == 503, (path, response.status_code, response.text)
        detail = response.json()["detail"]
        assert SITE in detail and NODE_HOST in detail and "unreachable" in detail, detail
    assert laptop.opened == [], "a local database was opened while bound"
    report = laptop.web.get("/api/v1/binding").json()
    assert report["mode"] == "bound" and report["reachable"] is False and report["error"]


def test_a_cached_answer_says_its_age(laptop, node):
    first = laptop.web.get(f"/api/v1/sites/{SITE}/channels").json()
    assert "cached_age_s" not in first["served"]
    node.up["up"] = False
    again = laptop.web.get(f"/api/v1/sites/{SITE}/channels").json()
    assert again["channels"] == first["channels"] and "cached_age_s" in again["served"]


def test_unbinding_clears_what_was_forwarded(laptop):
    laptop.web.get(f"/api/v1/sites/{SITE}/channels")
    assert bound_web._STATE.cache
    _unbind(laptop)
    laptop.web.get("/api/v1/sites")  # local now
    with pytest.raises(bound_web.ForwardRefused):
        bound_web.forward("channels", lambda lane: lane.channels(SITE), key=(SITE,))
    # The next bound read starts from nothing: the old binding's answers are gone.
    _bind(laptop.state)
    serving_mode.PROBES.clear()
    bound_web.forward("sites", lambda lane: lane.sites())
    assert [k for k in bound_web._STATE.cache if k[0] == "channels"] == []


# --------------------------------------------------------------------------- 3. test data


def test_test_data_is_marked_everywhere(laptop, monkeypatch):
    from axiom.extensions.builtins.chat import where

    monkeypatch.setenv("AXIOM_SAMPLE_DATA", "1")
    report = laptop.web.get("/api/v1/binding").json()
    assert report["mode"] == "test" and report["test_data"] is True
    assert "sample" in report["sources"]["data"]

    sites = laptop.web.get("/api/v1/sites").json()
    assert sites["test_data"] is True and sites["notes"][0] == bound_web.TEST_DATA_NOTE
    figure = laptop.web.get(f"/api/v1/sites/{SITE}/figure?channels=flow-a")
    assert bound_web.TEST_DATA_NOTE in figure.json()["detail"]

    footer = where._sources_text(where.sources_of().mode, "d", "docs", "m")
    assert footer[0].startswith("TEST MODE"), footer
    assert where.banner_line().startswith("LOCAL TEST MODE")


# --------------------------------------------------------------------------- 4. reads only


def test_a_write_is_never_forwarded(laptop):
    door = bound_web.BoundDoor(SITE, FACE)
    for verb in sorted(read_target.SUBMITS) + ["data.drop", "data.kit_retire"]:
        with pytest.raises(bound_web.ForwardRefused) as refused:
            door.query(verb, {})
        assert refused.value.status == 403
    assert not any("/data/" in url for url in laptop.calls), laptop.calls
    with pytest.raises(bound_web.ForwardRefused):
        bound_web.forward("kit_submit", lambda lane: None)


def test_the_forwarded_reads_are_reads_the_node_serves():
    served = set(read_api._VERBS) | set(read_api._KIT_VERBS)
    for read, verbs in bound_web.WEB_READS.items():
        assert set(verbs) <= served, (read, verbs)
        assert not set(verbs) & set(read_target.SUBMITS), read


def test_every_forwarded_web_route_is_a_get():
    forwarded = {"/api/v1/sites", "/api/v1/sites/{site}/channels", "/api/v1/sites/{site}/series",
                 "/api/v1/sites/{site}/dates", "/api/v1/sites/{site}/revision",
                 "/api/v1/sites/{site}/figure"}
    routes = {r.path: r.methods for r in build_api_router().routes if r.path in forwarded}
    assert set(routes) == forwarded
    assert all(methods == {"GET"} for methods in routes.values()), routes


# --------------------------------------------------------------------------- 5. the key


def test_the_key_never_reaches_the_browser_or_a_log(laptop, node, caplog):
    caplog.set_level(logging.DEBUG)
    paths = ["/api/v1/binding", "/api/v1/version", "/api/v1/sites",
             f"/api/v1/sites/{SITE}/channels", f"/api/v1/sites/{SITE}/revision",
             f"/api/v1/sites/{SITE}/dates?channels=flow-a",
             f"/api/v1/sites/{SITE}/figure?channels=flow-a&last=24h",
             f"/api/v1/sites/{SITE}/series?feed=plant&channels=flow-a&bucket_s=300"]
    seen = []
    for path in paths:
        response = laptop.web.get(path)
        assert response.status_code == 200, (path, response.text)
        seen.append(response.text + json.dumps(dict(response.headers)))
    node.up["up"] = False
    seen.append(laptop.web.get("/api/v1/sites/other/channels").text)
    everything = "\n".join(seen) + "\n".join(r.getMessage() for r in caplog.records)
    assert node.key not in everything
    assert TOKEN_REF not in everything  # not even where the key lives
    assert all(node.key not in url for url in laptop.calls)  # never in a URL


def test_view_as_selects_the_audiences_key_and_the_report_says_so(laptop, node, monkeypatch):
    (laptop.state / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": SITE,
        "bindings": {SITE: {"site": SITE, "face_url": FACE, "token_ref": TOKEN_REF,
                            "view_as": [{"role": "partner-viewer",
                                         "token_ref": "openbao://kv/data/alpha-viewer"}]}},
    }), encoding="utf-8")
    assert read_target._binding_token_ref() == TOKEN_REF
    monkeypatch.setenv("AXIOM_VIEW_AS", "partner-viewer")
    assert read_target._binding_token_ref() == "openbao://kv/data/alpha-viewer"
    report = laptop.web.get("/api/v1/binding").json()
    assert report["view_as"] == "partner-viewer" and report["view_as_options"] == ["partner-viewer"]
    assert "openbao://" not in json.dumps(report)


# --------------------------------------------------------------------------- serve --bound


def test_serve_bound_refuses_when_not_bound_and_beyond_loopback(laptop):
    from axiom.extensions.builtins.http.skills.serve import _bound_preflight

    lines = _bound_preflight({"bound": True}, "127.0.0.1")
    assert isinstance(lines, list) and SITE in lines[0] and "reachable" in lines[0]
    assert node_key_absent(lines)
    refused = _bound_preflight({"bound": True}, "0.0.0.0")
    assert isinstance(refused, SkillResult) and not refused.ok and "--expose" in refused.errors[0]
    warned = _bound_preflight({"bound": True, "expose": True}, "0.0.0.0")
    assert any(line.startswith("WARNING") for line in warned)
    _unbind(laptop)
    not_bound = _bound_preflight({"bound": True}, "127.0.0.1")
    assert isinstance(not_bound, SkillResult) and "needs a current site binding" in not_bound.errors[0]


def node_key_absent(lines) -> bool:
    return not any("axk_" in line or "Bearer" in line for line in lines)


def test_version_skew_disables_only_what_the_node_does_not_serve():
    node = serving_mode.NodeProbe(reachable=True, api="v1", version="9.9",
                                  data_reads=("data.aggregate",))
    notes, disabled = serving_mode.skew(node, needed=bound_web.WEB_READS)
    assert disabled == {"dates", "series", "figure"}
    assert all("data.series" in n for n in notes)
    old = serving_mode.NodeProbe(reachable=True, api="v1")  # an older node that did not say
    assert serving_mode.skew(old, needed=bound_web.WEB_READS) == ([], set())


# --------------------------------------------------------------------------- the key's sites


def test_a_bound_page_lists_the_keys_sites_and_another_site_is_refused_not_empty(
    laptop, node, monkeypatch,
):
    """The site list follows the binding's principal, and a site it does not hold is a 403.

    The node holds a site-bound key to its own site (``gold_query.scope_clause``): its catalogue
    lists that site only, and a read naming another site is refused by name. The real figure lane
    carries that refusal to the page as a refusal. An empty channel list for another site reads as
    "that site has no data", which is the answer this test forbids.
    """
    from axiom.extensions.builtins.scidisplay.node_figure_source import NodeFigureSource

    held = gold.aggregate

    def scoped(params, ctx):
        text = params.get("filter") or ""
        if "site = " in text and f"'{SITE}'" not in text:
            other = text.split("'")[1]
            return SkillResult(ok=False, errors=[
                f"this credential is bound to site {SITE!r}, which reads only {SITE!r}, "
                f"not {other!r}"])
        return held(params, ctx)

    monkeypatch.setattr(gold, "aggregate", scoped)
    bound_web.set_lane_factory(lambda door: NodeFigureSource(door=door, dates_cache_path=""))

    answer = laptop.web.get("/api/v1/sites")
    assert answer.status_code == 200, answer.text
    listed = answer.json()["sites"]
    assert [s["site"] for s in listed] == [SITE]

    for path in ("/api/v1/sites/beta-site/channels",
                 "/api/v1/sites/beta-site/figure?channels=flow-a",
                 "/api/v1/sites/beta-site/dates?channels=flow-a"):
        response = laptop.web.get(path)
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert "reads only" in response.json()["detail"], response.text

    own = laptop.web.get(f"/api/v1/sites/{SITE}/channels")
    assert own.status_code == 200 and own.json()["channels"], own.text


def test_a_front_door_that_refuses_the_version_read_falls_back_to_the_data_door(
    laptop, node, monkeypatch,
):
    """A site's nginx can gate ``/api/v1/version`` behind a browser session and strip the bearer.

    The data door is the contract a bound read uses. When it answers the binding's key the node
    is reachable, its version is unknown (which disables nothing), and the report says why.
    """
    monkeypatch.setattr(serving_mode, "_http_get", lambda url, headers, timeout: (401, {}))
    monkeypatch.setattr(gold, "tables", lambda params, ctx: SkillResult(ok=True, value={"data": {}}))
    serving_mode.PROBES.clear()
    body = laptop.web.get(f"/api/v1/sites/{SITE}/channels")
    assert body.status_code == 200, body.text
    report = laptop.web.get("/api/v1/binding").json()
    assert report["reachable"] is True
    assert any("refused /api/v1/version" in n for n in report["skew"]), report["skew"]

    # And when the data door refuses the key too, the node is unreachable, as before.
    node.up["up"] = False
    serving_mode.PROBES.clear()
    bound_web.reset()
    assert laptop.web.get(f"/api/v1/sites/{SITE}/channels").status_code == 503


def test_a_rate_limited_forwarded_read_is_waited_out_not_reported(monkeypatch):
    answers = [SkillResult(ok=False, errors=["the node answered HTTP 429."]),
               SkillResult(ok=True, value={"data": {"groups": []}})]
    monkeypatch.setattr(read_target, "ask", lambda target, verb, params: answers.pop(0))
    monkeypatch.setattr(bound_web.time, "sleep", lambda _s: None)
    assert bound_web.BoundDoor(SITE, FACE).query("data.aggregate", {}) == {"data": {"groups": []}}
