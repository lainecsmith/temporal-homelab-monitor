"""Snapshot builders shared by the tests. Shapes copied from what the activities and workflow emit."""

# ---- builders: shapes copied from what the activities and workflow actually emit

def container(name="web", image="nginx:1.27", state="running", cpu=50.0, **kw):
    return {"name": name, "image": image, "image_name": image.split(":")[0], "image_tag": image.split(":")[-1],
            "image_version": None, "state": state, "status": "Up 2 hours", "health": None, "ports": "",
            "cpu_pct": cpu, "mem_used_mb": 100.0, "mem_limit_mb": 1000.0, "mem_pct": 10.0, **kw}


def info(docker="absent", **kw):
    base = {"hostname": "h", "os": "Ubuntu 24.04", "kernel": "6.8", "arch": "x86_64", "cpu_cores": 4,
            "cpu_pct": 10.0, "load_avg": [0.1, 0.1, 0.1], "mem_total_mb": 8000, "mem_used_mb": 2000,
            "disk_root_total_gb": 100.0, "disk_root_used_pct": 40.0, "uptime_s": 1000, "gpus": [], "pi": {}}
    if docker != "absent":
        base["docker"] = docker
    else:
        base["docker"] = None
    return {**base, **kw}


def snap(host="h", status="online", inf="default", updates=None, **kw):
    return {"host": host, "status": status, "info": info() if inf == "default" else inf,
            "last_seen": "2026-10-23T14:02:11+00:00", "last_error": None, "updates": updates,
            "updates_checked_at": None, "updates_error": None, **kw}


def apt(count=3, security=1, packages=None, **kw):
    pk = packages if packages is not None else [
        {"name": f"p{i}", "current": "1", "available": "2",
          "security": None if security is None else i < security} for i in range(count)]
    return {"manager": "apt", "count": count, "security_count": security, "reboot_required": False,
            "index_age_h": 3.0, "packages": pk, "error": None, **kw}


def image(img="nginx:1.27", containers=("web",), status="up_to_date", **kw):
    return {"image": img, "status": status, "local_digest": "sha256:a", "remote_digest": "sha256:b",
            "error": None, "newest_tag": None, "newest_tag_same_major": None, "tags_error": None,
            "containers": list(containers), **kw}


def upd(host=None, images=None, docker="default"):
    d = {"images": images or [], "error": None} if docker == "default" else docker
    return {"host": host, "docker": d}
