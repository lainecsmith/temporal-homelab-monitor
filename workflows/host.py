"""The host entity. Runs on the controller worker; never touches the host directly."""
import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities.host_info import collect_host_info
    from common import host_queue

POLL_INTERVAL = timedelta(seconds=30)


@workflow.defn
class HostWorkflow:
    @workflow.init
    def __init__(self, host: str, state: Optional[dict] = None) -> None:
        self.host = host
        self.state = state or {
            "host": host,
            "status": "unknown",
            "info": None,
            "last_seen": None,
            "last_error": None,
        }
        self.refresh_requested = False

    @workflow.run
    async def run(self, host: str, state: Optional[dict] = None) -> None:
        while True:
            self.refresh_requested = False
            await self._collect()

            try:
                await workflow.wait_condition(
                    lambda: self.refresh_requested, timeout=POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(args=[self.host, self.state])

    async def _collect(self) -> None:
        try:
            info = await workflow.execute_activity(
                collect_host_info,
                task_queue=host_queue(self.host),
                start_to_close_timeout=timedelta(seconds=30),
                # If no host worker picks it up in time, the host is offline.
                schedule_to_start_timeout=timedelta(seconds=20),
                # Collection is cheap and re-runs next cycle; no retry storms.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self.state["info"] = info
            self.state["status"] = "online"
            self.state["last_seen"] = workflow.now().isoformat()
            self.state["last_error"] = None
        except ActivityError as e:
            self.state["status"] = "offline"
            self.state["last_error"] = str(e.cause or e)

    @workflow.signal
    def force_refresh(self) -> None:
        self.refresh_requested = True

    @workflow.query
    def snapshot(self) -> dict:
        return self.state
