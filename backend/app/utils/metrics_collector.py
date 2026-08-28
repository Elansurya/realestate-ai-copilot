"""
backend/app/utils/metrics_collector.py

Host/process-level resource-metric collection utilities for the
Enterprise Monitoring & Health module of the Enterprise Real Estate AI
Copilot CRM.

This module answers "what is the current CPU / memory / disk / process
utilization of the machine this application is running on", which feeds
either:
    - `MonitoringService.execute_health_check()` for the APPLICATION
      component's own self-reported metrics, or
    - the `/monitoring/metrics` API endpoint for a raw, point-in-time
      snapshot independent of any persisted `SystemHealth` row.

It never talks to the database or the repository/service layer -- it is
a pure, side-effect-free (aside from reading `/proc`-style OS counters
via `psutil`) metrics source. All collection is offloaded to a worker
thread (`asyncio.to_thread`) since `psutil` calls are blocking.

Design notes:
    - `psutil` is an optional dependency at import time: if it is not
      installed, every collector function degrades gracefully and
      returns `None` for the fields it cannot compute rather than
      raising, so a missing package never takes down a health-check
      request.
    - All percentage values are already expressed on a 0-100 scale to
      match `SystemHealth`'s `cpu_usage_percent` / `memory_usage_percent`
      / `disk_usage_percent` column semantics directly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

__all__ = [
    "SystemMetricsSnapshot",
    "get_system_metrics",
    "get_process_metrics",
    "get_disk_usage",
    "get_uptime_seconds",
]

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without psutil installed
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


SystemMetricsSnapshot = dict[str, Any]

_DEFAULT_CPU_SAMPLE_INTERVAL_SECONDS = 0.1
_PROCESS_START_TIME = time.time()


# --------------------------------------------------------------------------
# Host-Level System Metrics
# --------------------------------------------------------------------------
def _collect_system_metrics_blocking(
    disk_path: str,
    cpu_sample_interval_seconds: float,
) -> SystemMetricsSnapshot:
    """
    Blocking implementation of host-level metric collection; always
    run via `asyncio.to_thread` from the async entry point below.

    Args:
        disk_path: Filesystem path whose disk usage should be sampled.
        cpu_sample_interval_seconds: Interval `psutil.cpu_percent()`
            blocks for while sampling CPU utilization.

    Returns:
        A `SystemMetricsSnapshot` dict. Any field that could not be
        computed (missing `psutil`, unsupported platform, invalid
        `disk_path`) is set to `None` rather than raising.
    """
    if not _PSUTIL_AVAILABLE:
        return {
            "cpu_usage_percent": None,
            "memory_usage_percent": None,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "disk_usage_percent": None,
            "disk_used_bytes": None,
            "disk_total_bytes": None,
            "load_average": None,
            "psutil_available": False,
        }

    cpu_usage_percent = psutil.cpu_percent(interval=cpu_sample_interval_seconds)

    virtual_memory = psutil.virtual_memory()
    memory_usage_percent = float(virtual_memory.percent)
    memory_used_bytes = int(virtual_memory.used)
    memory_total_bytes = int(virtual_memory.total)

    disk_usage_percent: Optional[float] = None
    disk_used_bytes: Optional[int] = None
    disk_total_bytes: Optional[int] = None
    try:
        disk_usage = psutil.disk_usage(disk_path)
        disk_usage_percent = float(disk_usage.percent)
        disk_used_bytes = int(disk_usage.used)
        disk_total_bytes = int(disk_usage.total)
    except OSError:
        # Invalid or inaccessible disk_path -- leave disk fields as None
        # rather than failing the whole metrics collection.
        pass

    load_average: Optional[tuple[float, float, float]] = None
    getloadavg = getattr(psutil, "getloadavg", None)
    if getloadavg is not None:
        try:
            load_average = tuple(round(value, 2) for value in getloadavg())  # type: ignore[assignment]
        except OSError:
            load_average = None

    return {
        "cpu_usage_percent": round(float(cpu_usage_percent), 2),
        "memory_usage_percent": round(memory_usage_percent, 2),
        "memory_used_bytes": memory_used_bytes,
        "memory_total_bytes": memory_total_bytes,
        "disk_usage_percent": round(disk_usage_percent, 2) if disk_usage_percent is not None else None,
        "disk_used_bytes": disk_used_bytes,
        "disk_total_bytes": disk_total_bytes,
        "load_average": load_average,
        "psutil_available": True,
    }


async def get_system_metrics(
    *,
    disk_path: str = "/",
    cpu_sample_interval_seconds: float = _DEFAULT_CPU_SAMPLE_INTERVAL_SECONDS,
) -> SystemMetricsSnapshot:
    """
    Collects a point-in-time snapshot of host-level CPU, memory, and
    disk utilization, suitable for feeding directly into
    `MonitoringService.execute_health_check()`'s `cpu_usage_percent` /
    `memory_usage_percent` / `disk_usage_percent` parameters for the
    APPLICATION component.

    Args:
        disk_path: Filesystem path whose disk usage should be sampled
            (defaults to the root filesystem).
        cpu_sample_interval_seconds: Blocking sample interval used for
            CPU percentage measurement; higher values are more accurate
            but take proportionally longer.

    Returns:
        A `SystemMetricsSnapshot` dict with host resource utilization.
    """
    return await asyncio.to_thread(
        _collect_system_metrics_blocking, disk_path, cpu_sample_interval_seconds
    )


# --------------------------------------------------------------------------
# Process-Level Metrics
# --------------------------------------------------------------------------
def _collect_process_metrics_blocking() -> SystemMetricsSnapshot:
    """
    Blocking implementation of process-level metric collection for the
    current application process; always run via `asyncio.to_thread`.

    Returns:
        A dict describing the current process's resource footprint.
        Fields are `None` if `psutil` is unavailable.
    """
    if not _PSUTIL_AVAILABLE:
        return {
            "process_cpu_percent": None,
            "process_memory_rss_bytes": None,
            "process_memory_percent": None,
            "open_file_descriptors": None,
            "thread_count": None,
            "psutil_available": False,
        }

    process = psutil.Process()
    with process.oneshot():
        process_cpu_percent = process.cpu_percent(interval=None)
        memory_info = process.memory_info()
        process_memory_percent = process.memory_percent()
        thread_count = process.num_threads()
        try:
            open_file_descriptors: Optional[int] = process.num_fds()
        except AttributeError:
            # num_fds() is POSIX-only; not available on Windows.
            open_file_descriptors = None

    return {
        "process_cpu_percent": round(float(process_cpu_percent), 2),
        "process_memory_rss_bytes": int(memory_info.rss),
        "process_memory_percent": round(float(process_memory_percent), 2),
        "open_file_descriptors": open_file_descriptors,
        "thread_count": int(thread_count),
        "psutil_available": True,
    }


async def get_process_metrics() -> SystemMetricsSnapshot:
    """
    Collects a point-in-time snapshot of the current application
    process's own resource footprint (CPU, RSS memory, thread count,
    open file descriptors).

    Returns:
        A `SystemMetricsSnapshot` dict describing process-level usage.
    """
    return await asyncio.to_thread(_collect_process_metrics_blocking)


# --------------------------------------------------------------------------
# Disk Usage (Targeted)
# --------------------------------------------------------------------------
async def get_disk_usage(path: str = "/") -> SystemMetricsSnapshot:
    """
    Collects disk usage for a single, specific filesystem path -- useful
    for checking a dedicated volume (e.g. a document-storage mount)
    independently of the general host disk metrics.

    Args:
        path: The filesystem path to sample.

    Returns:
        A dict with `disk_usage_percent`, `disk_used_bytes`, and
        `disk_total_bytes`, or all-`None` values if unavailable.
    """
    if not _PSUTIL_AVAILABLE:
        return {"disk_usage_percent": None, "disk_used_bytes": None, "disk_total_bytes": None}

    def _collect() -> SystemMetricsSnapshot:
        try:
            usage = psutil.disk_usage(path)
        except OSError:
            return {"disk_usage_percent": None, "disk_used_bytes": None, "disk_total_bytes": None}
        return {
            "disk_usage_percent": round(float(usage.percent), 2),
            "disk_used_bytes": int(usage.used),
            "disk_total_bytes": int(usage.total),
        }

    return await asyncio.to_thread(_collect)


# --------------------------------------------------------------------------
# Process Uptime
# --------------------------------------------------------------------------
def get_uptime_seconds() -> float:
    """
    Returns the number of seconds since this metrics-collector module
    was first imported, used as a lightweight proxy for "how long has
    this application process been running" when a more precise process
    start time is not otherwise tracked.

    Returns:
        Elapsed seconds as a float.
    """
    return round(time.time() - _PROCESS_START_TIME, 3)