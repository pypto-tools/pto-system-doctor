from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from modules.alert.queue import enqueue
from modules.common import (
    DoctorError,
    RuntimePaths,
    append_jsonl,
    atomic_write_json,
    read_json,
)
from modules.resource.collectors import (
    HostSnapshot,
    TaskUsage,
    attribute_tasks,
    collect_host,
    collect_self_usage,
    read_running_tasks,
    sample_state,
)
from modules.resource.config import Settings
from modules.resource.exemptions import active_override, load_leases
from modules.resource.policy import (
    Decision,
    annotate_protection,
    evaluate,
    io_is_high,
    select_victim,
)
from modules.resource.taskqueue import TaskQueueController


LEVEL_NAMES = {
    "healthy": "健康",
    "prewarn": "预警",
    "warn": "告警",
    "pause": "暂停级",
    "critical": "严重",
}


def default_state() -> dict[str, Any]:
    return {
        "decision_level": "unknown",
        "below_kill_since": None,
        "io_high_since": None,
        "recovery_since": None,
        "incident_id": None,
        "incident_kills": 0,
        "kill_timestamps": [],
        "cooldown_until": 0,
        "last_scan": 0,
        "kill_locked_reason": "",
        "pending_effect_check": None,
        "last_action": None,
        "sample": {},
        "self_sample": None,
        "self_degraded": False,
        "last_persisted_at": 0,
        "no_victim_alerted": False,
        "kill_rate_limited": False,
    }


def load_state(paths: RuntimePaths) -> dict[str, Any]:
    state = default_state()
    loaded = read_json(paths.state_dir / "resource-state.json", {})
    if isinstance(loaded, dict):
        state.update({key: loaded[key] for key in state if key in loaded})
    return state


def save_state(paths: RuntimePaths, state: dict[str, Any]) -> None:
    atomic_write_json(paths.state_dir / "resource-state.json", state)


def incident_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:8]


def audit(paths: RuntimePaths, event: dict[str, Any]) -> None:
    append_jsonl(paths.logs_dir / "resource-actions.jsonl", event)


def alert(
    paths: RuntimePaths,
    settings: Settings,
    *,
    severity: str,
    title: str,
    body: str,
    details: dict[str, Any],
    dedupe_key: str,
) -> None:
    if not settings.alert_enabled:
        return
    enqueue(
        paths.state_dir,
        source="resource",
        severity=severity,
        title=title,
        body=body,
        details=details,
        dedupe_key=dedupe_key,
        cooldown_seconds=settings.alert_cooldown_seconds,
    )


def update_temporal_state(
    state: dict[str, Any], snapshot: HostSnapshot, settings: Settings, now: float
) -> tuple[bool, bool]:
    available = snapshot.meminfo["MemAvailable"]
    if available <= settings.kill_available:
        state["below_kill_since"] = state.get("below_kill_since") or now
    else:
        state["below_kill_since"] = None
    if io_is_high(snapshot, settings):
        state["io_high_since"] = state.get("io_high_since") or now
    else:
        state["io_high_since"] = None
    memory_sustained = state.get("below_kill_since") is not None and (
        now - float(state["below_kill_since"]) >= settings.kill_sustained_seconds
    )
    io_sustained = state.get("io_high_since") is not None and (
        now - float(state["io_high_since"]) >= settings.io_sustained_seconds
    )
    return memory_sustained, io_sustained


def deep_tasks(
    paths: RuntimePaths,
    settings: Settings,
    snapshot: HostSnapshot,
    proc_root: Path,
    now: float,
) -> list[TaskUsage]:
    tasks = read_running_tasks(settings.taskqueue_state_dir)
    attribute_tasks(tasks, proc_root)
    annotate_protection(
        tasks,
        load_leases(paths.state_dir, now),
        settings,
        snapshot.meminfo["MemAvailable"],
    )
    return tasks


