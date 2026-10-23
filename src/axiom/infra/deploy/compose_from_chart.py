# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Derive a single host's Compose file from the node chart's rendered manifests.

The chart (``infra/charts/axiom-node``) is the one definition of a node's roles
(ADR-170). A host without Kubernetes runs Compose, and that Compose is generated
here from ``helm template`` output, so there is never a second hand-maintained
copy to drift. Only the subset the chart uses is understood; anything else is
refused rather than dropped, because a silently missing workload is worse than
a failed render.

Mapping:
- a StatefulSet/Deployment becomes one service, named like its Kubernetes
  Service (``<release>-<role>``) so configuration that names a peer works on
  both targets;
- the ``data`` claim becomes a named volume; a ConfigMap mount becomes a bind
  of a folder the operator owns (``$<ROLE>_CONFIG_DIR``, default
  ``./config/<role>``); a Secret becomes an env file the operator writes from
  the vault (``$<ROLE>_SECRETS_ENV``), never a value in the file;
- ``workingDir`` becomes ``working_dir``;
- probes become a healthcheck, the memory limit ``mem_limit``, the CPU request
  ``cpus``; ``hostNetwork`` becomes ``network_mode: host``;
- a CronJob with an ``*/N`` minute schedule becomes a loop at the same cadence;
- the role's priority (``axiom.io/priority``) becomes CPU shares and the
  kernel's out-of-memory preference, so on small hardware the realtime path
  keeps running while storage and forwarding are throttled first.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

import yaml

HEADER = (
    "# GENERATED from infra/charts/axiom-node by axiom.infra.deploy.compose_from_chart.\n"
    "# Do not edit: change the chart or its values and generate again.\n"
)
# Compose names services itself, and a role's config is a bind; the node's
# identity ConfigMap and a blue/green front Service are read below. A preview
# Service is a Kubernetes convenience with nothing to run on one host.
_IGNORED = {"Service", "PriorityClass"}
# Priority on a single host: CPU share under contention and which process the
# kernel kills first when memory runs out. The realtime path wins both.
_PRIORITY = {
    "realtime": {"cpu_shares": 2048, "oom_score_adj": -500},
    "background": {"cpu_shares": 256, "oom_score_adj": 500},
}


def _role(doc: dict) -> str:
    md = doc.get("metadata") or {}
    return (md.get("annotations") or {}).get("axiom.io/role") or md.get("name", "role")


def _env_name(role: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", role.upper())


def _cpus(v: str | None) -> float | None:
    if not v:
        return None
    s = str(v)
    return round(int(s[:-1]) / 1000, 3) if s.endswith("m") else float(s)


def _memory(v: str) -> str:
    """Kubernetes binary units (Mi, Gi) to Compose's (m, g); both mean 2^20, 2^30."""
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi)?", str(v))
    if not m:
        raise ValueError(f"memory {v!r} is not in Ki/Mi/Gi")
    return m.group(1) + {"Ki": "k", "Mi": "m", "Gi": "g", None: ""}[m.group(2)]


def _literal(v: Any) -> Any:
    """A value Kubernetes passes through verbatim: Compose would interpolate its
    `$`, so a shell loop's `$d` would silently become empty. `$$` is a literal `$`."""
    return v.replace("$", "$$") if isinstance(v, str) else v


def _healthcheck(c: dict) -> dict | None:
    probe = c.get("readinessProbe") or c.get("livenessProbe")
    if not probe:
        return None
    if "httpGet" in probe:
        g = probe["httpGet"]
        url = f"http://127.0.0.1:{g['port']}{g.get('path', '/')}"
        test = [
            "CMD",
            "python",
            "-c",
            f"import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('{url}',timeout=4).status<400 else 1)",
        ]
    elif "exec" in probe:
        test = ["CMD", *map(_literal, probe["exec"]["command"])]
    else:
        raise ValueError(f"unsupported probe {list(probe)}")
    hc: dict[str, Any] = {
        "test": test,
        "interval": f"{probe.get('periodSeconds', 10)}s",
        "timeout": "5s",
        "retries": 5,
    }
    sp = c.get("startupProbe")
    if sp:
        hc["start_period"] = f"{sp.get('periodSeconds', 10) * sp.get('failureThreshold', 3)}s"
    return hc


