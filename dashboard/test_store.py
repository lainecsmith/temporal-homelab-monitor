from dashboard.builders import apt, container, image, info, snap, upd
from dashboard.store import Store

T1, T2, T3 = ("2026-10-01T06:00:00+00:00", "2026-10-02T06:00:00+00:00", "2026-10-03T06:00:00+00:00")


def snapshot(at, packages=("a", "b"), security=("a",), count=None, images=None, error=None, available="2"):
    pk = [{"name": n, "current": "1", "available": available, "security": n in security} for n in packages]
    host = apt(count=len(pk) if count is None else count, security=len(security), packages=pk)
    d = {"containers": [container("web")], "error": None}
    return snap("h", inf=info(docker=d), updates=upd(host=host, images=images or []),
                updates_checked_at=at, updates_error=error)


def rows(store, host="h"):
    return {(r["kind"], r["name"], r["first_seen_at"]): dict(r) for r in store.db.execute(
        "SELECT * FROM pending_update WHERE host = ?", (host,))}


def open_names(store, kind="os_package"):
    return sorted(r["name"] for r in store.db.execute(
        "SELECT name FROM pending_update WHERE host='h' AND kind=? AND resolved_at IS NULL", (kind,)))


def test_first_check_records_open_items_with_the_checks_timestamp():
    s = Store(); s.save("h", snapshot(T1), "now")
    r = rows(s)
    assert set(r) == {("os_package", "a", T1), ("os_package", "b", T1)}
    assert r[("os_package", "a", T1)]["is_security"] == 1 and r[("os_package", "b", T1)]["is_security"] == 0


def test_the_same_check_is_only_processed_once():
    s = Store(); s.save("h", snapshot(T1), "now")
    s.save("h", snapshot(T1, packages=("a",)), "later")  # same updates_checked_at: a 30 s poll, not a new check
    assert open_names(s) == ["a", "b"]


def test_item_missing_from_a_later_check_is_resolved_at_that_check():
    s = Store(); s.save("h", snapshot(T1), "now"); s.save("h", snapshot(T2, packages=("a",)), "now")
    r = rows(s)
    assert r[("os_package", "b", T1)]["resolved_at"] == T2
    a = r[("os_package", "a", T1)]
    assert a["resolved_at"] is None and a["last_seen_at"] == T2


def test_an_item_that_returns_gets_a_new_clock():
    s = Store()
    s.save("h", snapshot(T1, packages=("a",)), "n"); s.save("h", snapshot(T2, packages=()), "n")
    s.save("h", snapshot(T3, packages=("a",)), "n")
    r = rows(s)
    assert r[("os_package", "a", T1)]["resolved_at"] == T2
    assert r[("os_package", "a", T3)]["resolved_at"] is None


def test_a_failed_check_resolves_nothing():
    s = Store(); s.save("h", snapshot(T1), "n")
    s.save("h", snapshot(T2, packages=(), error="timeout"), "n")
    assert open_names(s) == ["a", "b"]


def test_hosts_with_no_check_or_no_package_manager_create_nothing():
    s = Store()
    s.save("h", snap("h", updates=None), "n")
    s.save("h", snap("h", updates=upd(host=None), updates_checked_at=T1), "n")
    assert rows(s) == {}


def test_a_truncated_os_list_cannot_resolve_packages():
    s = Store()
    s.save("h", snapshot(T1, packages=("p0", "p1"), security=(), count=150), "n")
    s.save("h", snapshot(T2, packages=("p1", "p2"), security=(), count=150), "n")  # p0 may just be past the cap
    assert open_names(s) == ["p0", "p1", "p2"]


def test_a_newer_available_version_does_not_reset_the_age():
    s = Store(); s.save("h", snapshot(T1, available="2"), "n"); s.save("h", snapshot(T2, available="3"), "n")
    a = rows(s)[("os_package", "a", T1)]
    assert a["first_seen_at"] == T1 and a["available_version"] == "3"


def test_image_updates_are_tracked_and_resolved():
    s = Store()
    pending = image(status="update_available", newest_tag="1.28")
    s.save("h", snapshot(T1, images=[pending]), "n")
    r = rows(s)
    assert ("image_build", "nginx:1.27", T1) in r and ("image_tag", "nginx:1.27", T1) in r
    assert r[("image_tag", "nginx:1.27", T1)]["available_version"] == "1.28"
    s.save("h", snapshot(T2, images=[image(status="up_to_date")]), "n")
    assert all(v["resolved_at"] == T2 for k, v in rows(s).items() if k[0].startswith("image_"))


def test_image_resolution_still_works_when_the_os_list_is_truncated():
    s = Store()
    s.save("h", snapshot(T1, count=150, images=[image(status="update_available")]), "n")
    s.save("h", snapshot(T2, count=150, images=[image(status="up_to_date")]), "n")
    assert rows(s)[("image_build", "nginx:1.27", T1)]["resolved_at"] == T2


def test_snapshot_survives_and_summary_columns_are_filled():
    s = Store(); s.save("h", snapshot(T1), "2026-10-01T06:00:05+00:00")
    host, fetched_at, snap_ = s.rows()[0]
    assert (host, fetched_at, snap_["updates_checked_at"]) == ("h", "2026-10-01T06:00:05+00:00", T1)
    c = dict(s.db.execute("SELECT * FROM host_snapshot").fetchone())
    assert (c["os_updates"], c["security_updates"], c["reboot_required"]) == (2, 1, 0)


def test_oldest_ages_split_security_from_the_rest():
    s = Store()
    old = "2020-01-01T00:00:00+00:00"
    s.save("h", snapshot(old, packages=("a", "b"), security=("b",)), "n")
    ages = s.oldest_ages()["h"]
    assert ages["oldest_update_days"] > 1000 and ages["oldest_security_update_days"] > 1000
    assert len(s.pending_updates("h")) == 2
