"""Create (or nudge) the entity workflow for a host. Idempotent via signal-with-start."""
import argparse
import asyncio

from temporalio.client import Client

from common import CONTROLLER_QUEUE, host_workflow_id, local_hostname
from workflows.host import HostWorkflow


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=local_hostname())
    p.add_argument("--address", default="localhost:7233")
    p.add_argument("--namespace", default="default")
    args = p.parse_args()

    client = await Client.connect(args.address, namespace=args.namespace)
    handle = await client.start_workflow(
        HostWorkflow.run,
        args=[args.host],
        id=host_workflow_id(args.host),
        task_queue=CONTROLLER_QUEUE,
        start_signal="force_refresh",
    )
    print(f"entity running: {handle.id}")

    await asyncio.sleep(3)
    print(await handle.query(HostWorkflow.snapshot))


if __name__ == "__main__":
    asyncio.run(main())
