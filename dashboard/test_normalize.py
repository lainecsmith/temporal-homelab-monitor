from dashboard.builders import apt, container, image, info, snap, upd
from dashboard.normalize import fleet_summary, normalize


# ---- one host: each row of the empty-state table

def test_new_entity_is_collecting():
    n = normalize({"host": "h", "status": "unknown", "info": None, "last_seen": None, "last_error": None,
                   "updates": None, "updates_checked_at": None, "updates_error": None})
    assert n["state"] == "collecting" and not n["has_data"] and n["metrics"] is None
    assert n["docker"]["state"] == "unknown"


def test_offline_never_collected_keeps_the_error():
    n = normalize(snap(status="offline", inf=None, last_error="schedule to start timeout"))
    assert n["state"] == "offline" and not n["has_data"]
    assert n["last_error"] == "schedule to start timeout"


def test_offline_keeps_last_known_data():
    n = normalize(snap(status="offline"))
    assert n["state"] == "offline" and n["has_data"] and n["metrics"]["mem_pct"] == 25.0


def test_docker_absent_is_not_the_same_as_empty():
    assert normalize(snap())["docker"]["state"] == "absent"
    assert normalize(snap(inf=info(docker={"containers": [], "error": None})))["docker"]["state"] == "empty"


def test_docker_unreadable_hints_at_the_docker_group():
    n = normalize(snap(inf=info(docker={"containers": [], "error": "permission denied while trying to connect"})))
    assert n["docker"]["state"] == "error" and n["docker"]["hint"] == "docker_permission"


def test_docker_counts():
    d = {"containers": [container("a"), container("b", state="exited")], "error": None}
    n = normalize(snap(inf=info(docker=d)))
    assert n["docker"]["state"] == "ok" and (n["docker"]["running"], n["docker"]["total"]) == (1, 2)


def test_never_checked_updates_are_not_zero():
    d = {"containers": [container()], "error": None}
    n = normalize(snap(inf=info(docker=d)))
    assert n["updates"]["state"] == "never_checked" and n["updates"]["os"]["state"] == "not_checked"
    assert n["docker"]["containers"][0]["update"]["state"] == "not_checked"
    assert n["summary"]["os_updates"] is None and n["summary"]["image_updates"] is None


def test_no_package_manager_is_untracked_not_zero():
    n = normalize(snap(updates=upd(host=None), updates_checked_at="t"))
    assert n["updates"]["os"]["state"] == "untracked"
    assert n["summary"]["os_updates"] is None and n["summary"]["os_applicable"] is False


def test_os_check_error():
    n = normalize(snap(updates=upd(host=apt(count=0, error="apt exited 100")), updates_checked_at="t"))
    assert n["updates"]["os"]["state"] == "error" and n["summary"]["os_updates"] is None


def test_image_that_cant_be_checked_is_never_up_to_date():
    d = {"containers": [container()], "error": None}
    u = upd(images=[image(status="unknown", error="registry requires login (private image?)")])
    c = normalize(snap(inf=info(docker=d), updates=u, updates_checked_at="t"))["docker"]["containers"][0]["update"]
    assert c["state"] == "unknown" and "login" in c["reason"] and c["new_build"] is None


def test_security_and_reboot_unknown_stay_null_not_zero_or_false():
    host = apt(count=7, security=None, reboot_required=None)
    n = normalize(snap(updates=upd(host=host), updates_checked_at="t"))
    assert n["summary"]["os_updates"] == 7
    assert n["summary"]["security_updates"] is None and n["summary"]["reboot_required"] is None


def test_container_added_since_last_check():
    d = {"containers": [container("web"), container("new", image="redis:7")], "error": None}
    u = upd(images=[image()])
    cs = normalize(snap(inf=info(docker=d), updates=u, updates_checked_at="t"))["docker"]["containers"]
    assert cs[0]["update"]["state"] == "up_to_date"
    assert cs[1]["update"]["state"] == "not_checked" and "Added" in cs[1]["update"]["reason"]


def test_new_build_and_new_version_are_reported_separately():
    d = {"containers": [container("pg", image="postgres:16.4"), container("web")], "error": None}
    u = upd(images=[
        image("postgres:16.4", ["pg"], status="update_available", newest_tag="18.6", newest_tag_same_major="16.15"),
        image("nginx:1.27", ["web"], status="up_to_date", newest_tag="1.28"),
    ])
    cs = normalize(snap(inf=info(docker=d), updates=u, updates_checked_at="t"))["docker"]["containers"]
    assert cs[0]["update"]["state"] == "update_available" and cs[0]["update"]["new_build"] is True
    assert cs[0]["update"]["newest_tag_same_major"] == "16.15" and cs[0]["update"]["newest_tag"] == "18.6"
    assert cs[1]["update"]["state"] == "update_available" and cs[1]["update"]["new_build"] is False


