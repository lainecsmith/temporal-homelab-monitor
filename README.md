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
