# Disk-pressure ladder

What the ladder measures, what to do at each rung, and the two kubelet behaviours
that turn a full disk into an outage. The site repository holds the specifics: which
volumes a node depends on, the thresholds it uses, and its own commands.

## What it measures

`axi hygiene stat disk` reads, for every volume the node depends on (the container
runtime's data root, the database's volume, the bronze root):

- **free**: the bytes free on the filesystem;
- **floor**: the kubelet's hard eviction threshold for the signals that govern the
  volume (`evictionHard`, `nodefs.available` and `imagefs.available`), read from the
  running kubelet's `configz`;
- **minimum reclaim**: `evictionMinimumReclaim` for the same signals;
- **margin**: free minus the floor, or free minus (floor + minimum reclaim) while
  pressure is on;
- every `imagePullPolicy: Never` image a pod uses, and whether the node still has it.

A volume that no kubelet signal governs (a bind-mounted archive) has a floor of zero.
A k3d node's `nodefs` and `imagefs` both live on the container runtime's data root,
so that volume carries both signals.

| Rung | When (default; per site) | Sent as |
|---|---|---|
| warn | margin < 150 GB, or free < floor + minimum reclaim | normal |
| page | margin < 80 GB, or a never-pull image is missing from the node | high |
| critical | free < floor + 20 GB, or disk pressure is on | urgent |

`axi hygiene stat disk --notify` is the timer's form. It writes an inbox alert (which
the notifications gateway delivers) when the rung changes in either direction,
repeats a standing page or critical every hour and a standing warning every day,
and saves the reading that `axi status` shows as one line. The reading is machine
state, saved under the platform's directory (`~/.axi/hygiene/disk-ladder.json`)
whichever brand's command ran it, so the timer's `axi` and a person's `neut status`
see one reading. A failed notification exits non-zero and is retried on the next run.

Configuration is a TOML file named by `$AXIOM_DISK_LADDER_CONFIG`, else
`config/disk-ladder.toml` or `disk-ladder.toml` under the platform's state directory
(then the brand's), so a verb typed by hand finds the file a site's deploy installed:

```toml
[ladder]
warn_margin_gb = 150
page_margin_gb = 80
critical_over_floor_gb = 20
recipient = "@operator:site"      # else $AXIOM_DISK_LADDER_ALERT_TO, else the node's owner

[kubelet]
cluster = "my-cluster"            # the k3d cluster, used in the re-import command
kubectl_context = "k3d-my-cluster"
images = ["db-image:1.0"]          # never-pull images to check even with no pod running

[[volume]]
name = "container-data-root"
path = "auto:docker-data-root"     # or a literal path (preferred on a known node)
signals = ["nodefs", "imagefs"]

[[volume]]
name = "bronze"
path = "/srv/bronze"               # `~` is expanded
```

`auto:docker-data-root` is resolved by, in order: `docker info` (needs the docker
socket), `data-root` in `/etc/docker/daemon.json`, the overlay mounts of running
containers in `/proc/mounts`, and the k3d node containers' volume sources. When none
answers the volume is reported **unresolved**, the report is lifted to at least
`warn` and a person is told; the ladder never measures another path in its place.
A path behind a directory the node's user cannot search (a docker volume under a
root-only data root) is measured at its mountpoint from `/proc/mounts`: the same
filesystem, so the same numbers.

## The two traps

**Minimum reclaim keeps pressure on.** Once free space drops below the floor, the
kubelet keeps the `DiskPressure` condition until free space reaches the floor *plus*
the minimum reclaim. A percentage reclaim on a large shared volume is large: 10 % of
3.3 TB is 330 GB. Freeing 100 GB then changes nothing visible, pods stay evicted, and
replacements are rejected. Check `configz` before freeing files:

```bash
kubectl get --raw "/api/v1/nodes/<node>/proxy/configz" | jq '.kubeletconfig | {evictionHard, evictionMinimumReclaim}'
```

Set an absolute reclaim in the kubelet configuration (for k3s, in
`/etc/rancher/k3s/config.yaml`):

```yaml
kubelet-arg:
  - "eviction-minimum-reclaim=nodefs.available=5Gi,imagefs.available=5Gi"
```

then restart the kubelet. On k3d, restart the node container (`docker restart
<node>`); do not recreate it, because a recreate discards a configuration file edited
inside the container. Make the change durable in the cluster definition.

**Image garbage collection deletes never-pull images.** Under pressure the kubelet
deletes unused images, including one built locally with `imagePullPolicy: Never`.
The pod that needs it cannot start again until the image is back on the node:

```bash
k3d image import <image> -c <cluster>      # from the host's docker
```

If the host no longer has the image either, rebuild it on the host first.

## What to do at each rung

**warn.** Nothing is failing. Plan space within the week: list the largest
directories on the volume, move archives and backups to another volume or host
(verify a checksum before deleting the source), compress cold logs, and check the
minimum reclaim (above). Do not start a job that writes a large copy (a table
rewrite, a bulk import) without first working out its peak footprint against the
margin.

**page.** Act today. Stop or pause any job that is growing the volume, and read its
script before trusting it to free space: a copy-then-swap job holds both copies until
its last step. Offload with checksums, compress, and confirm the margin is rising
with `axi hygiene stat disk`. If a never-pull image is missing, re-import it now,
before a pod restart needs it.

**critical.** Eviction is imminent or happening. Stop writers first. Free space to
above the floor plus the minimum reclaim, not just above the floor. Re-import any
deleted never-pull image, then confirm the database pod is back and the serving
health check answers. Record what filled the volume and what freed it in the incident
log, and lower the minimum reclaim if it was a percentage.

**ok after a page or critical.** The ladder sends a recovery note. Check the evicted
pods restarted and nothing is stuck in `ContainerStatusUnknown` or `Evicted`; delete
those pod records once their replacements are running.
