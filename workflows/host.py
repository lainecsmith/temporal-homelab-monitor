"""The host entity. Runs on the controller worker; never touches the host directly."""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities.host_info import collect_host_info
    from activities.updates import check_updates
    from common import host_queue

POLL_INTERVAL = timedelta(seconds=30)
UPDATE_CHECK_INTERVAL = timedelta(hours=6)


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
            "updates": None,
            "updates_checked_at": None,
            "updates_error": None,
        }
        self.refresh_requested = False
        self.update_check_requested = False
        self.update_task: Optional[asyncio.Task] = None

    @workflow.run
    async def run(self, host: str, state: Optional[dict] = None) -> None:
        while True:
            self.refresh_requested = False
            await self._collect()
            if self._update_check_due():
                self.update_check_requested = False
                self.update_task = asyncio.create_task(self._check_updates())

            try:
                await workflow.wait_condition(
                    lambda: self.refresh_requested, timeout=POLL_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

            # A pending check_updates request isn't carried across continue-as-new, so hold off
            # until the loop has started that check. Re-checked after the awaits because a
            # signal can arrive while they run.
            if workflow.info().is_continue_as_new_suggested() and not self.update_check_requested:
                await workflow.wait_condition(workflow.all_handlers_finished)
                if self.update_task:
                    await self.update_task
                if not self.update_check_requested:
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

    def _update_check_due(self) -> bool:
        if self.state["status"] != "online" or (self.update_task and not self.update_task.done()):
            return False
        if self.update_check_requested or self.state["updates_checked_at"] is None:
            return True
        last = datetime.fromisoformat(self.state["updates_checked_at"])
        return workflow.now() - last >= UPDATE_CHECK_INTERVAL

    async def _check_updates(self) -> None:
        try:
            self.state["updates"] = await workflow.execute_activity(
                check_updates,
                task_queue=host_queue(self.host),
                # softwareupdate on macOS and registry lookups for many images can be slow.
                start_to_close_timeout=timedelta(minutes=10),
                schedule_to_start_timeout=timedelta(seconds=20),
                # On failure, wait for the next interval rather than retrying every cycle.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self.state["updates_error"] = None
        except ActivityError as e:
            self.state["updates_error"] = str(e.cause or e)
        # Set even on failure so a broken check isn't retried every cycle.
        self.state["updates_checked_at"] = workflow.now().isoformat()

    @workflow.signal
    def force_refresh(self) -> None:
        self.refresh_requested = True

    @workflow.signal(name="check_updates")
    def request_update_check(self) -> None:
        self.update_check_requested = True
        self.refresh_requested = True

    @workflow.query
    def snapshot(self) -> dict:
        return self.state