def _workload(doc: dict, release: str) -> tuple[str, dict, list[str]]:
    """The role's main container; companions and init steps are handled by the caller."""
    return _container(
        doc, doc["spec"]["template"]["spec"]["containers"][0], doc["metadata"]["name"], main=True
    )


def _container(doc: dict, c: dict, name: str, main: bool) -> tuple[str, dict, list[str]]:
    role = _role(doc)
    spec = doc["spec"]["template"]["spec"]
    svc: dict[str, Any] = {"image": c["image"], "restart": "unless-stopped"}
    if c.get("args"):
        svc["command"] = [_literal(a) for a in c["args"]]
    if c.get("workingDir"):
        svc["working_dir"] = c["workingDir"]
    env: dict[str, str] = {}
    for e in c.get("env") or []:
        if "value" in e:
            env[e["name"]] = _literal(e.get("value", ""))
        elif "secretKeyRef" in (e.get("valueFrom") or {}):
            # One key of a Secret: on a single host, an environment variable of
            # that name that the operator sets from the vault when starting.
            # Compose substitutes it at `up`, so it is never written to disk.
            key = e["valueFrom"]["secretKeyRef"]["key"]
            env[e["name"]] = f"${{{key}:?{key} must be set from the vault}}"
        else:
            raise ValueError(
                f"{name}: env {e['name']} comes from a source this generator does not understand"
            )
    if env:
        svc["environment"] = env
    for ef in c.get("envFrom") or []:
        if "secretRef" in ef:
            svc.setdefault("env_file", []).append(
                f"${{{_env_name(role)}_SECRETS_ENV:-./secrets/{role}.env}}"
            )
        else:
            raise ValueError(f"{name}: envFrom {list(ef)} is not supported")
    sec = spec.get("securityContext") or {}
    if "runAsUser" in sec:
        svc["user"] = str(sec["runAsUser"])
    if spec.get("hostNetwork"):
        svc["network_mode"] = "host"
    ann = doc["metadata"].get("annotations") or {}
    svc.update(_PRIORITY.get(ann.get("axiom.io/priority", "background"), {}))
    pub = ann.get("axiom.io/publish")
    if main and pub and not spec.get("hostNetwork"):
        svc["ports"] = [pub]
    if main and ann.get("axiom.io/listen"):
        svc["x-axiom-listen"] = json.loads(ann["axiom.io/listen"])
    hc = _healthcheck(c)
    if hc:
        svc["healthcheck"] = hc
    res = c.get("resources") or {}
    if (res.get("limits") or {}).get("memory"):
        svc["mem_limit"] = _memory(res["limits"]["memory"])
    cpus = _cpus((res.get("requests") or {}).get("cpu"))
    if cpus:
        svc["cpus"] = cpus
    if main and ann.get("axiom.io/container-name"):
        svc["container_name"] = ann["axiom.io/container-name"]
    claims = {vct["metadata"]["name"] for vct in doc["spec"].get("volumeClaimTemplates") or []}
    vols = {v["name"]: v for v in spec.get("volumes") or []}
    mounts, named = [], []
    for m in c.get("volumeMounts") or []:
        ro = ":ro" if m.get("readOnly") else ""
        if m["name"] in claims:
            vname = ann.get("axiom.io/volume-name") or f"{doc['metadata']['name']}-{m['name']}"
            named.append(vname)
            mounts.append(f"{vname}:{m['mountPath']}{ro}")
        elif "configMap" in vols.get(m["name"], {}) and m["name"] == "config":
            mounts.append(
                f"${{{_env_name(role)}_CONFIG_DIR:-./config/{role}}}:{m['mountPath']}{ro}"
            )
        elif "configMap" in vols.get(m["name"], {}):
            # An extraConfig entry: its own host directory, overridable per entry.
            var = f"{_env_name(role)}_{_env_name(m['name'])}_DIR"
            mounts.append(f"${{{var}:-./config/{role}-{m['name']}}}:{m['mountPath']}{ro}")
        elif "persistentVolumeClaim" in vols.get(m["name"], {}):
            vname = vols[m["name"]]["persistentVolumeClaim"]["claimName"]
            # Another role's StatefulSet volume (data-<set>-0 on Kubernetes) is
            # that role's named volume on Compose.
            vname = _CLAIMS.get(vname, vname)
            named.append(vname)
            mounts.append(f"{vname}:{m['mountPath']}{ro}")
        elif (
            m["mountPath"] == "/dev/shm"
            and (vols.get(m["name"], {}).get("emptyDir") or {}).get("medium") == "Memory"
        ):
            svc["shm_size"] = _memory(vols[m["name"]]["emptyDir"]["sizeLimit"])
        elif "emptyDir" in vols.get(m["name"], {}):
            vname = f"{doc['metadata']['name']}-{m['name']}"
            named.append(vname)
            mounts.append(f"{vname}:{m['mountPath']}{ro}")
        else:
            raise ValueError(
                f"{name}: volume {m['name']} has a source this generator does not understand"
            )
    if mounts:
        svc["volumes"] = mounts
    return name, svc, named


