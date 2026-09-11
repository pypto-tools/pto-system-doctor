from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from modules.resource.collectors import HostSnapshot, TaskUsage
from modules.resource.config import Settings


SEVERITY = {"healthy": 0, "prewarn": 1, "warn": 2, "pause": 3, "critical": 4}


@dataclass(frozen=True)
class Signal:
    resource: str
    level: str
    reason: str
    kill_eligible: bool = False
    victim_metric: str = "memory"


@dataclass(frozen=True)
class Decision:
    level: str
    signals: tuple[Signal, ...]
    should_pause: bool
    should_kill: bool
    victim_metric: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "signals": [signal.__dict__ for signal in self.signals],
            "should_pause": self.should_pause,
            "should_kill": self.should_kill,
            "victim_metric": self.victim_metric,
        }


def evaluate(
    snapshot: HostSnapshot, settings: Settings, *, io_sustained: bool
) -> Decision:
    signals: list[Signal] = []
    available = snapshot.meminfo["MemAvailable"]
    memory_emergency = available <= settings.emergency_available
    if memory_emergency:
        signals.append(
            Signal(
                "memory",
                "critical",
                "可用内存已降至紧急水位",
                True,
                "memory",
            )
        )
    elif available <= settings.kill_available:
        signals.append(
            Signal("memory", "critical", "可用内存已降至处置水位", True, "memory")
        )
    elif available <= settings.warn_available:
        signals.append(Signal("memory", "warn", "可用内存已低于告警水位"))

    reclaim = snapshot.vmstat_rates.get("pgscan_kswapd", 0)
    direct = snapshot.vmstat_rates.get("pgscan_direct", 0)
    if direct >= settings.vmstat_direct_rate:
        signals.append(Signal("reclaim", "pause", "直接内存回收速率持续偏高"))
    elif reclaim >= settings.vmstat_prewarn_rate:
        signals.append(Signal("reclaim", "prewarn", "kswapd 内存扫描速率持续偏高"))

    fd_ratio_hit = (
        snapshot.fd.ratio is not None and snapshot.fd.ratio >= settings.fd_max_ratio
    )
    if snapshot.fd.used >= settings.fd_critical_absolute:
        signals.append(
            Signal(
                "fd",
                "critical",
                "系统文件句柄数已达到严重水位",
                True,
                "fd",
            )
        )
    elif snapshot.fd.used >= settings.fd_pause_absolute or fd_ratio_hit:
        signals.append(Signal("fd", "pause", "系统文件句柄数已达到暂停水位"))
    elif snapshot.fd.used >= settings.fd_warn_absolute:
        signals.append(Signal("fd", "warn", "系统文件句柄数已达到告警水位"))

    thread_ratio = max(
        snapshot.threads.ratio or 0.0, snapshot.threads.cgroup_ratio or 0.0
    )
    if thread_ratio >= settings.thread_critical_ratio:
        signals.append(
            Signal(
                "threads",
                "critical",
                "系统线程/PID 容量已达到严重水位",
                True,
                "processes",
            )
        )
    elif thread_ratio >= settings.thread_pause_ratio:
        signals.append(Signal("threads", "pause", "系统线程/PID 容量已达到暂停水位"))
    elif snapshot.threads.current >= settings.thread_warn_absolute:
        signals.append(Signal("threads", "warn", "系统线程数已达到告警水位"))

    if io_sustained:
        signals.append(Signal("io", "pause", "磁盘延迟或利用率持续偏高"))

    level = max(
        (signal.level for signal in signals), key=SEVERITY.get, default="healthy"
    )
    kill_signals = [signal for signal in signals if signal.kill_eligible]
    if memory_emergency:
        victim_metric = "memory"
    else:
        victim_metric = (
            max(kill_signals, key=lambda item: SEVERITY[item.level]).victim_metric
            if kill_signals
            else "memory"
        )
    return Decision(
        level=level,
        signals=tuple(signals),
        should_pause=SEVERITY[level]
        >= SEVERITY[settings.maintenance_min_level],
        should_kill=bool(kill_signals),
        victim_metric=victim_metric,
    )


def io_is_high(snapshot: HostSnapshot, settings: Settings) -> bool:
    return any(
        metric.util_pct >= settings.io_pause_util_pct
        or metric.await_ms >= settings.io_pause_await_ms
        for metric in snapshot.io
    )


def annotate_protection(
    tasks: list[TaskUsage],
    leases: dict[str, dict[str, Any]],
    settings: Settings,
    available: int,
) -> None:
    for task in tasks:
        task.protected_reason = ""
        task.leased = False
        task.lease_floor = None
        if (
            task.user in settings.protected_users
            and available > settings.emergency_available
        ):
            task.protected_reason = "protected user above emergency floor"
            continue
        lease = leases.get(task.task_id)
        if not lease:
            continue
        floor = int(lease.get("floor", settings.emergency_available))
        task.leased = True
        task.lease_floor = floor
        if available > floor:
            task.protected_reason = "active lease above safety floor"


def select_victim(
    tasks: list[TaskUsage], settings: Settings, *, metric: str, emergency: bool
) -> TaskUsage | None:
    candidates = [task for task in tasks if not task.protected_reason]
    if metric == "memory":
        if not emergency:
            candidates = [
                task
                for task in candidates
                if task.kill_memory >= settings.min_victim_anon_memory
            ]
        return max(
            candidates,
            key=lambda task: (task.kill_memory, task.pss, task.task_id),
            default=None,
        )
    if metric == "fd":
        candidates = [
            task for task in candidates if task.fd_count >= settings.min_victim_fd
        ]
        return max(
            candidates,
            key=lambda task: (task.fd_count, task.process_count, task.task_id),
            default=None,
        )
    if metric == "processes":
        candidates = [
            task
            for task in candidates
            if task.process_count >= settings.min_victim_processes
        ]
        return max(
            candidates,
            key=lambda task: (task.process_count, task.fd_count, task.task_id),
            default=None,
        )
    return None
