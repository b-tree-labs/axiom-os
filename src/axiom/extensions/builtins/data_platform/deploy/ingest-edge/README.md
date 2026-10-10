# Ingest edge: install on one VM

An ingest edge is the public landing node of ADR-177. Producers push to it
over HTTPS; the node that owns the data platform pulls from it. It serves
`/ingest` (the two push lanes), `/edge` (the export, for the downstream only)
and `/healthz`, and nothing else. It holds no web application, no chat, no
retrieval and no database.

## What the host needs

- Linux with systemd, Python 3.11 or newer, and Caddy for TLS.
- A public DNS name, inbound TCP 443 only, normal outbound access.
- Persistent disk for `/var/lib/axiom-edge`, sized for the backlog the edge
  may hold while the downstream is unreachable, plus the retention window.

## Install

1. Create the service user and directories:

   ```bash
   sudo useradd --system --home /var/lib/axiom-edge axiom-edge
   sudo install -d -o axiom-edge -g axiom-edge /var/lib/axiom-edge/{state,outbox,bronze}
   sudo install -d /etc/axiom-edge /opt/axiom-edge
   ```

2. Install a tagged release:

   ```bash
   sudo python3 -m venv /opt/axiom-edge/venv
   sudo /opt/axiom-edge/venv/bin/pip install "axiom-os-lm==<release>"
   ```

3. Copy `edge.env.example` to `/etc/axiom-edge/edge.env` and set
   `AXIOM_EDGE_DOWNSTREAM` to the principal of the node that will pull.

4. Administrative commands record who ran them, so set `AXIOM_ACTOR` to your
   own principal (for example `@you:org`) in the shell you run them from.
   Register one push connector per producing site, as the service user:

   ```bash
   export AXI_STATE_DIR=/var/lib/axiom-edge/state
   export AXIOM_GATE_API_KEYS_FILE=/var/lib/axiom-edge/state/gate-api-keys.json
   sudo -u axiom-edge --preserve-env=AXI_STATE_DIR,AXIOM_GATE_API_KEYS_FILE,AXIOM_ACTOR \
     /opt/axiom-edge/venv/bin/axi data register \
       --bronze-root /var/lib/axiom-edge/bronze/<site>-src --site <site> \
       --default-disposition allow --default-tier restricted \
       <site>-src push
   ```

5. Issue keys, in the same shell. Each producer gets a key bound to its own site that can
   push and read its own site's summary (`GET /ingest/summary`, which is how a
   collector confirms a run landed before it would send it again); the
   downstream gets the one key that can pull. Each plaintext is
   shown once: hand it over directly and store it in the receiver's vault.

   ```bash
   axi gate issue api-key --principal @<site>-daq:<site> --site <site> --scope data_platform:invoke --scope data_platform:read
   axi gate issue api-key --principal <AXIOM_EDGE_DOWNSTREAM value> --scope edge_export:read
   ```

6. Install the unit and the proxy, then start both:

   ```bash
   sudo cp axiom-ingest-edge.service /etc/systemd/system/
   sudo cp Caddyfile /etc/caddy/Caddyfile   # set EDGE_HOSTNAME first
   sudo systemctl daemon-reload
   sudo systemctl enable --now axiom-ingest-edge caddy
   curl -fsS https://<EDGE_HOSTNAME>/healthz
   ```

## On the downstream node

Register the edge and pull it on a timer:

```bash
axi secrets set <edge>-pull-key            # the downstream key, from stdin
axi data register --bronze-root ~/.axi/bronze/<edge> \
  --credential-ref <vault reference to <edge>-pull-key> \
  <edge> edge --edge-url https://<EDGE_HOSTNAME>
axi data edge-pull <edge>
```

Each pulled batch lands in the local connector with the same name as its
source on the edge (`--source-map edge-name=local-name` renames). Rerunning is
safe: the cursor advances only past durable batches and rows deduplicate.

## Rotation and revocation

`axi gate revoke api-key <key_id>` takes effect on the next request. Issue the
replacement first, hand it over, then revoke the old key.

## As a container behind an existing nginx site

When the host already runs Docker, use `docker/`: one compose service bound
to the host's loopback, one nginx block for `/ingest/` and `/edge/`, and a
folder the operator owns on the host (`/srv/ingest-edge/config`) holding who
may push: `sources.txt` and the hashed keys file, both re-read live, so adding
or removing a partner needs no restart and no help from the deployer. Its
README is written for the person who deploys it, with a troubleshooting
table.

## Behind an existing nginx site, without root