def _front(doc: dict, release: str) -> tuple[str, dict, list[str]]:
    """A blue/green role's stable front: nginx on the role's port, routing to the
    slots named in /front/upstream.conf. A switch rewrites that file and reloads
    nginx gracefully (``axiom.infra.deploy.bluegreen``); in-flight requests
    finish on the old slot."""
    from axiom.infra.deploy.bluegreen import upstream_lines

    name = doc["metadata"]["name"]
    ann = doc["metadata"]["annotations"]
    role, port, active = ann["axiom.io/role"], int(ann["axiom.io/port"]), ann["axiom.io/active"]
    slots = [s for s in ann["axiom.io/slots"].split(",") if s]
    initial = "\\n".join(upstream_lines(release, role, port, active, slots=slots))
    conf = (
        # Docker's own DNS, asked again every 5 s: a slot that restarts comes
        # back on a new address, and `resolve` (bluegreen.upstream_lines)
        # follows it. `zone` is what lets nginx keep re-resolved servers.
        "resolver 127.0.0.11 valid=5s ipv6=off; "
        "upstream active { zone active 64k; include /front/upstream.conf; } "
        "server { listen "
        + str(port)
        + "; client_max_body_size 64m; location / { proxy_pass http://active; "
        "proxy_http_version 1.1; proxy_set_header Host $$host; "
        "proxy_set_header X-Real-IP $$remote_addr; "
        "proxy_set_header X-Forwarded-For $$proxy_add_x_forwarded_for; "
        "proxy_set_header X-Forwarded-Proto $$scheme; proxy_next_upstream error timeout; "
        "proxy_read_timeout 300s; } }"
    )
    script = (
        f"mkdir -p /front && [ -s /front/upstream.conf ] || printf '{initial}\\n' > /front/upstream.conf; "
        # A front volume written before `resolve` existed keeps working and
        # gains it: a server line without it is given it in place.
        "sed -i -E 's/^(server [^ ;]+);$$/\\1 resolve;/' /front/upstream.conf; "
        f"printf '%s\\n' '{conf}' > /etc/nginx/conf.d/default.conf; exec nginx -g 'daemon off;'"
    )
    svc: dict[str, Any] = {
        "image": ann["axiom.io/front-image"],
        "restart": "unless-stopped",
        "command": ["sh", "-c", script],
        "volumes": [f"{name}-front:/front"],
        "depends_on": {
            f"{name}-{s}": {"condition": "service_started"}
            for s in slots
            if s == active or active == "both"
        },
        "healthcheck": {
            "test": ["CMD", "wget", "-q", "-O", "/dev/null", f"http://127.0.0.1:{port}/healthz"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 5,
        },
        "x-axiom-front": {
            "role": role,
            "port": port,
            "active": active,
            "slots": slots,
            "release": release,
        },
    }
    svc.update(_PRIORITY["background"])
    if ann.get("axiom.io/publish"):
        svc["ports"] = [ann["axiom.io/publish"]]
    return name, svc, [f"{name}-front"]


def _cronjob(doc: dict, release: str) -> tuple[str, dict, list[str]]:
    sched = doc["spec"]["schedule"]
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", sched.strip())
    if not m:
        raise ValueError(f"{doc['metadata']['name']}: schedule {sched!r} is not '*/N * * * *'")
    tmpl = {
        "metadata": doc["metadata"],
        "spec": {"template": doc["spec"]["jobTemplate"]["spec"]["template"]},
    }
    name, svc, named = _workload(tmpl, release)
    args = " ".join(svc.pop("command", []))
    svc["command"] = [
        "sh",
        "-c",
        f"while true; do {args} || echo 'run failed' >&2; sleep {int(m.group(1)) * 60}; done",
    ]
    return name, svc, named


_CLAIMS: dict[str, str] = {}


def _claim_names(docs: list[dict]) -> dict[str, str]:
    out = {}
    for d in docs:
        if d.get("kind") != "StatefulSet":
            continue
        for vct in d["spec"].get("volumeClaimTemplates") or []:
            ann = d["metadata"].get("annotations") or {}
            name = d["metadata"]["name"]
            out[f"{vct['metadata']['name']}-{name}-0"] = (
                ann.get("axiom.io/volume-name") or f"{name}-{vct['metadata']['name']}"
            )
    return out


def compose_from_manifests(docs: list[dict], release: str = "node") -> dict:
    _CLAIMS.clear()
    _CLAIMS.update(_claim_names(docs))
    services: dict[str, dict] = {}
    volumes: dict[str, dict] = {}
    egress: list[dict] = []
    node: dict | None = None
    for doc in docs:
        kind = doc.get("kind")
        if (
            kind == "Service"
            and ((doc.get("metadata") or {}).get("annotations") or {}).get("axiom.io/front")
            == "true"
        ):
            fname, fsvc, fvols = _front(doc, release)
            services[fname] = fsvc
            volumes.update({v: {} for v in fvols})
            continue
        if kind == "PersistentVolumeClaim":
            volumes[doc["metadata"]["name"]] = {}
            continue
        if kind in _IGNORED:
            continue
        if kind == "ConfigMap":
            if ((doc.get("metadata") or {}).get("labels") or {}).get(
                "axiom.io/node-identity"
            ) == "true":
                node = json.loads(doc["data"]["node.json"])
            continue
        for e in json.loads(
            ((doc.get("metadata") or {}).get("annotations") or {}).get("axiom.io/egress", "[]")
        ):
            if e not in egress:
                egress.append(e)
        if kind in ("StatefulSet", "Deployment"):
            spec = doc["spec"]["template"]["spec"]
            name, svc, named = _workload(doc, release)
            # Kubernetes runs init steps before any container in the pod; keep
            # that order on Compose for the role and its companions alike.
            inits: dict[str, dict] = {}
            for init in spec.get("initContainers") or []:
                iname, isvc, inamed = _container(doc, init, f"{name}-{init['name']}", main=False)
                isvc["restart"] = "no"
                isvc.pop("healthcheck", None)
                services[iname] = isvc
                volumes.update({v: {} for v in inamed})
                inits[iname] = {"condition": "service_completed_successfully"}
            if inits:
                svc["depends_on"] = dict(inits)
            for comp in spec["containers"][1:]:
                cname, csvc, cnamed = _container(doc, comp, f"{name}-{comp['name']}", main=False)
                csvc.pop("healthcheck", None)
                if inits:
                    csvc["depends_on"] = dict(inits)
                services[cname] = csvc
                volumes.update({v: {} for v in cnamed})
        elif kind == "CronJob":
            name, svc, named = _cronjob(doc, release)
        else:
            raise ValueError(f"{kind} is not something a single host can run from this chart")
        name = ((doc.get("metadata") or {}).get("annotations") or {}).get(
            "axiom.io/compose-service"
        ) or name
        services[name] = svc
        for v in named:
            volumes[v] = {}
    out: dict[str, Any] = {}
    if node is not None:
        out["x-axiom-node"] = node
    if egress:
        out["x-axiom-egress"] = egress
    out["services"] = services
    if volumes:
        out["volumes"] = volumes
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--chart", required=True)
    ap.add_argument("--release", default="node")
    ap.add_argument("-f", "--values", action="append", default=[])
    ap.add_argument("--set", action="append", default=[], dest="sets")
    ap.add_argument("-o", "--out", default="-")
    a = ap.parse_args(argv)
    cmd = ["helm", "template", a.release, a.chart]
    for v in a.values:
        cmd += ["-f", v]
    for s in a.sets:
        cmd += ["--set", s]
    rendered = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    compose = compose_from_manifests([d for d in yaml.safe_load_all(rendered) if d], a.release)
    text = HEADER + yaml.safe_dump(compose, sort_keys=False)
    if a.out == "-":
        sys.stdout.write(text)
    else:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
