# Ingest edge behind an existing nginx site: for the deployer

**What it is.** A small container that receives sensor data from partner sites
over HTTPS and holds it until the platform
pulls it. It serves `/ingest/` (partners push), `/edge/` (the platform
pulls, with its own key) and `/healthz`. It has no web app, no database and no
login page.

**What it changes on the VM.**
- One container on `127.0.0.1:8787`.
- One nginx block routing `/ingest/` and `/edge/` to it. It doesn't touch `/`,
  or any other path the site serves.
- One folder, `/srv/ingest-edge/config`, owned by the edge operator. It holds who may push: a
  list of sources and a file of key **hashes**, never a usable key. the operator adds or
  removes partners there himself, with no restart, so this is a one-time deploy.

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
curl -s http://127.0.0.1:8787/healthz  # -> {"status":"ok"}

# 3. nginx: paste nginx-ingest-edge.conf into the HTTPS server block for
#    <your-host> in /etc/nginx/sites-available/<your-site>,
#    BEFORE the line "    location / {". Then:
sudo cp /etc/nginx/sites-available/<your-site> ~/<your-site>.bak   # do this before editing
sudo nginx -t && sudo systemctl reload nginx
```

## Check it worked (from anywhere)

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-host>/ingest/rows   # 501 now (no partners yet), 403 once the edge operator adds them: either means it is up
curl -s -o /dev/null -w "%{http_code}\n" https://<your-host>/                     # 200: the site is untouched
```

That's all. Tell the edge operator, and he adds the partners.

## Roll back

```bash
sudo cp ~/<your-site>.bak /etc/nginx/sites-available/<your-site> && sudo nginx -t && sudo systemctl reload nginx
docker compose down        # add -v only to also delete data not yet pulled
```

## If something goes wrong

| What you see | Why | Fix |
|---|---|---|
| `required variable EDGE_CONFIG_DIR is missing` on any `docker compose` command | `.env` isn't there yet; compose reads it for every command, including `down` | `cp dot-env.example .env`, then rerun |
| `nginx -t` fails after pasting the block | The block landed outside the HTTPS `server { }`, or a brace is missing | Restore the backup, paste again just above `    location / {` in the **first** HTTPS server block (the one whose `server_name` is your public host name) |
| `/ingest/rows` returns **501** | Expected until the edge operator adds partners: the edge has no keys yet | Nothing; it becomes 403 once the edge operator's files are in `/srv/ingest-edge/config` |
| `/ingest/rows` returns **502** | nginx is fine; the container isn't answering | `docker compose ps` should say `healthy`; if not, `docker compose logs ingest-edge` |
| `/ingest/rows` returns **404** | nginx didn't pick up the block (wrong file or server block) or wasn't reloaded | `sudo nginx -T \| grep -n "location /ingest/"` should show it once |
| `/` returns something other than 200 afterwards | Something else changed | Roll back first, then tell the edge operator |
| `docker compose build` can't download | The VM's outbound access to Docker Hub or PyPI is blocked | Both were reachable on 2026-10-07; retry, and tell the edge operator if it persists |
| Port 8787 already in use | Something new took it | Change `127.0.0.1:8787` in `compose.yaml` **and** the two `proxy_pass` lines to a free port |
| Logs show `Traceback … No module named 'mcp'`, `llm_serving`, `memory layer needs Postgres` or `no RAG store` at startup | The platform probes optional parts the edge doesn't use | Harmless if `/healthz` says ok |
| Logs say `no /config/sources.txt yet; no partner can push` | Expected until the edge operator adds partners | Nothing; the edge operator's step |

## Day to day

- Logs: `docker compose logs -f ingest-edge`. Healthy lines include
  `edge: now accepting <source> for site <site>`.
- It restarts on its own after a reboot (`restart: unless-stopped`).
- Received data lives in the `edge-data` volume, held only until the platform
  pulls it.
