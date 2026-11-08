"""Raspberry Pi / host health: temperature, memory, disk, database size, load and uptime."""
import os
import shutil
import time
from pathlib import Path

from . import config

DISK_WARN_FREE_GB = 1.0
DISK_WARN_FREE_PCT = 10
TEMP_WARN_C = 75
TEMP_BAD_C = 80
MEM_WARN_MB = 100
STARTED = time.time()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _meminfo() -> dict:
    out = {}
    text = _read("/proc/meminfo") or ""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) / 1024  # MB
    return out


def _rss_mb() -> float | None:
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return None


def snapshot() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    mem = _meminfo()
    disk = shutil.disk_usage(config.DATA_DIR)
    db_files = [config.DB_PATH, Path(str(config.DB_PATH) + "-wal")]
    db_mb = sum(f.stat().st_size for f in db_files if f.exists()) / 1e6
    uptime = _read("/proc/uptime")
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    model = (_read("/proc/device-tree/model") or "").rstrip("\x00") or None
    h = {
        "model": model,
        "temp_c": int(temp) / 1000 if temp and temp.lstrip("-").isdigit() else None,
        "mem_total_mb": mem.get("MemTotal"),
        "mem_available_mb": mem.get("MemAvailable"),
        "swap_used_mb": (mem["SwapTotal"] - mem["SwapFree"]) if "SwapTotal" in mem else None,
        "app_rss_mb": _rss_mb(),
        "disk_total_gb": disk.total / 1e9,
        "disk_free_gb": disk.free / 1e9,
        "disk_free_pct": disk.free / disk.total * 100 if disk.total else None,
        "db_mb": db_mb,
        "load": load,
        "cpus": os.cpu_count(),
        "uptime_s": float(uptime.split()[0]) if uptime else None,
        "app_uptime_s": time.time() - STARTED,
    }
    h["warnings"] = warnings(h)
    return h


def warnings(h: dict) -> list[tuple[str, str]]:
    """(level, message) pairs; level is 'bad' or 'warn'."""
    out = []
    if h["disk_free_gb"] < DISK_WARN_FREE_GB or (h["disk_free_pct"] or 100) < DISK_WARN_FREE_PCT:
        out.append(("bad", f"SD card nearly full: {h['disk_free_gb']:.1f} GB free"))
    if h["temp_c"] is not None:
        if h["temp_c"] >= TEMP_BAD_C:
            out.append(("bad", f"Pi is overheating ({h['temp_c']:.0f}°C) and slowing itself down"))
        elif h["temp_c"] >= TEMP_WARN_C:
            out.append(("warn", f"Pi is running hot ({h['temp_c']:.0f}°C)"))
    if h["mem_available_mb"] is not None and h["mem_available_mb"] < MEM_WARN_MB:
        out.append(("warn", f"Low memory: {h['mem_available_mb']:.0f} MB available"))
    return out