def check_kill_effect(
    paths: RuntimePaths,
    settings: Settings,
    state: dict[str, Any],
    snapshot: HostSnapshot,
    decision: Decision,
    now: float,
) -> None:
    pending = state.get("pending_effect_check")
    if not isinstance(pending, dict) or now < float(pending.get("check_after", 0)):
        return
    metric = str(pending.get("metric", "memory"))
    before = int(pending.get("before_value", 0))
    if metric == "fd":
        improvement = before - snapshot.fd.used
        minimum = 1
    elif metric == "processes":
        improvement = before - snapshot.threads.current
        minimum = 1
    else:
        improvement = snapshot.meminfo["MemAvailable"] - before
        minimum = settings.kill_min_improvement
    still_critical = any(
        signal.kill_eligible and signal.victim_metric == metric
        for signal in decision.signals
    )
    if still_critical and improvement < minimum:
        state["kill_locked_reason"] = (
            f"上一次按 {metric} 指标处置任务后，主机指标仅改善 {improvement}"
        )
        alert(
            paths,
            settings,
            severity="critical",
            title="资源守护已停止自动终止任务",
            body=state["kill_locked_reason"],
            details={"pending": pending, "snapshot": snapshot.to_dict()},
            dedupe_key="resource-kill-ineffective",
        )
    state["pending_effect_check"] = None


def rate_limit_allows(state: dict[str, Any], settings: Settings, now: float) -> bool:
    cutoff = now - settings.kill_rate_limit_window_seconds
    timestamps = [
        float(value)
        for value in state.get("kill_timestamps", [])
        if float(value) >= cutoff
    ]
    state["kill_timestamps"] = timestamps
    return len(timestamps) < settings.kill_rate_limit_count


def maybe_recover(
    paths: RuntimePaths,
    settings: Settings,
    state: dict[str, Any],
    snapshot: HostSnapshot,
    decision: Decision,
    now: float,
    perform_actions: bool,
) -> None:
    incident = state.get("incident_id")
    recovered = (
        snapshot.meminfo["MemAvailable"] >= settings.recovery_available
        and not decision.should_pause
    )
    if not incident or not recovered:
        state["recovery_since"] = None
        return
    state["recovery_since"] = state.get("recovery_since") or now
    if now - float(state["recovery_since"]) < settings.recovery_sustained_seconds:
        return
    released = False
    if perform_actions:
        released = TaskQueueController(paths, settings).release_maintenance(
            str(incident)
        )
    audit(
        paths,
        {
            "event": "recovery",
            "incident_id": incident,
            "released_maintenance": released,
            "snapshot": snapshot.to_dict(),
        },
    )
    alert(
        paths,
        settings,
        severity="info",
        title="主机资源状态已恢复",
        body=(
            "资源指标已连续处于恢复水位，"
            f"本次事件={incident}，调度维护{'已释放' if released else '无需释放或未由守卫持有'}。"
        ),
        details={
            "incident_id": incident,
            "released_maintenance": released,
            "decision": decision.to_dict(),
            "snapshot": snapshot.to_dict(),
        },
        dedupe_key="resource-recovery",
    )
    state.update(default_state())
    state["decision_level"] = decision.level
    state["sample"] = sample_state(snapshot)


