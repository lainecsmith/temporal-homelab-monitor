"""The only module that talks to Temporal. Thin on purpose: everything with logic in it
(health classification, fan-out, storage) lives in service.py and is tested against a fake
of this class. Strings are used for workflow and signal names so the dashboard never has to
import the workflow code."""
import asyncio
import re
import time
from datetime import timedelta
from typing import Optional

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from common import host_workflow_id
from dashboard.errors import GatewayError

ALLOWED_SIGNALS = {"force_refresh", "check_updates"}
HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
RUNNING_HOSTS = 'WorkflowType = "HostWorkflow" AND ExecutionStatus = "Running"'
# If the server can't tell us about pollers, don't guess: the caller falls back to inferring
# controller health from whether host queries succeed.
_POLLER_INFO_UNAVAILABLE = {RPCStatusCode.UNIMPLEMENTED, RPCStatusCode.INVALID_ARGUMENT,
                            RPCStatusCode.PERMISSION_DENIED}


class TemporalGateway:
    def __init__(self, address: str, namespace: str, query_timeout_s: float = 5.0):
        self.address, self.namespace, self.query_timeout_s = address, namespace, query_timeout_s
        self.client: Optional[Client] = None

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def connect(self) -> None:
        try:
            self.client = await asyncio.wait_for(Client.connect(self.address, namespace=self.namespace), 10)
        except Exception as e:
            raise GatewayError("unreachable", f"Can't connect to Temporal at {self.address}: {e}") from e

    async def poller_ages(self, queue: str) -> Optional[list]:
        """Seconds since each worker last polled the queue; None if the server can't say."""
        req = DescribeTaskQueueRequest(
            namespace=self.namespace, task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW)
        try:
            resp = await self.client.workflow_service.describe_task_queue(req, timeout=timedelta(seconds=3))
        except RPCError as e:
            if e.status in _POLLER_INFO_UNAVAILABLE:
                return None
            raise GatewayError("unreachable", f"Temporal request failed: {e}") from e
        except Exception as e:
            raise GatewayError("unreachable", str(e)) from e
        now = time.time()
        return [now - p.last_access_time.ToSeconds() for p in resp.pollers]

    async def host_ids(self) -> list:
        prefix = host_workflow_id("")
        try:
            return [wf.id[len(prefix):] async for wf in self.client.list_workflows(RUNNING_HOSTS)
                    if wf.id.startswith(prefix)]
        except Exception as e:
            raise GatewayError("unreachable", f"Couldn't list hosts: {e}") from e

    async def snapshot(self, host: str) -> dict:
        handle = self.client.get_workflow_handle(host_workflow_id(host))
        try:
            return await asyncio.wait_for(
                handle.query("snapshot", rpc_timeout=timedelta(seconds=self.query_timeout_s)),
                self.query_timeout_s + 1)
        except asyncio.TimeoutError as e:
            raise GatewayError("timeout", f"{host}: query timed out") from e
        except RPCError as e:
            kind = "unreachable" if e.status in (RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED) else "query_failed"
            raise GatewayError(kind, f"{host}: {e}") from e
        except Exception as e:
            raise GatewayError("query_failed", f"{host}: {e}") from e

    async def signal(self, host: str, name: str) -> None:
        if name not in ALLOWED_SIGNALS or not HOST_NAME.match(host):
            raise GatewayError("invalid", "Unknown signal or host name.")
        handle = self.client.get_workflow_handle(host_workflow_id(host))
        try:
            await handle.signal(name)
        except RPCError as e:
            kind = "not_found" if e.status == RPCStatusCode.NOT_FOUND else "unreachable"
            raise GatewayError(kind, f"{host}: {e}") from e
        except Exception as e:
            raise GatewayError("unreachable", f"{host}: {e}") from e
