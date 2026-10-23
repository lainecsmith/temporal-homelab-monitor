"""Writes frontend_tests/fixtures.json from the real Fleet.view(), so the JavaScript tests
run against the same payload shapes the API serves. Run from the repo root:

    uv run python -m dashboard.make_fixtures
"""
import asyncio
import json
from pathlib import Path

from dashboard.builders import apt, container, image, info, snap, upd
from dashboard.service import Fleet
from dashboard.store import Store
from dashboard.test_service import FakeGateway

CHECKED = "2026-10-23T12:00:00+00:00"
NOW = "2026-10-23T14:02:15+00:00"


def host(name, os_count, security, containers=(), images=(), status="online", reboot=False, **info_kw):
    pk = [{"name": f"pkg-{i}", "current": "1.0", "available": "1.1", "security": i < security} for i in range(os_count)]
    if os_count:
        pk[0]["name"] = "openssl"
    docker = {"containers": list(containers), "error": None} if containers else None
    return snap(
        name, status=status, inf=info(docker=docker or "absent", **info_kw),
        updates=upd(host=apt(count=os_count, security=security, packages=pk, reboot_required=reboot), images=list(images)),
        updates_checked_at=CHECKED)


def snapshots():
    nas_containers = [
        container("jellyfin", "jellyfin/jellyfin:10.10.3", cpu=34.0),
        container("postgres", "postgres:16.4", cpu=6.4),
        container("nextcloud", "nextcloud:29-apache", cpu=16.8),
        container("redis", "redis:7.4-alpine", cpu=2.4),
        container("dashboard", "homelab/dashboard:dev", health="unhealthy", cpu=0.8),
        container("restic-backup", "restic/restic:latest", state="exited", cpu=None, mem_used_mb=None),
    ]
    nas_images = [
        image("jellyfin/jellyfin:10.10.3", ["jellyfin"], status="up_to_date", newest_tag="10.11.2"),
        image("postgres:16.4", ["postgres"], status="update_available", newest_tag="18.6", newest_tag_same_major="16.15"),
        image("nextcloud:29-apache", ["nextcloud"], status="update_available"),
        image("redis:7.4-alpine", ["redis"], status="up_to_date"),
        image("homelab/dashboard:dev", ["dashboard"], status="unknown", error="image has no registry digest (built locally?)"),
        image("restic/restic:latest", ["restic-backup"], status="up_to_date"),
    ]
    return {
        "media-server": host("media-server", 12, 3, [container("plex", "plexinc/pms-docker:1.41", cpu=40.0)],
                             [image("plexinc/pms-docker:1.41", ["plex"], status="update_available")], reboot=True,
                             cpu_pct=23.0, mem_used_mb=9760, mem_total_mb=16000, disk_root_used_pct=74.0),
        "nas": host("nas", 21, 8, nas_containers, nas_images, cpu_pct=11.0, mem_used_mb=26900,
                    mem_total_mb=32000, disk_root_used_pct=88.0, disk_root_total_gb=458.0),
        "gpu-box": host("gpu-box", 0, 0, [container("ollama", "ollama/ollama:0.3.0", cpu=20.0)],
                        [image("ollama/ollama:0.3.0", ["ollama"], status="update_available")],
                        gpus=[{"name": "RTX 4070", "memory_total_mb": 12282, "memory_used_mb": 1500,
                               "utilization_pct": 12, "temp_c": 51}]),
        "pi-hole": host("pi-hole", 5, 0, [container("pihole", "pihole/pihole:2024.07.0", cpu=1.0)],
                        [image("pihole/pihole:2024.07.0", ["pihole"], status="up_to_date")],
                        os="Raspberry Pi OS (Debian 12)", arch="aarch64",
                        pi={"soc_temp": "temp=52.1'C", "throttled": "throttled=0x0"}),
        "pi-garage": host("pi-garage", 2, 0, status="offline", os="Raspberry Pi OS (Debian 12)", arch="aarch64"),
    }


def build_fleet(ages=(1.0,)):
    snaps = snapshots()
    gw = FakeGateway(list(snaps), snaps, ages=ages)
    fleet = Fleet(gw, Store(), "controller", now=lambda: NOW)
    asyncio.run(fleet.poll_once())
    return fleet, gw


def main():
    fleet, gw = build_fleet()
    ok = fleet.view()
    nas = fleet.host_view("nas")
    for p in nas["pending_updates"]:  # ages depend on the clock; pin them
        p["age_days"] = 41 if p.get("is_security") else 12 if p["kind"].startswith("image") else 7
    gw.ages = []
    asyncio.run(fleet.poll_once())
    down = fleet.view()
    out = Path(__file__).with_name("frontend_tests") / "fixtures.json"
    out.write_text(json.dumps({"fleet_ok": ok, "fleet_controller_down": down, "host_nas": nas}, indent=1, sort_keys=True))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