def test_docker_update_check_error_marks_containers_unknown():
    d = {"containers": [container()], "error": None}
    n = normalize(snap(inf=info(docker=d), updates=upd(docker={"images": [], "error": "cannot connect"}),
                       updates_checked_at="t"))
    assert n["updates"]["images"]["state"] == "error"
    assert n["docker"]["containers"][0]["update"]["state"] == "unknown"


def test_container_cpu_is_also_given_as_share_of_host():
    d = {"containers": [container(cpu=200.0)], "error": None}  # two full cores on a 4 core host
    c = normalize(snap(inf=info(docker=d)))["docker"]["containers"][0]
    assert c["cpu_pct"] == 200.0 and c["cpu_pct_of_host"] == 50.0


# ---- the three subtler cases

def test_failed_check_does_not_claim_freshness():
    n = normalize(snap(updates=upd(host=apt()), updates_checked_at="2026-10-23T12:00:00+00:00",
                       updates_error="timeout"))
    u = n["updates"]
    assert u["state"] == "last_check_failed" and u["checked_at"] is None
    assert u["last_attempt_at"] == "2026-10-23T12:00:00+00:00" and u["data_is_stale"] is True
    assert n["summary"]["os_updates"] == 3  # the previous good results are still there


def test_first_check_failing_has_no_data_and_is_not_stale():
    u = normalize(snap(updates=None, updates_checked_at="t", updates_error="timeout"))["updates"]
    assert u["state"] == "last_check_failed" and u["data_is_stale"] is False


def test_zero_updates_with_an_old_index_is_flagged():
    def zero(age):
        h = apt(count=0, security=0, index_age_h=age)
        return normalize(snap(updates=upd(host=h), updates_checked_at="t"))["updates"]["os"]
    assert zero(200.0)["zero_is_stale"] is True
    assert zero(3.0)["zero_is_stale"] is False
    assert zero(None)["zero_is_stale"] is False and zero(None)["index_stale"] is None


def test_truncated_package_list_reports_the_hidden_count():
    h = apt(count=150, security=20, packages=[{"name": f"p{i}", "current": "1", "available": "2",
                                                "security": True} for i in range(100)])
    os_ = normalize(snap(updates=upd(host=h), updates_checked_at="t"))["updates"]["os"]
    assert os_["count"] == 150 and len(os_["packages"]) == 100 and os_["hidden_count"] == 50


def test_old_snapshot_shape_is_tolerated():
    n = normalize({"host": "h", "status": "online", "info": {"os": "x"}, "last_seen": "t", "last_error": None})
    assert n["state"] == "online" and n["docker"]["state"] == "absent"
    assert n["updates"]["state"] == "never_checked"


# ---- fleet totals say what they cover

def test_fleet_totals_report_coverage():
    d = {"containers": [container()], "error": None}
    a = normalize(snap("a", inf=info(docker=d), updates_checked_at="t", updates=upd(
        host=apt(count=12, security=3, reboot_required=True),
        images=[image(status="update_available"), ])))
    b = normalize(snap("b", updates=upd(host=apt(count=5, security=None, reboot_required=None)),
                       updates_checked_at="t"))                       # dnf-style, no docker
    c = normalize({"host": "c", "status": "unknown", "info": None, "updates": None})  # just registered
    dd = normalize(snap("d", status="offline", inf=None))                              # never collected
    e = normalize(snap("e", inf=info(docker=d)))                                       # online, never checked
    f = fleet_summary([a, b, c, dd, e])

    assert (f["hosts_total"], f["hosts_online"], f["hosts_offline"], f["hosts_collecting"]) == (5, 3, 1, 1)
    assert f["os_updates"] == {"total": 17, "covered": 2, "of": 5}
    assert f["security_updates"] == {"total": 3, "covered": 1, "of": 5}
    # b has no docker (not applicable); c and d are unknown so they still count toward "of"
    assert f["image_updates"] == {"total": 1, "covered": 1, "of": 4}
    assert f["reboot_required"] == {"hosts": ["a"], "covered": 1, "of": 5}


def test_fleet_lists_hosts_whose_last_check_failed():
    ok = normalize(snap("ok", updates=upd(host=apt()), updates_checked_at="t"))
    bad = normalize(snap("bad", updates=upd(host=apt()), updates_checked_at="t", updates_error="x"))
    assert fleet_summary([ok, bad])["failed_check_hosts"] == ["bad"]


def test_untracked_hosts_are_excluded_from_os_coverage():
    t = normalize(snap("t", updates=upd(host=None), updates_checked_at="t"))
    ok = normalize(snap("ok", updates=upd(host=apt(count=4)), updates_checked_at="t"))
    assert fleet_summary([t, ok])["os_updates"] == {"total": 4, "covered": 1, "of": 1}
