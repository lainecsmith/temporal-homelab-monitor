"""Activities that run ON the host. Only the host worker registers these."""
import platform
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
    }
