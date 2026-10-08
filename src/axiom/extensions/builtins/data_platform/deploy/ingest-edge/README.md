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

5. Issue keys, in the same shell. Each producer gets a key bound to its own site that can only
   push; the downstream gets the one key that can pull. Each plaintext is
   shown once: hand it over directly and store it in the receiver's vault.

   ```bash
   axi gate issue api-key --principal @<site>-daq:<site> --site <site> --scope data_platform:invoke
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
