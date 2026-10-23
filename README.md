# homelab-monitor: Step 1 walking skeleton

## Run it

    uv sync

    temporal server start-dev            # terminal 1
    uv run worker.py                     # terminal 2 (controller + host worker)
    uv run starter.py                    # terminal 3

Query it any time:

    temporal workflow query --workflow-id host-$(hostname -s | tr A-Z a-z) --type snapshot

Force a refresh:

    temporal workflow signal --workflow-id host-<name> --name force_refresh

Check for updates now (otherwise every 6 hours; results are in the snapshot's `updates`):

    temporal workflow signal --workflow-id host-<name> --name check_updates

Web UI: http://localhost:8233

## Step 2 checks (split the roles)

    # central box
    uv run worker.py --role controller --address <server>:7233
    # each homelab host
    uv run worker.py --role host --address <server>:7233
    # start the entity for a host
    uv run starter.py --host <name> --address <server>:7233

Kill the host worker: status flips to "offline" within ~20s of the next cycle
(schedule_to_start timeout). Restart it: status returns to "online".

Note: dev server binds to localhost by default. For LAN workers start it with
`temporal server start-dev --ip 0.0.0.0`.

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
