# homelab-monitor

Runs as two roles: one central box with the Temporal server, the controller
worker and the dashboard, and a host worker on every homelab machine you want
to monitor (the central box can be one of them).

## Run it

Install dependencies on every machine:

    uv sync

### Central box

    # Temporal server, reachable from the LAN (Web UI on :8233)
    temporal server start-dev --ip 0.0.0.0

    # controller worker: runs the HostWorkflow for every host
    uv run worker.py --role controller

    # dashboard, from the repo root; open http://localhost:8000
    uv run uvicorn dashboard.app:app --port 8000

The dev server keeps everything in memory, so after restarting it, a host only
reappears once its worker restarts.

The dashboard binds to loopback and has no authentication. Its settings and
details are in [dashboard/README.md](dashboard/README.md).

### Each homelab host

    uv run worker.py --role host --address <central-box>:7233

On startup the host worker registers its own entity workflow (`host-<name>`,
where `<name>` is the short, lowercase hostname; override with
`--hostname`) and triggers an immediate refresh. There's no separate
registration step. Both roles also take `--namespace` (default `default`).

To see a host's first snapshot from the command line (optional; this also
registers the host if its worker hasn't yet):

    uv run starter.py --host <name> --address <central-box>:7233

### Talking to a host

Query its snapshot:

    temporal workflow query --address <central-box>:7233 --workflow-id host-<name> --type snapshot

Force a refresh:

    temporal workflow signal --address <central-box>:7233 --workflow-id host-<name> --name force_refresh

Check for updates now (otherwise every 6 hours; results are in the snapshot's `updates`):

    temporal workflow signal --address <central-box>:7233 --workflow-id host-<name> --name check_updates

The dashboard's Refresh and Check for updates buttons send the same signals.

### Offline hosts

Stop a host worker and its status flips to "offline" within ~20s of the next
cycle (schedule_to_start timeout). Start it again and it returns to "online".

## Deploy

Ansible deploys both roles against an existing Temporal server:

- **Central box:** the controller worker and the dashboard run in Docker
  Compose ([deploy/docker-compose.yml](deploy/docker-compose.yml)). The machine
  needs Docker with the Compose plugin.
- **Each monitored host:** the host worker runs natively as the
  `homelab-monitor-host` systemd service, as a `homelab-monitor` system user.
  It isn't containerized because it reports on the host itself. Supports
  Ubuntu and 64-bit Raspberry Pi OS; 32-bit is refused because the Temporal
  Python SDK has no 32-bit ARM builds.

Hosts clone this repo from GitHub, so push before deploying. Then, from your
own machine:

    cd deploy/ansible
    cp inventory.example.yml inventory.yml    # fill in temporal_address and your machines
    ansible-playbook site.yml                 # add --ask-become-pass if sudo needs a password

Re-run the same command to update everything. To update only the hosts, add
`--limit monitored`. To deploy a branch, tag or commit other than `main`, add
`-e monitor_version=<ref>`. Deploy the controller and the host workers
together so their code stays in step.

On the central box, `temporal_address` has to be the server's LAN address,
not `localhost`, because the containers resolve `localhost` to themselves. The
dashboard listens on port 8000 on all interfaces and has no authentication;
set `dashboard_bind: 127.0.0.1` in the inventory to keep it local.

If Docker is already installed on a host, the playbook adds the service user
to the `docker` group. That gives the user root-equivalent access to the
machine; it's what lets the worker list containers. If you install Docker
later, re-run the playbook.

Logs:

    journalctl -u homelab-monitor-host -f                                          # on a host
    docker compose -f /opt/homelab-monitor-controller/deploy/docker-compose.yml logs -f   # central box

## Docker containers

If `docker` is installed on a host, the snapshot's `info.docker` lists every
container, including stopped ones: name, image (`image_name`, `image_tag`, and
`image_version` from the image's `org.opencontainers.image.version` label),
state, status, health and ports. Running containers also get `cpu_pct`,
`mem_used_mb`, `mem_limit_mb` and `mem_pct`. `cpu_pct` is per core, so it can
go above 100%.

The user running the host worker must be able to run `docker ps` without sudo:

    id -nG                            # should include "docker"
    sudo usermod -aG docker $USER     # if not; then log out and back in, and restart the worker

If it can't reach Docker, `info.docker.error` says why. `info.docker` is `null`
when Docker isn't installed.

Stopped containers stay in the list until they're removed (`docker rm <name>`,
or `docker container prune` for all of them).

## Update checks

Every 6 hours, or on the `check_updates` signal, each host reports pending
updates in the snapshot's `updates`:

- `updates.host`: OS packages from apt, dnf, pacman (`checkupdates`) or macOS
  `softwareupdate`. On apt hosts it also counts security updates and reports
  whether a reboot is required. apt's package index isn't refreshed (that needs
  root); `index_age_h` shows how old it is.
- `updates.docker.images`: one entry per image, with the containers using it.
  - `status`: `update_available` if the registry has a newer build of the same
    tag, otherwise `up_to_date` or `unknown` (reason in `error`).
  - `newest_tag` / `newest_tag_same_major`: newer version tags, e.g.
    `postgres:16.4` -> `18.6` / `16.15`. Only tags shaped like the current one
    count, so `-alpine` stays `-alpine` and pre-releases like `-rc1` are skipped.
    Both are `null` for non-version tags like `latest`; errors are in
    `tags_error`.

Registry lookups use anonymous access, so private images show as `unknown`.
They don't count against Docker Hub's pull rate limit.
