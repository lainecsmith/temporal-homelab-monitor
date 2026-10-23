"""Polls Temporal, keeps the SQLite copy current, and builds the API payloads.

Pages read the SQLite copy, never Temporal directly, so they load fast and still work when
the controller or Temporal is down. `health` says how far to trust what's shown."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable

from dashboard.errors import GatewayError
from dashboard.normalize import fleet_summary, normalize
from dashboard.store import Store

log = logging.getLogger("dashboard")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fleet:
    def __init__(self, gateway, store: Store, controller_queue: str, interval_s: float = 15.0,
                 poller_max_age_s: float = 60.0, now: Callable[[], str] = _utcnow):
        self.gateway, self.store, self.controller_queue = gateway, store, controller_queue
        self.interval_s, self.poller_max_age_s, self.now = interval_s, poller_max_age_s, now
        self.health = {"state": "starting", "detail": None, "checked_at": None}
        self.failures: dict = {}      # host -> error, from the last healthy poll
        self.listed = None            # hosts whose workflow was running at the last healthy poll
        self._poll_lock = asyncio.Lock()  # the page can ask for a poll while the background one runs

    def _set_health(self, state: str, detail=None) -> None:
        self.health = {"state": state, "detail": detail, "checked_at": self.now()}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # the poller must never die
                log.exception("poll failed")
                self._set_health("dashboard_error", str(e))
            await asyncio.sleep(self.interval_s)

    async def poll_once(self) -> None:
        async with self._poll_lock:
            await self._poll()

    async def _poll(self) -> None:
        # 1. Can we reach Temporal, and is anything polling the controller queue?
        try:
            if not self.gateway.connected:
                await self.gateway.connect()
            ages = await self.gateway.poller_ages(self.controller_queue)
        except GatewayError as e:
            return self._set_health("temporal_unreachable", str(e))
        if ages is not None and not any(a <= self.poller_max_age_s for a in ages):
            return self._set_health("controller_down", "No worker is polling the controller queue.")

        # 2. Which hosts exist, and what do they say? A host being offline is a successful
        #    query; only a control plane problem makes queries fail.
        try:
            hosts = await self.gateway.host_ids()
        except GatewayError as e:
            return self._set_health("temporal_unreachable", str(e))
        results = await asyncio.gather(*(self._fetch(h) for h in hosts))
        good = [(h, s) for h, s, e in results if e is None]
        bad = {h: e for h, s, e in results if e is not None}

        if hosts and not good:
            # Every query failed. Poller info said the controller was fine (or wasn't available),
            # so infer the cause from the errors.
            first = next(iter(bad.values()))
            if any(e.kind == "unreachable" for e in bad.values()):
                return self._set_health("temporal_unreachable", str(first))
            return self._set_health("controller_down", f"Every host query failed ({first}).")

        fetched_at = self.now()
        for host, snapshot in good:
            self.store.save(host, snapshot, fetched_at)
        self.listed = set(hosts)
        self.failures = {h: str(e) for h, e in bad.items()}
        self._set_health("ok")

    async def _fetch(self, host: str):
        try:
            return host, await self.gateway.snapshot(host), None
        except GatewayError as e:
            return host, None, e

    async def signal(self, host: str, name: str) -> dict:
        """Signals are accepted without a worker, so this works while the controller is down;
        the host acts on it once the controller is back. `queued` tells the UI which case it is."""
        await self.gateway.signal(host, name)
        return {"sent": True, "queued": self.health["state"] != "ok"}

    def view(self) -> dict:
        ages = self.store.oldest_ages()
        rows = {host: (fetched_at, snapshot) for host, fetched_at, snapshot in self.store.rows()}
        for host in self.failures:
            # A host whose queries have never succeeded has nothing stored, but it exists
            # and the page should say so rather than hide it.
            rows.setdefault(host, (None, {"host": host, "status": "unknown", "info": None, "last_seen": None,
                                          "last_error": None, "updates": None, "updates_checked_at": None,
                                          "updates_error": None}))
        hosts = []
        for host in sorted(rows):
            fetched_at, snapshot = rows[host]
            n = normalize(snapshot)
            if self.health["state"] != "ok":
                reason = self.health["state"]
            elif host in self.failures:
                reason = "entity_not_responding"
            elif self.listed is not None and host not in self.listed:
                reason = "workflow_not_running"
            else:
                reason = None
            n["meta"] = {
                "fetched_at": fetched_at, "stale": reason is not None, "stale_reason": reason,
                "error": self.failures.get(host),
                **ages.get(host, {"oldest_update_days": None, "oldest_security_update_days": None}),
            }
            hosts.append(n)
        return {"control_plane": self.health, "generated_at": self.now(),
                "fleet": fleet_summary(hosts), "hosts": hosts}

    def host_view(self, host: str):
        found = next((h for h in self.view()["hosts"] if h["host"] == host), None)
        if found is not None:
            found["pending_updates"] = self.store.pending_updates(host)
        return found
