# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run a journey over HTTP. Never raises: every failure becomes a result.

This proves what the server SAYS (status, redirects, cache headers, content
types). It does not prove what a browser DOES with it; a browser driver, when
installed, adds that (spec-synthetic-journeys section 5). Cookie values and
Set-Cookie values are never recorded.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from . import cachepolicy
from .model import Evidence, Journey, JourneyResult, Step, StepResult, roll_up

_RECORDED_HEADERS = (
    "cache-control",
    "last-modified",
    "etag",
    "expires",
    "content-type",
    "location",
    "vary",
    "age",
)


class _AssetFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.urls.append(a["src"] or "")
        elif (
            tag == "link"
            and a.get("href")
            and (a.get("rel") or "").lower() in ("stylesheet", "modulepreload")
        ):
            self.urls.append(a["href"] or "")


def _evidence(resp: httpx.Response | None, started: float, note: str | None = None) -> Evidence:
    ev = Evidence(elapsed_ms=int((time.monotonic() - started) * 1000), note=note)
    if resp is not None:
        ev.status = resp.status_code
        ev.headers = {k: resp.headers[k] for k in _RECORDED_HEADERS if k in resp.headers}
    return ev


def _session_cookie(journey: Journey, env: Mapping[str, str]) -> tuple[str, str] | None:
    raw = env.get(journey.session_cookie_env or "") if journey.session_cookie_env else None
    if not raw or "=" not in raw:
        return None
    name, _, value = raw.partition("=")
    return name.strip(), value.strip()


def _cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}


def _check_assets(
    client: httpx.Client,
    journey: Journey,
    step: Step,
    resp: httpx.Response,
    cookies: dict[str, str],
) -> str | None:
    """Return a failure message, or None. Fetches at most five same-origin assets."""
    finder = _AssetFinder()
    finder.feed(resp.text)
    base = journey.base_url.rstrip("/") + "/"
    origin = urlparse(base).netloc
    seen: list[str] = []
    for u in finder.urls:
        full = urljoin(base, u)
        if urlparse(full).netloc == origin and full not in seen:
            seen.append(full)
        if len(seen) == 5:
            break
    for full in seen:
        r = client.get(full, headers=_cookie_header(cookies), follow_redirects=False)
        ctype = r.headers.get("content-type", "")
        if ctype.lower().startswith("text/html") or 300 <= r.status_code < 400:
            return (
                f"asset {urlparse(full).path} came back as {r.status_code} {ctype or 'no content-type'}"
                " instead of the asset itself"
            )
    return None


def _evaluate(
    step: Step,
    resp: httpx.Response,
    ev: Evidence,
    client: httpx.Client,
    journey: Journey,
    cookies: dict[str, str],
) -> list[str]:
    x = step.expect
    problems: list[str] = []
    if x.status is not None and resp.status_code != x.status:
        problems.append(f"status {resp.status_code}, expected {x.status}")
    if x.status_in and resp.status_code not in x.status_in:
        problems.append(f"status {resp.status_code}, expected one of {list(x.status_in)}")
    if x.content_type and not resp.headers.get("content-type", "").lower().startswith(
        x.content_type.lower()
    ):
        problems.append(
            f"content-type {resp.headers.get('content-type')!r}, expected {x.content_type!r}"
        )
    for h in x.header_present:
        if h not in resp.headers:
            problems.append(f"header {h} is absent")
    for h in x.header_absent:
        if h in resp.headers:
            problems.append(f"header {h} is present")
    for h, rx in x.header_matches.items():
        if not re.search(rx, resp.headers.get(h, "")):
            problems.append(f"header {h} does not match {rx!r}")
    if x.location_matches and not re.search(x.location_matches, resp.headers.get("location", "")):
        problems.append(
            f"Location {resp.headers.get('location')!r} does not match {x.location_matches!r}"
        )
    if x.cache_policy:
        ok, why = cachepolicy.satisfies(x.cache_policy, resp.status_code, dict(resp.headers))
        if not ok:
            problems.append(f"cache policy {x.cache_policy}: {why}")
    for s in x.body_contains:
        if s not in resp.text:
            problems.append(f"body lacks {s!r}")
    for s in x.body_not_contains:
        if s in resp.text:
            problems.append(f"body contains {s!r}")
    if x.max_latency_ms is not None and (ev.elapsed_ms or 0) > x.max_latency_ms:
        problems.append(f"{ev.elapsed_ms} ms, limit {x.max_latency_ms} ms")
    if x.assets_not_html and resp.status_code == 200:
        msg = _check_assets(client, journey, step, resp, cookies)
        if msg:
            problems.append(msg)
    return problems


def run_step(
    client: httpx.Client, journey: Journey, step: Step, env: Mapping[str, str]
) -> StepResult:
    cookies: dict[str, str] = {}
    if step.auth == "session":
        sc = _session_cookie(journey, env)
        if sc is None:
            var = journey.session_cookie_env or "(no session_cookie_env declared)"
            return StepResult(
                step.name,
                "unproven",
                f"needs a session cookie in {var}; not set, so this was not checked",
            )
        cookies[sc[0]] = sc[1]
    url = journey.base_url.rstrip("/") + step.path
    started = time.monotonic()
    try:
        resp = client.request(
            step.method,
            url,
            headers={**step.headers, **_cookie_header(cookies)},
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        return StepResult(
            step.name,
            "fail",
            f"request failed: {type(exc).__name__}: {exc}",
            _evidence(None, started, note=type(exc).__name__),
        )
    ev = _evidence(resp, started)
    try:
        problems = _evaluate(step, resp, ev, client, journey, cookies)
    except httpx.HTTPError as exc:
        problems = [f"follow-up request failed: {type(exc).__name__}: {exc}"]
    if problems:
        return StepResult(step.name, "fail", "; ".join(problems), ev)
    return StepResult(step.name, "pass", "as expected", ev)


def run_journey(journey: Journey, *, env: Mapping[str, str] | None = None) -> JourneyResult:
    env = os.environ if env is None else env
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    results: list[StepResult] = []
    try:
        with httpx.Client(timeout=journey.timeout_s, verify=journey.verify_tls) as client:
            for step in journey.steps:
                results.append(run_step(client, journey, step, env))
    except (
        Exception
    ) as exc:  # the tolerance contract: a checker that raises hides the outage it is for
        results.append(StepResult("journey", "fail", f"runner error: {type(exc).__name__}: {exc}"))
    return JourneyResult(
        journey=journey.name,
        base_url=journey.base_url,
        status=roll_up([r.status for r in results]),
        steps=results,
        started_at=started_at,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
