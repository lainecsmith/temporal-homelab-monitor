import asyncio
import logging

from dashboard.builders import apt, container, info, snap, upd
from dashboard.errors import GatewayError
from dashboard.service import Fleet
from dashboard.store import Store


class FakeGateway:
    """Stands in for TemporalGateway. Records what it was asked so tests can check what wasn't."""

    def __init__(self, hosts=(), snapshots=None, ages=(1.0,), connect_error=None, query_errors=None,
                 signal_error=None):
        self.hosts, self.snapshots, self.ages = list(hosts), snapshots or {}, ages
        self.connect_error, self.query_errors, self.signal_error = connect_error, query_errors or {}, signal_error
        self.connected, self.queried, self.signals = False, [], []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def poller_ages(self, queue):
        if isinstance(self.ages, Exception):
            raise self.ages
        return None if self.ages is None else list(self.ages)

    async def host_ids(self):
        return list(self.hosts)

    async def snapshot(self, host):
        self.queried.append(host)
        if host in self.query_errors:
            raise self.query_errors[host]
        return self.snapshots[host]

    async def signal(self, host, name):
        self.signals.append((host, name))
        if self.signal_error:
            raise self.signal_error


def fleet(gw, store=None):
    return Fleet(gw, store or Store(), "controller", now=lambda: "2026-10-23T14:00:00+00:00")


def poll(f):
    asyncio.run(f.poll_once())


def hosts_by_name(f):
    return {h["host"]: h for h in f.view()["hosts"]}


def two_hosts():
    return {"a": snap("a"), "b": snap("b", status="offline")}


def test_healthy_poll_stores_snapshots_and_reports_ok():
    f = fleet(FakeGateway(["a", "b"], two_hosts())); poll(f)
    assert f.health["state"] == "ok"
    h = hosts_by_name(f)
    assert set(h) == {"a", "b"} and not h["a"]["meta"]["stale"]
    assert h["b"]["state"] == "offline" and not h["b"]["meta"]["stale"]  # offline is data, not a failure


def test_before_the_first_poll_stored_data_is_marked_stale():
    store = Store(); store.save("a", snap("a"), "earlier")
    h = hosts_by_name(fleet(FakeGateway(), store))["a"]
    assert h["meta"]["stale"] and h["meta"]["stale_reason"] == "starting"


def test_controller_down_sends_no_host_queries_and_serves_last_known_data():
    store = Store(); store.save("a", snap("a"), "earlier")
    gw = FakeGateway(["a"], two_hosts(), ages=[])  # reachable, but nothing polling
    f = fleet(gw, store); poll(f)
    assert f.health["state"] == "controller_down" and gw.queried == []
    a = hosts_by_name(f)["a"]
    assert a["meta"]["stale"] and a["meta"]["stale_reason"] == "controller_down" and a["has_data"]


def test_a_poller_that_stopped_a_while_ago_counts_as_down():
    f = fleet(FakeGateway(["a"], two_hosts(), ages=[300.0, 400.0])); poll(f)
    assert f.health["state"] == "controller_down"


def test_temporal_unreachable_on_connect():
    store = Store(); store.save("a", snap("a"), "earlier")
    f = fleet(FakeGateway(connect_error=GatewayError("unreachable", "no route")), store); poll(f)
    assert f.health["state"] == "temporal_unreachable" and "no route" in f.health["detail"]
    assert hosts_by_name(f)["a"]["meta"]["stale_reason"] == "temporal_unreachable"


def test_temporal_unreachable_when_listing_hosts_fails():
    class Broken(FakeGateway):
        async def host_ids(self):
            raise GatewayError("unreachable", "dropped")
    f = fleet(Broken()); poll(f)
    assert f.health["state"] == "temporal_unreachable"


def test_one_failing_host_is_its_own_state_and_the_others_are_fine():
    gw = FakeGateway(["a", "b"], two_hosts(), query_errors={"b": GatewayError("query_failed", "b: workflow task failing")})
    f = fleet(gw); poll(f)
    assert f.health["state"] == "ok"
    h = hosts_by_name(f)
    assert not h["a"]["meta"]["stale"]
    assert h["b"]["meta"]["stale_reason"] == "entity_not_responding" and "failing" in h["b"]["meta"]["error"]


def test_every_query_failing_is_read_as_controller_down_even_when_pollers_look_fine():
    errs = {h: GatewayError("timeout", f"{h}: timed out") for h in "ab"}
    f = fleet(FakeGateway(["a", "b"], two_hosts(), query_errors=errs)); poll(f)
    assert f.health["state"] == "controller_down" and "timed out" in f.health["detail"]


