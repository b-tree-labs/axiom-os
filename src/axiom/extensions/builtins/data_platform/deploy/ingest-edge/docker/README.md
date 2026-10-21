# Ingest edge behind an existing nginx site: for the deployer

**What it is.** A small container that receives sensor data from partner sites
over HTTPS and holds it until the platform pulls it. It serves `/ingest/`
(partners push), `/edge/` (the platform pulls, with its own key) and `/healthz`
(on the loopback only). It has no web app, no database and no login page.

**What it changes on the VM.**
- One container on `127.0.0.1:8787`.
- Two `location` blocks in your nginx, for `/ingest/` and `/edge/`. They don't
  touch `/`, or any other path the site serves.
- One small file in `/etc/nginx/conf.d/` (limits and a log format; it changes
  nothing until the two blocks use it).
- One folder, `/srv/ingest-edge/config`, owned by the edge operator. It holds who
  may push: a list of sources and a file of key **hashes**, never a usable key.
  The operator adds or removes partners there himself, with no restart, so this
  is a one-time deploy.

## Deploy (about 10 minutes, once)

```bash
# 1. The folder the edge operator manages (the only sudo step besides nginx)
sudo install -d -o <operator> -m 755 /srv/ingest-edge/config

# 2. The container. Create .env first: every compose command reads it.
cd ~/ingest-edge                       # this folder, copied to the VM
cp dot-env.example .env                # sets EDGE_CONFIG_DIR=/srv/ingest-edge/config
cp edge.env.example edge.env           # no secrets in it
docker compose build
docker compose up -d
curl -s http://127.0.0.1:8787/healthz  # -> {"status":"ok", ...} (more fields follow; "ok" is what counts)

# 3. nginx
#    a. The limits file (http level). It only declares things.
sudo cp nginx-ingest-edge-limits.conf /etc/nginx/conf.d/ingest-edge-limits.conf
#    b. Back the site file up, then paste nginx-ingest-edge.conf into the HTTPS
#       server block for <your-host> in /etc/nginx/sites-available/<your-site>,
#       BEFORE the line "    location / {".
sudo cp /etc/nginx/sites-available/<your-site> ~/<your-site>.bak
#    c. Recommended, one more paste: nginx-ingest-edge-port80.conf goes inside the
#       site's port-80 server block (the one that redirects to https), above its
#       redirect. It stops a mistyped http:// address from carrying a key.
sudo nginx -t && sudo systemctl reload nginx
```

