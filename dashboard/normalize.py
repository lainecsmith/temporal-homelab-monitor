"""Turns a raw HostWorkflow snapshot into explicit UI states.

The snapshot uses null for several different things: never collected, never checked, not
applicable to this host, or not reported by this package manager. The frontend should never
have to guess which, and must never render missing data as zero or healthy, so this is the
one place that decides. Pure functions with no Temporal imports, so they're easy to test.

    normalize(snapshot)      -> one host, with explicit states per area
    fleet_summary(hosts)     -> totals across normalized hosts, with how many hosts they cover
"""
from typing import Optional

INDEX_STALE_HOURS = 48  # apt reads a cached index; past this, "0 updates" can't be trusted

_CONTAINER_FIELDS = (
    "name", "image", "image_name", "image_tag", "image_version", "state", "status",
    "health", "ports", "cpu_pct", "mem_used_mb", "mem_limit_mb", "mem_pct",
)


def _pct(used, total) -> Optional[float]:
    if used is None or not total:
        return None
    return round(used / total * 100, 1)


def _metrics(info: Optional[dict]) -> Optional[dict]:
    if info is None:
        return None
    return {
        "os": info.get("os"),
        "kernel": info.get("kernel"),
        "arch": info.get("arch"),
        "cpu_cores": info.get("cpu_cores"),
        "cpu_pct": info.get("cpu_pct"),
        "load_avg": info.get("load_avg"),
        "mem_total_mb": info.get("mem_total_mb"),
        "mem_used_mb": info.get("mem_used_mb"),
        "mem_pct": _pct(info.get("mem_used_mb"), info.get("mem_total_mb")),
        "disk_total_gb": info.get("disk_root_total_gb"),
        "disk_used_pct": info.get("disk_root_used_pct"),
        "uptime_s": info.get("uptime_s"),
        "gpus": info.get("gpus") or [],
        "pi": info.get("pi") or None,
    }


# ---------------------------------------------------------------- docker

def _docker_base(info: Optional[dict]) -> dict:
    """state: unknown (no data yet) | absent | error | empty | ok"""
    base = {"state": "unknown", "error": None, "hint": None, "raw": []}
    if info is None:
        return base
    raw = info.get("docker")
    if raw is None:
        return {**base, "state": "absent"}
    error = raw.get("error")
    containers = raw.get("containers") or []
    if error:
        hint = "docker_permission" if "permission denied" in error.lower() else None
        return {**base, "state": "error", "error": error, "hint": hint}
    return {**base, "state": "ok" if containers else "empty", "raw": containers}


# ---------------------------------------------------------------- updates

def _image_item(entry: dict) -> dict:
    """One checked image. 'unknown' means we couldn't tell, never 'up to date'."""
    status = entry.get("status")
    new_build = True if status == "update_available" else False if status == "up_to_date" else None
    newest = entry.get("newest_tag")
    same_major = entry.get("newest_tag_same_major")
    if new_build or newest:
        state = "update_available"
    elif status == "up_to_date":
        state = "up_to_date"
    else:
        state = "unknown"
    return {
        "state": state,
        "new_build": new_build,
        "newest_tag": newest,
        "newest_tag_same_major": same_major,
        "tags_error": entry.get("tags_error"),
        "reason": entry.get("error") if state == "unknown" else None,
    }


def _no_image_update(state: str, reason: str) -> dict:
    return {"state": state, "new_build": None, "newest_tag": None,
            "newest_tag_same_major": None, "tags_error": None, "reason": reason}


def _images(updates: Optional[dict], docker_state: str) -> dict:
    """state: not_applicable | not_checked | error | ok"""
    out = {"state": "not_checked", "error": None, "updates": None, "items": []}
    if docker_state == "absent":
        return {**out, "state": "not_applicable"}
    if updates is None:
        return out
    raw = updates.get("docker")
    if raw is None:
        return {**out, "state": "not_applicable"}
    if raw.get("error"):
        return {**out, "state": "error", "error": raw["error"]}
    items = [
        {"image": e.get("image"), "containers": e.get("containers") or [], **_image_item(e)}
        for e in raw.get("images") or []
    ]
    return {**out, "state": "ok", "items": items,
            "updates": sum(1 for i in items if i["state"] == "update_available")}


def _os_block(state: str, **fields) -> dict:
    base = {
        "state": state, "error": None, "manager": None, "count": None, "security_count": None,
        "reboot_required": None, "index_age_h": None, "index_stale": None, "zero_is_stale": False,
        "packages": [], "hidden_count": 0,
    }
    return {**base, **fields}


def _os(updates: Optional[dict], stale_hours: float) -> dict:
    """state: not_checked | untracked | error | ok"""
    if updates is None:
        return _os_block("not_checked")
    host = updates.get("host")
    if host is None:
        return _os_block("untracked")
    if host.get("error"):
        return _os_block("error", error=host["error"], manager=host.get("manager"))
    count = host.get("count")
    packages = host.get("packages") or []
    age = host.get("index_age_h")
    index_stale = None if age is None else age > stale_hours
    return _os_block(
        "ok",
        manager=host.get("manager"),
        count=count,
        # null from dnf, pacman and softwareupdate means "not reported", not zero
        security_count=host.get("security_count"),
        reboot_required=host.get("reboot_required"),
        index_age_h=age,
        index_stale=index_stale,
        zero_is_stale=count == 0 and index_stale is True,
        packages=packages,
        hidden_count=max(0, (count or 0) - len(packages)),
    )


