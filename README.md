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
  Compose ([deploy/docker-compose.yml](deploy/docker-compose.yml)), checked
  out to `/opt/homelab-monitor-controller`.
- **Each monitored host:** the host worker runs natively as the
  `homelab-monitor-host` systemd service, as a `homelab-monitor` system user,
  checked out to `/opt/homelab-monitor`. It isn't containerized because it
  reports on the host itself.

Every machine clones this repo from GitHub, so a deploy only picks up commits
you've pushed. Run all commands below from `deploy/ansible` on your own
machine.

### First deploy

1. **Check the prerequisites.**
   - Ansible on your machine (`pipx install ansible-core`), or prefix each
     `ansible*` command with `uvx --from ansible-core`.
   - SSH key access to every machine, as a user that can `sudo`.
   - Every host runs Ubuntu or 64-bit Raspberry Pi OS. On each Pi,
     `dpkg --print-architecture` must print `arm64`; the playbook refuses
     32-bit, because the Temporal Python SDK has no 32-bit ARM builds.
   - The central box has Docker with the Compose plugin
     (`docker compose version` works).
   - The Temporal server is running, and every machine can reach it on port
     7233.

2. **Write the inventory.**

        cp inventory.example.yml inventory.yml

   Set `temporal_address` to the server's LAN address, not `localhost`
   (inside the containers, `localhost` is the container itself). Put the
   central box under `controller`, and every machine to monitor under
   `monitored`; the central box can be in both. `inventory.yml` is
   git-ignored.

3. **Check Ansible can reach everything.**

        ansible all -m ping

4. **Deploy.**

        ansible-playbook site.yml    # add --ask-become-pass if sudo needs a password

   The first run on the central box builds the Docker image, which takes a
   few minutes.

5. **Check it worked.**
   - Dashboard: `http://<central-box>:8000`. Each host should appear within a
     minute.
   - A host's worker: `systemctl status homelab-monitor-host`.
   - From Temporal:
     `temporal workflow list --address <temporal>:7233 --query 'WorkflowType="HostWorkflow"'`
     lists one `host-<name>` workflow per host.

### Later deploys

Push your changes to `main`, then:

    ansible-playbook site.yml

The playbook only restarts a host worker when its code, dependencies, unit
file or group membership changed. The central box always rebuilds the image,
but only recreates containers when it changed.

Deploy the controller and the host workers together (the plain command
above) so their code stays in step. Other options:

- **A different branch, tag or commit** (including rolling back to an older
  one): `ansible-playbook site.yml -e monitor_version=<ref>`. The next plain
  run goes back to `main`.
- **Add a host:** add it under `monitored` in the inventory, then
  `ansible-playbook site.yml --limit <name>`.
- **Remove a host:** the playbook doesn't do this. Stop the worker and end its
  workflow:

        ssh <host> sudo systemctl disable --now homelab-monitor-host
        temporal workflow terminate --address <temporal>:7233 --workflow-id host-<name>

  Then remove it from the inventory. The dashboard keeps showing the host's
  last known data, marked as not running.

### Security and logs

The dashboard listens on port 8000 on all interfaces and has no
authentication. Set `dashboard_bind: 127.0.0.1` in the inventory to keep it
local to the central box.

If Docker is installed on a host, the playbook adds the service user to the
`docker` group so the worker can list containers. That gives the user
root-equivalent access to the machine. If you install Docker on a host later,
re-run the playbook.

Logs:

    journalctl -u homelab-monitor-host -f                                                # on a host
    docker compose -f /opt/homelab-monitor-controller/deploy/docker-compose.yml logs -f  # central box

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

## TODO

### Apply updates from the dashboard

Already listed under "Not done yet" in [dashboard/README.md](dashboard/README.md).
Plan:

- **Add a replay test first.** Save a real `HostWorkflow` history
  (`temporal workflow show --workflow-id host-<name> --output json`) and replay
  it against the current code with the SDK's `Replayer` in pytest. This catches
  non-determinism errors before a deploy, with or without Worker Versioning.
- **Keep the workflow change replay-safe.** Add an Update handler (it can return
  the result to the page, which also fixes the Refresh workaround described in
  the dashboard README). Start the new `apply_updates` activity only from that
  handler, or from the loop behind a flag only the handler sets. Running
  histories never contain the Update, so they replay unchanged. Don't change
  what the loop does every cycle (adding activities, reordering `_collect` and
  the update check, changing the wait); if that's ever needed, use
  `workflow.patched()` or Worker Versioning.
- **Give the host worker narrow root access.** It runs as the unprivileged
  `homelab-monitor` user. Have the playbook install a sudoers rule that allows
  only the specific upgrade commands, and have the activity run only those.
- **Design the activity for slow, partial work.** Upgrades can take many
  minutes and can leave a reboot pending: heartbeat, use a long
  `start_to_close_timeout`, don't retry automatically, and refresh the snapshot
  (including `reboot_required`) afterwards.
- **Deploy the hosts first.** Host queues aren't versioned, so if the
  controller calls `apply_updates` before a host worker has it, that call
  fails. Run `ansible-playbook site.yml --limit monitored`, then the full
  playbook.

### Worker Versioning (later)

Not needed for the above. Worth adding when we want to make breaking workflow
changes without patching.

- **Workflow:** mark `HostWorkflow` Pinned. In the loop, also continue-as-new
  when `workflow.info().is_target_worker_deployment_version_changed()`, passing
  `initial_versioning_behavior=ContinueAsNewVersioningBehavior.AUTO_UPGRADE`, so
  each host moves to a new version within about one 30-second cycle. (Upgrade
  on continue-as-new is in Public Preview.) Fill new `state` keys with defaults,
  since a new version receives the previous version's `state`.
- **Workers:** only the controller gets a `deployment_config`, with the git
  commit as its build ID. Host workers stay unversioned: they only run
  activities on their own queues, so their in-place systemd deploy keeps
  working as long as activity inputs and outputs stay compatible.
- **Controller deploy:** replace the in-place `docker compose up -d` with:
  start the new version's container alongside the old one, run
  `temporal worker deployment set-current-version`, wait until no running
  workflows are on the old version, then stop the old container. The central
  box needs the Temporal CLI, or a small SDK script that does the same.
- **Check first:** the Temporal server must be at least 1.29.1
  (`temporal operator cluster system`). The dashboard's controller health check
  counts pollers with `DescribeTaskQueue` ([dashboard/gateway.py](dashboard/gateway.py)),
  which may not show versioned pollers; if so, it would report the controller
  as down and needs to describe the worker deployment version instead.
- **First switch:** existing unversioned workflows move to the first Current
  version on their next task (within 30 seconds here). Keep the unversioned
  controller running until they have.