def test_without_poller_info_the_fallback_still_tells_controller_down_from_a_healthy_fleet():
    errs = {h: GatewayError("timeout", "t") for h in "ab"}
    down = fleet(FakeGateway(["a", "b"], two_hosts(), ages=None, query_errors=errs)); poll(down)
    ok = fleet(FakeGateway(["a", "b"], two_hosts(), ages=None)); poll(ok)
    assert down.health["state"] == "controller_down" and ok.health["state"] == "ok"


def test_every_query_unreachable_is_temporal_unreachable():
    errs = {h: GatewayError("unreachable", "gone") for h in "ab"}
    f = fleet(FakeGateway(["a", "b"], two_hosts(), query_errors=errs)); poll(f)
    assert f.health["state"] == "temporal_unreachable"


def test_a_host_whose_workflow_stopped_running_is_flagged():
    gw = FakeGateway(["a", "b"], two_hosts()); f = fleet(gw); poll(f)
    gw.hosts = ["a"]; poll(f)
    h = hosts_by_name(f)
    assert h["b"]["meta"]["stale_reason"] == "workflow_not_running" and not h["a"]["meta"]["stale"]


def test_recovery_clears_the_stale_state():
    gw = FakeGateway(["a"], two_hosts(), ages=[]); f = fleet(gw); poll(f)
    gw.ages = [1.0]; poll(f)
    assert f.health["state"] == "ok" and not hosts_by_name(f)["a"]["meta"]["stale"]


def test_the_poller_survives_an_unexpected_error():
    class Boom(FakeGateway):
        async def host_ids(self):
            raise RuntimeError("boom")
    f = fleet(Boom())
    f.interval_s = 0.01
    logging.getLogger("dashboard").setLevel(logging.CRITICAL)  # expected errors; keep the output quiet

    async def run():
        task = asyncio.create_task(f.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(run())
    assert f.health["state"] == "dashboard_error" and "boom" in f.health["detail"]


def test_signals_report_whether_they_were_queued():
    gw = FakeGateway(["a"], two_hosts()); f = fleet(gw); poll(f)
    assert asyncio.run(f.signal("a", "force_refresh")) == {"sent": True, "queued": False}
    gw.ages = []; poll(f)
    assert asyncio.run(f.signal("a", "check_updates")) == {"sent": True, "queued": True}
    assert gw.signals == [("a", "force_refresh"), ("a", "check_updates")]


def test_signal_errors_propagate_for_the_api_to_map():
    f = fleet(FakeGateway(signal_error=GatewayError("not_found", "no such host")))
    try:
        asyncio.run(f.signal("zzz", "force_refresh"))
        assert False, "expected GatewayError"
    except GatewayError as e:
        assert e.kind == "not_found"


def test_host_view_includes_pending_updates_with_age():
    gw = FakeGateway(["a"], {"a": snap("a", inf=info(docker=None), updates_checked_at="2020-01-01T00:00:00+00:00",
                                         updates=upd(host=apt(count=2, security=1)))})
    f = fleet(gw); poll(f)
    v = f.host_view("a")
    assert len(v["pending_updates"]) == 2 and v["meta"]["oldest_update_days"] > 1000
    assert f.host_view("nope") is None


def test_a_host_that_has_never_answered_still_appears():
    gw = FakeGateway(["a", "new"], {"a": snap("a")},
                     query_errors={"new": GatewayError("query_failed", "new: workflow task failing")})
    f = fleet(gw); poll(f)
    h = hosts_by_name(f)["new"]
    assert h["state"] == "collecting" and not h["has_data"]
    assert h["meta"]["stale_reason"] == "entity_not_responding" and h["meta"]["fetched_at"] is None


def test_polls_run_one_at_a_time():
    events = []

    class Recording(FakeGateway):
        async def poller_ages(self, queue):
            events.append("ages")
            await asyncio.sleep(0)  # a point where an unlocked second poll could slip in
            return await super().poller_ages(queue)

        async def host_ids(self):
            events.append("hosts")
            return await super().host_ids()

        async def snapshot(self, host):
            events.append("snap")
            await asyncio.sleep(0)
            return await super().snapshot(host)

    f = fleet(Recording(["a", "b"], two_hosts()))

    async def both():
        await asyncio.gather(f.poll_once(), f.poll_once())
    asyncio.run(both())
    assert events == ["ages", "hosts", "snap", "snap", "ages", "hosts", "snap", "snap"]