def _updates_state(snap: dict) -> dict:
    """state: never_checked | ok | last_check_failed

    The workflow sets updates_checked_at even when a check fails (so a broken check isn't
    retried every cycle), and keeps the previous results. So the timestamp is only a
    freshness claim when the last attempt succeeded."""
    updates = snap.get("updates")
    error = snap.get("updates_error")
    attempt = snap.get("updates_checked_at")
    if error is not None:
        state = "last_check_failed"
    elif updates is None:
        state = "never_checked"
    else:
        state = "ok"
    return {
        "state": state,
        "checked_at": attempt if state == "ok" else None,
        "last_attempt_at": attempt,
        "error": error,
        "data_is_stale": error is not None and updates is not None,
    }


# ---------------------------------------------------------------- one host

def _container(c: dict, cores: Optional[int], images: dict) -> dict:
    out = {k: c.get(k) for k in _CONTAINER_FIELDS}
    cpu = c.get("cpu_pct")
    # docker stats reports per core, so it can exceed 100; this is the share of the whole host
    out["cpu_pct_of_host"] = round(cpu / cores, 1) if cpu is not None and cores else None

    if images["state"] == "not_checked":
        out["update"] = _no_image_update("not_checked", "Not checked yet")
    elif images["state"] == "error":
        out["update"] = _no_image_update("unknown", images["error"])
    elif images["state"] == "not_applicable":
        out["update"] = _no_image_update("unknown", "Image checks aren't available on this host")
    else:
        match = next((i for i in images["items"] if c.get("name") in i["containers"]), None)
        if match is None:
            out["update"] = _no_image_update("not_checked", "Added since the last check")
        else:
            out["update"] = {k: match[k] for k in (
                "state", "new_build", "newest_tag", "newest_tag_same_major", "tags_error", "reason")}
    return out


def normalize(snapshot: dict, index_stale_hours: float = INDEX_STALE_HOURS) -> dict:
    """Tolerates older snapshot shapes: every field is read with .get()."""
    info = snapshot.get("info")
    status = snapshot.get("status")
    state = "online" if status == "online" else "offline" if status == "offline" else "collecting"

    metrics = _metrics(info)
    cores = metrics["cpu_cores"] if metrics else None
    docker = _docker_base(info)
    updates_raw = snapshot.get("updates")

    os_block = _os(updates_raw, index_stale_hours)
    images = _images(updates_raw, docker["state"])
    containers = [_container(c, cores, images) for c in docker.pop("raw")]
    docker = {**docker, "containers": containers, "total": len(containers),
              "running": sum(1 for c in containers if c["state"] == "running")}

    os_ok = os_block["state"] == "ok"
    has_docker = None if info is None else docker["state"] != "absent"
    return {
        "host": snapshot.get("host"),
        "state": state,
        "has_data": info is not None,
        "last_seen": snapshot.get("last_seen"),
        "last_error": snapshot.get("last_error"),
        "metrics": metrics,
        "docker": docker,
        "updates": {**_updates_state(snapshot), "os": os_block, "images": images},
        "summary": {
            "os_updates": os_block["count"] if os_ok else None,
            "security_updates": os_block["security_count"] if os_ok else None,
            "reboot_required": os_block["reboot_required"] if os_ok else None,
            "image_updates": images["updates"] if images["state"] == "ok" else None,
            "os_applicable": os_block["state"] != "untracked",
            "has_docker": has_docker,  # None = don't know yet
        },
    }


# ---------------------------------------------------------------- fleet

def fleet_summary(hosts: list) -> dict:
    """Totals over normalized hosts. Each total says how many hosts it covers, so a host
    with no data yet can't drag a total down and read as good news."""

    def total(key: str, applicable) -> dict:
        values = [h["summary"][key] for h in hosts if h["summary"][key] is not None]
        return {"total": sum(values), "covered": len(values),
                "of": sum(1 for h in hosts if applicable(h))}

    os_applicable = lambda h: h["summary"]["os_applicable"]
    docker_applicable = lambda h: h["summary"]["has_docker"] is not False
    reboot = [h["summary"]["reboot_required"] for h in hosts if h["summary"]["reboot_required"] is not None]

    return {
        "hosts_total": len(hosts),
        "hosts_online": sum(1 for h in hosts if h["state"] == "online"),
        "hosts_offline": sum(1 for h in hosts if h["state"] == "offline"),
        "hosts_collecting": sum(1 for h in hosts if h["state"] == "collecting"),
        "os_updates": total("os_updates", os_applicable),
        "security_updates": total("security_updates", os_applicable),
        "image_updates": total("image_updates", docker_applicable),
        "reboot_required": {
            "hosts": [h["host"] for h in hosts if h["summary"]["reboot_required"]],
            "covered": len(reboot),
            "of": sum(1 for h in hosts if os_applicable(h)),
        },
        "failed_check_hosts": [h["host"] for h in hosts if h["updates"]["state"] == "last_check_failed"],
    }