def iteration(
    paths: RuntimePaths,
    settings: Settings,
    state: dict[str, Any],
    *,
    proc_root: Path = Path("/proc"),
    perform_actions: bool,
    now: float | None = None,
) -> tuple[dict[str, Any], int]:
    now = time.time() if now is None else now
    snapshot = collect_host(
        proc_root=proc_root,
        previous=state.get("sample") if isinstance(state.get("sample"), dict) else {},
        devices=settings.io_devices,
        now=now,
    )
    memory_sustained, io_sustained = update_temporal_state(
        state, snapshot, settings, now
    )
    decision = evaluate(snapshot, settings, io_sustained=io_sustained)
    self_usage, self_sample = collect_self_usage(
        state.get("self_sample"), proc_root=proc_root
    )
    state["self_sample"] = self_sample
    degraded = (
        self_usage.rss > settings.self_memory_limit
        or self_usage.cpu_pct > settings.self_cpu_limit_pct
    )
    old_level = state.get("decision_level", "unknown")
    old_degraded = bool(state.get("self_degraded"))
    material_before = (
        state.get("incident_id"),
        state.get("incident_kills"),
        state.get("kill_locked_reason"),
        state.get("last_action"),
        state.get("no_victim_alerted"),
        state.get("kill_rate_limited"),
        state.get("pending_effect_check"),
    )
    state["decision_level"] = decision.level
    state["self_degraded"] = degraded
    state["sample"] = sample_state(snapshot)

    if old_level != decision.level:
        audit(
            paths,
            {
                "event": "transition",
                "from": old_level,
                "to": decision.level,
                "decision": decision.to_dict(),
                "snapshot": snapshot.to_dict(),
            },
        )
        if decision.level != "healthy":
            alert(
                paths,
                settings,
                severity="critical" if decision.level == "critical" else "warning",
                title=f"主机资源状态已变为{LEVEL_NAMES.get(decision.level, decision.level)}",
                body="; ".join(signal.reason for signal in decision.signals),
                details={
                    "decision": decision.to_dict(),
                    "snapshot": snapshot.to_dict(),
                },
                dedupe_key=f"resource-transition-{decision.level}",
            )
    if degraded and not old_degraded:
        alert(
            paths,
            settings,
            severity="warning",
            title="资源守护自身负载过高，已降级",
            body=(
                f"内存占用={self_usage.rss} 字节，CPU 占用={self_usage.cpu_pct:.2f}%；"
                "自动处置已暂停"
            ),
            details={"rss": self_usage.rss, "cpu_pct": self_usage.cpu_pct},
            dedupe_key="resource-self-degraded",
        )

    check_kill_effect(paths, settings, state, snapshot, decision, now)
    override = active_override(paths.state_dir, now)
    actions_allowed = perform_actions and not degraded and not override
    controller: TaskQueueController | None = None

    if (
        actions_allowed
        and settings.action_mode in {"pause", "kill"}
        and decision.should_pause
    ):
        if not state.get("incident_id"):
            state["incident_id"] = incident_id()
        controller = TaskQueueController(paths, settings)
        created = controller.enter_maintenance(str(state["incident_id"]))
        if created:
            audit(
                paths,
                {
                    "event": "pause",
                    "incident_id": state["incident_id"],
                    "decision": decision.to_dict(),
                    "snapshot": snapshot.to_dict(),
                },
            )

    memory_sustained = memory_sustained or (
        snapshot.meminfo["MemAvailable"] <= settings.emergency_available
    )

    immediate_nonmemory = any(
        signal.kill_eligible and signal.resource != "memory"
        for signal in decision.signals
    )
    kill_ready = decision.should_kill and (memory_sustained or immediate_nonmemory)
    last_scan = float(state.get("last_scan") or 0)
    scan_due = (
        last_scan <= 0
        or last_scan > now
        or now - last_scan >= settings.task_scan_seconds
    )
    rate_allowed = rate_limit_allows(state, settings, now)
    rate_limit_relevant = (
        actions_allowed and settings.action_mode == "kill" and kill_ready
    )
    if rate_limit_relevant and not rate_allowed:
        if not state.get("kill_rate_limited"):
            audit(
                paths,
                {
                    "event": "kill-rate-limited",
                    "decision": decision.to_dict(),
                    "snapshot": snapshot.to_dict(),
                },
            )
            alert(
                paths,
                settings,
                severity="critical",
                title="已达到全局自动终止任务速率上限",
                body="在滚动时间窗口恢复前，将暂停继续自动终止任务。",
                details={"decision": decision.to_dict()},
                dedupe_key="resource-kill-rate-limit",
            )
        state["kill_rate_limited"] = True
    elif rate_allowed or not rate_limit_relevant:
        state["kill_rate_limited"] = False
    can_kill = (
        actions_allowed
        and settings.action_mode == "kill"
        and kill_ready
        and scan_due
        and now >= float(state.get("cooldown_until") or 0)
        and int(state.get("incident_kills") or 0) < settings.max_kills_per_incident
        and not state.get("kill_locked_reason")
        and rate_allowed
    )
    if can_kill:
        tasks = deep_tasks(paths, settings, snapshot, proc_root, now)
        state["last_scan"] = now
        emergency = snapshot.meminfo["MemAvailable"] <= settings.emergency_available
        victim = select_victim(
            tasks, settings, metric=decision.victim_metric, emergency=emergency
        )
        if victim:
            state["no_victim_alerted"] = False
            if decision.victim_metric == "fd":
                before = snapshot.fd.used
            elif decision.victim_metric == "processes":
                before = snapshot.threads.current
            else:
                before = snapshot.meminfo["MemAvailable"]
            controller = controller or TaskQueueController(paths, settings)
            success, detail = controller.kill_task(victim, proc_root)
            state["cooldown_until"] = now + settings.kill_cooldown_seconds
            state["last_action"] = {
                "task_id": victim.task_id,
                "success": success,
                "timestamp": now,
            }
            event = {
                "event": "kill" if success else "kill-failed",
                "incident_id": state.get("incident_id"),
                "task": victim.to_dict(),
                "detail": detail,
                "decision": decision.to_dict(),
                "snapshot": snapshot.to_dict(),
            }
            audit(paths, event)
            alert(
                paths,
                settings,
                severity="critical",
                title=f"受管任务{'已终止' if success else '终止失败'}",
                body=f"任务={victim.task_id}，用户={victim.user}，详情={detail}",
                details=event,
                dedupe_key=f"resource-kill-{victim.task_id}",
            )
            if success:
                state["incident_kills"] = int(state.get("incident_kills") or 0) + 1
                state.setdefault("kill_timestamps", []).append(now)
                state["pending_effect_check"] = {
                    "task_id": victim.task_id,
                    "metric": decision.victim_metric,
                    "before_value": before,
                    "check_after": now + max(5, settings.pressure_poll_seconds * 2),
                }
            else:
                state["kill_locked_reason"] = "pto-task 未确认任务已被完整清理"
        elif not state.get("no_victim_alerted"):
            state["no_victim_alerted"] = True
            event = {
                "event": "no-managed-victim",
                "decision": decision.to_dict(),
                "snapshot": snapshot.to_dict(),
            }
            audit(paths, event)
            alert(
                paths,
                settings,
                severity="critical",
                title="资源压力严重，但没有可处置的受管任务",
                body="任务调度将保持暂停，需要人工排查。",
                details=event,
                dedupe_key="resource-no-managed-victim",
            )

    maybe_recover(
        paths, settings, state, snapshot, decision, now, perform_actions=actions_allowed
    )
    material_after = (
        state.get("incident_id"),
        state.get("incident_kills"),
        state.get("kill_locked_reason"),
        state.get("last_action"),
        state.get("no_victim_alerted"),
        state.get("kill_rate_limited"),
        state.get("pending_effect_check"),
    )
    flush_interval = 60 if decision.level == "healthy" else 10
    should_save = (
        old_level != decision.level
        or old_degraded != degraded
        or material_before != material_after
        or now - float(state.get("last_persisted_at") or 0) >= flush_interval
    )
    if should_save:
        state["last_persisted_at"] = now
        save_state(paths, state)
    if degraded:
        delay = settings.self_degraded_poll_seconds
    elif decision.level == "healthy":
        delay = settings.healthy_poll_seconds
    else:
        delay = settings.pressure_poll_seconds
    return state, delay


def evaluate_read_only(
    paths: RuntimePaths,
    settings: Settings,
    *,
    proc_root: Path = Path("/proc"),
    deep: bool,
) -> dict[str, Any]:
    snapshot = collect_host(proc_root=proc_root, devices=settings.io_devices)
    decision = evaluate(snapshot, settings, io_sustained=False)
    tasks: list[TaskUsage] = []
    victim = None
    if deep:
        tasks = deep_tasks(paths, settings, snapshot, proc_root, snapshot.timestamp)
        victim = select_victim(
            tasks,
            settings,
            metric=decision.victim_metric,
            emergency=snapshot.meminfo["MemAvailable"] <= settings.emergency_available,
        )
    try:
        override = active_override(paths.state_dir)
    except DoctorError:
        override = {"status": "unavailable"}
    return {
        "snapshot": snapshot,
        "decision": decision,
        "tasks": tasks,
        "victim": victim,
        "override": override,
    }