When the edge shares a host that already serves a site, nginx stays the only
thing on 443 and the edge is one more set of locations in that site's server
block. On such a host the operator often has no root: the edge then runs as a
per-user service and keeps its data under a directory the operator owns. An
administrator is needed twice, once each: to add one `include` line to the
site's server block, and to enable linger for the operator's account.

The edge adds one location, for `/ingest` and `/edge` paths only, so the
site's own paths, including any `/healthz`, are untouched. Its health is
checked on the host (`curl http://127.0.0.1:<port>/healthz`); from outside,
an anonymous `POST /ingest/rows` answered 401 or 403 shows the route reaches
the edge.

### 1. Preflight (read-only)

```bash
scp preflight.sh <operator>@<host>: && ssh <operator>@<host> sh preflight.sh <public-host-name>
```

It reports the OS, Python, free disk (including `$HOME`), the nginx server
block that serves the name, listening ports, whether the user systemd manager
and linger are available, and outbound access, and ends with GO or NO-GO. It
changes nothing. Pick a port for the edge that nothing listens on (the
preflight lists them) and that the site does not already proxy to.

### 2. Install as the operator (no root)

```bash
mkdir -p ~/axiom-edge ~/.config/axiom-edge ~/.config/systemd/user
python3 -m venv ~/axiom-edge/venv
~/axiom-edge/venv/bin/pip install "axiom-os-lm==<release>"
install -m 0755 check-free-space.sh run-edge-without-linger.sh ~/axiom-edge/
install -m 0600 edge.user.env.example ~/.config/axiom-edge/edge.env
$EDITOR ~/.config/axiom-edge/edge.env   # replace OPERATOR; set the port, data dir, downstream
mkdir -p "$(sed -n 's/^AXIOM_EDGE_DATA_DIR=//p' ~/.config/axiom-edge/edge.env)"/{state,outbox,bronze}
```

Register one push connector per producing site and issue the keys as in
steps 4 and 5 above, with `AXI_STATE_DIR` and `AXIOM_GATE_API_KEYS_FILE`
exported from `edge.env` and no `sudo`. Name each connector the same as the
downstream's existing connector for that site, so pulled batches continue the
series the downstream already holds.

Start it:

```bash
install -m 0644 axiom-ingest-edge.user.service ~/.config/systemd/user/axiom-ingest-edge.service
systemctl --user daemon-reload
systemctl --user enable --now axiom-ingest-edge
curl -fsS http://127.0.0.1:<port>/healthz      # {"status":"ok"}
```

With linger enabled (`loginctl enable-linger <operator>`, run once by an
administrator) the edge keeps running after logout and starts at boot.
**Without linger**, use `~/axiom-edge/run-edge-without-linger.sh start`
instead: it keeps running after logout, but it does not survive a reboot and
nothing restarts it if it exits. Check `run-edge-without-linger.sh status`
after any host maintenance until linger is enabled.

### 3. Add the edge to the site's nginx (administrator, once)

Edit `nginx-axiom-edge.conf` so `proxy_pass` names the chosen port. The
administrator pastes its one `location` block, or an `include` of the file,
inside the site's `server { listen 443 ssl; ... }` block, **before its
`location /`**, and applies it:

```bash
sudo cp <site-file> <site-file>.pre-edge     # rollback copy
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` must pass before the reload; a failed test leaves the running site
untouched. Check the site still answers as before.

**Rollback:** restore the `.pre-edge` copy, `sudo nginx -t && sudo systemctl
reload nginx`, then `systemctl --user disable --now axiom-ingest-edge` (or
`run-edge-without-linger.sh stop`). The site is back exactly as it was; the
edge's data stays in its data directory until removed.

### 4. Prove it from off campus

Run the **Ingest edge smoke** workflow by hand with `edge_url` set to the
edge's public `https://` address. Its `deployed` job runs
`scripts/ingest_edge_probe.py` from a hosted runner: the edge's own refusal
of an anonymous push, anonymous refusals (including a request claiming a
loopback address), the site's own page still answering, and, with the probe
keys in the repository secrets `EDGE_PROBE_PRODUCER_KEY` and
`EDGE_PROBE_PULL_KEY`, a push, a replay that lands nothing, and the batch in
the export. Issue the probe keys for a probe-only site and connector so they
can be revoked on their own. Producers are pointed at the edge only after this
passes.

### 5. First pull on the downstream

As in "On the downstream node" above, with `--edge-url https://<public-host-name>`.
Pulled batches land in the local connector of the same name as their source
on the edge, so the downstream's conform takes them in like any other deposit.
Put the pull on a timer after the first run succeeds.
