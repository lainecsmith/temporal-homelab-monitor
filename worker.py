"""
Run the controller worker, the host worker, or both.

  uv run worker.py                    # both (Step 1, single process)
  uv run worker.py --role controller  # central box: runs HostWorkflow
  uv run worker.py --role host        # each homelab host: runs collection activities
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from activities.host_info import collect_host_info
from activities.updates import check_updates
from common import CONTROLLER_QUEUE, host_queue, host_workflow_id, local_hostname
from workflows.host import HostWorkflow


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--role", choices=["controller", "host", "both"], default="both")
    p.add_argument("--address", default="localhost:7233")
    p.add_argument("--namespace", default="default")
    p.add_argument("--hostname", default=local_hostname())
    args = p.parse_args()

    client = await Client.connect(args.address, namespace=args.namespace)

    if args.role in ("host", "both"):
        # Register this host's entity (signal-with-start: idempotent, and the signal
        # triggers an immediate refresh). Started by name so a host agent doesn't need
        # to import the workflow. Deliberately not caught: if registration fails, crash
        # so systemd restarts us and the cause shows up in the logs, rather than running
        # as a host the dashboard never sees.
        await client.start_workflow(
            "HostWorkflow",
            args=[args.hostname],
            id=host_workflow_id(args.hostname),
            task_queue=CONTROLLER_QUEUE,
            start_signal="force_refresh",
        )

    workers = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        if args.role in ("controller", "both"):
            workers.append(Worker(
                client, task_queue=CONTROLLER_QUEUE, workflows=[HostWorkflow]
            ))
        if args.role in ("host", "both"):
            workers.append(Worker(
                client,
                task_queue=host_queue(args.hostname),
                activities=[collect_host_info, check_updates],
                activity_executor=executor,
            ))

        print(f"running role={args.role} host={args.hostname}")
        await asyncio.gather(*(w.run() for w in workers))


if __name__ == "__main__":
    asyncio.run(main())
