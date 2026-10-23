"""Activities that run ON the host. Only the host worker registers these."""
import json
import platform
import re
import shutil
import subprocess
import time
from typing import Optional

import psutil
from temporalio import activity


def _os_pretty_name() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _run(cmd: list, timeout: int = 5) -> Optional[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gpus() -> list:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    gpus = []
    for line in (out or "").splitlines():
        name, mem_total, mem_used, util, temp = [p.strip() for p in line.split(",")]
        gpus.append({
            "name": name,
            "memory_total_mb": int(mem_total),
            "memory_used_mb": int(mem_used),
            "utilization_pct": int(util),
            "temp_c": int(temp),
        })
    return gpus


def _pi_extras() -> dict:
    """Raspberry Pi only: SoC temperature and throttle flags via vcgencmd."""
    if not shutil.which("vcgencmd"):
        return {}
    return {
        "soc_temp": _run(["vcgencmd", "measure_temp"]),        # temp=48.3'C
        "throttled": _run(["vcgencmd", "get_throttled"]),      # throttled=0x0
    }


_MB_PER_UNIT = {
    "b": 1 / 2**20, "kib": 1 / 2**10, "mib": 1, "gib": 2**10, "tib": 2**20,
    "kb": 1e3 / 2**20, "mb": 1e6 / 2**20, "gb": 1e9 / 2**20, "tb": 1e12 / 2**20,
}


def _to_mb(size: str) -> Optional[float]:
    """'12.3MiB' -> 12.3, '1.9GiB' -> 1945.6"""
    m = re.fullmatch(r"([\d.]+)\s*([a-zA-Z]+)", size.strip())
    if not m or m.group(2).lower() not in _MB_PER_UNIT:
        return None
    return round(float(m.group(1)) * _MB_PER_UNIT[m.group(2).lower()], 1)


def _pct(s: str) -> Optional[float]:
    try:
        return float(s.strip().rstrip("%"))
    except ValueError:
        return None


def _health(status: str) -> Optional[str]:
    """Pulls 'healthy' / 'unhealthy' / 'starting' out of 'Up 2 hours (healthy)'."""
    m = re.search(r"\((?:health: )?(healthy|unhealthy|starting)\)", status)
    return m.group(1) if m else None


def _split_image(ref: str) -> tuple:
    """'ghcr.io/foo/bar:1.2@sha256:...' -> ('ghcr.io/foo/bar', '1.2'). Untagged means 'latest'."""
    if re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", ref):
        return None, None  # image was re-pulled or removed; ps shows its ID instead of a name
    has_digest = "@" in ref
    ref = ref.split("@", 1)[0]
    repo, _, last = ref.rpartition("/")
    name, sep, tag = last.partition(":")  # rpartition on "/" first so a registry port isn't taken as a tag
    name = f"{repo}/{name}" if repo else name
    return name, tag if sep else (None if has_digest else "latest")


def _label(labels: str, key: str) -> Optional[str]:
    """Pulls one value out of docker ps's 'k1=v1,k2=v2' label string."""
    for kv in labels.split(","):
        k, sep, v = kv.partition("=")
        if sep and k == key:
            return v
    return None


def _docker_json(args: list, timeout: int) -> list:
    """Runs a docker command with --format json; raises RuntimeError with docker's message."""
    try:
        out = subprocess.run(
            ["docker", *args, "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"docker {args[0]} timed out after {timeout}s")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"docker {args[0]} exited {out.returncode}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def _docker() -> Optional[dict]:
    """None if docker isn't installed; otherwise containers, or an error (e.g. no socket permission)."""
    if not shutil.which("docker"):
        return None
    try:
        ps = _docker_json(["ps", "-a"], timeout=10)
        # Stats only covers running containers and takes a couple of seconds to sample.
        stats = {s["ID"]: s for s in _docker_json(["stats", "--no-stream"], timeout=20)} if any(
            c["State"] == "running" for c in ps
        ) else {}
    except RuntimeError as e:
        return {"containers": [], "error": str(e)}

    containers = []
    for c in ps:
        s = stats.get(c["ID"])
        mem_used, _, mem_limit = (s["MemUsage"].partition("/") if s else ("", "", ""))
        image_name, image_tag = _split_image(c["Image"])
        containers.append({
            "name": c["Names"],
            "image": c["Image"],
            "image_name": image_name,
            "image_tag": image_tag,
            # What the image says it is; more useful than the tag when the tag is "latest".
            "image_version": _label(c["Labels"], "org.opencontainers.image.version"),
            "state": c["State"],            # running, exited, restarting, ...
            "status": c["Status"],          # Up 2 hours (healthy)
            "health": _health(c["Status"]),
            "ports": c["Ports"],
            "cpu_pct": _pct(s["CPUPerc"]) if s else None,
            "mem_used_mb": _to_mb(mem_used) if s else None,
            "mem_limit_mb": _to_mb(mem_limit) if s else None,
            "mem_pct": _pct(s["MemPerc"]) if s else None,
        })
    return {"containers": containers, "error": None}


@activity.defn
def collect_host_info() -> dict:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "hostname": platform.node(),
        "os": _os_pretty_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "cpu_pct": psutil.cpu_percent(interval=1),
        "load_avg": [round(x, 2) for x in psutil.getloadavg()],
        "mem_total_mb": mem.total // 2**20,
        "mem_used_mb": mem.used // 2**20,
        "disk_root_total_gb": round(disk.total / 2**30, 1),
        "disk_root_used_pct": disk.percent,
        "uptime_s": int(time.time() - psutil.boot_time()),
        "gpus": _gpus(),
        "pi": _pi_extras(),
        "docker": _docker(),
    }