## Check it worked (from anywhere)

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-host>/ingest/rows   # 403 (no key); 501 until the operator has added a key
curl -s -o /dev/null -w "%{http_code}\n" https://<your-host>/                     # 200: the site is untouched
curl -s -o /dev/null -w "%{http_code}\n" https://<your-host>/healthz              # NOT the edge: whatever the site answers for it (the edge's /healthz is loopback only)
openssl s_client -connect <your-host>:443 -tls1_1 -cipher 'DEFAULT:@SECLEVEL=0' </dev/null 2>&1 | grep -c "protocol version"   # 1: TLS 1.1 refused, as it should be. 0 means the site still accepts it: tell the edge operator (partners use 1.2 or 1.3)
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://<your-host>/ingest/rows   # 404 with the port-80 block (3c), 301 without it
```

That's all. Tell the edge operator, and he adds the partners.

## For the edge operator

You need the config folder (`/srv/ingest-edge/config`) and `edge-admin.sh` from
this folder. It uses docker when you can, otherwise an `axi` of the **same
version** as `compose.yaml` (`pip install --user "axiom-os-lm==<that version>"`).

```bash
export EDGE_CONFIG_DIR=/srv/ingest-edge/config
./edge-admin.sh add-site <site>        # registers <site>-src for <site>, issues its key
./edge-admin.sh add-puller             # the key the platform node pulls with
./edge-admin.sh new-key <site>         # a second key for a site (rotation)
./edge-admin.sh keys                   # who has a key
./edge-admin.sh revoke <key_id>        # refused from the next request
./edge-admin.sh status
```

A key is written to `issued-keys/<name>.key` (mode 600) and never printed: give
the file to its owner directly (their vault, not email or chat), then delete it.
`add-site` waits until the edge has registered the source (up to a minute) before it
says the key can go out: a partner that pushes sooner is refused, and their sender
sets those batches aside.

- **The puller's key must be issued to the principal in `edge.env`
  (`AXIOM_EDGE_DOWNSTREAM`).** `add-puller` does that. Another principal holding
  the same scope is refused.
- **Rotating a partner's key without a gap:** `./edge-admin.sh new-key <site>`
  issues a second key for the site (both work at once). Get it to the partner,
  wait until they say rows are landing, then `revoke` the old id.
- **Revoking** takes effect on the next request. Other partners' keys keep
  working.
- A partner's key can push to its own site's source only. Pushing to another
  site's source is refused (403) with a message naming the fix.

## Roll back

```bash
sudo cp ~/<your-site>.bak /etc/nginx/sites-available/<your-site> && sudo rm /etc/nginx/conf.d/ingest-edge-limits.conf && sudo nginx -t && sudo systemctl reload nginx
docker compose down        # add -v only to also delete data not yet pulled
```

## If something goes wrong

| What you see | Why | Fix |
|---|---|---|
| `required variable EDGE_CONFIG_DIR is missing` on any `docker compose` command | `.env` isn't there yet; compose reads it for every command, including `down` | `cp dot-env.example .env`, then rerun |
| `nginx -t` fails after pasting the block | The block landed outside the HTTPS `server { }`, or a brace is missing | Restore the backup, paste again just above `    location / {` in the **first** HTTPS server block (the one whose `server_name` is your public host name) |
| `nginx -t` says `zero size shared memory zone "ingest_edge_req"` or `unknown log format "ingest_edge"` | The limits file isn't in `/etc/nginx/conf.d/` (or nginx.conf doesn't include `conf.d/*.conf`) | Step 3a; `sudo nginx -T \| grep -n "conf.d"` should show the include |
| `/ingest/rows` returns **501** | Expected until the operator has added a key: the edge has none | Nothing; it becomes 403 once the operator's files are in `/srv/ingest-edge/config` |
| `/ingest/rows` returns **502** | nginx is fine; the container isn't answering | `docker compose ps` should say `healthy`; if not, `docker compose logs ingest-edge` |
| `/ingest/rows` returns **404** | nginx didn't pick up the block (wrong file or server block) or wasn't reloaded | `sudo nginx -T \| grep -n "location /ingest/"` should show it once |
| `/ingest/rows` returns **429** | One address went over 50 requests a second or 20 open requests (`limit_req`/`limit_conn`) | Nothing for a partner at normal speed; if it is the platform's pull, raise `rate`/`limit_conn` in the limits file |
| `/` returns something other than 200 afterwards | Something else changed | Roll back first, then tell the edge operator |
| `docker compose build` can't download | The VM's outbound access to Docker Hub or PyPI is blocked | Both were reachable on 2026-10-07; retry, and tell the edge operator if it persists |
| Port 8787 already in use | Something new took it | Change `127.0.0.1:8787` in `compose.yaml` **and** the two `proxy_pass` lines to a free port |
| Logs show `Traceback … No module named 'mcp'`, `llm_serving`, `memory layer needs Postgres` or `no RAG store` at startup | The platform probes optional parts the edge doesn't use | Harmless if `/healthz` says ok |
| Logs say `no /config/sources.txt yet; no partner can push` | Expected until the operator adds partners | Nothing; the operator's step |
| Logs say `exists but is not readable by this container` | A file in the config folder is `600`; the container is another user | `chmod a+r` on it (the operator's script does this) |

## Day to day

- Logs: `docker compose logs -f ingest-edge`; nginx's own
  `/var/log/nginx/ingest-edge.access.log` (no query strings, no keys). Healthy
  lines include `edge: now accepting <source> for site <site>`.
- It restarts on its own after a reboot (`restart: unless-stopped`).
- Received data lives in the `edge-data` volume, held only until the platform
  pulls it. Watch the disk that volume is on: when free space falls below the
  floor the edge answers pushes `507` (partners hold their data and retry), and
  `/healthz` still says ok.
- The container runs read-only, with no extra privileges and at most 256
  processes, and its logs are capped at 5 files of 10 MB. `compose.yaml` has a
  commented `mem_limit: 1g` to switch on if you want a memory ceiling (the edge
  uses about 180 MiB).
