from __future__ import annotations

from typing import Any


def _decision(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details")
    if not isinstance(details, dict):
        return {}
    decision = details.get("decision")
    return decision if isinstance(decision, dict) else {}


def _resources(event: dict[str, Any]) -> set[str]:
    resources: set[str] = set()
    signals = _decision(event).get("signals", [])
    if isinstance(signals, list):
        for signal in signals:
            if isinstance(signal, dict) and isinstance(signal.get("resource"), str):
                resources.add(signal["resource"])
    title_and_body = f"{event.get('title', '')} {event.get('body', '')}"
    keywords = {
        "memory": ("内存", "MemAvailable"),
        "reclaim": ("回收", "kswapd", "pgscan"),
        "fd": ("文件句柄", "FD"),
        "threads": ("线程", "PID"),
        "io": ("磁盘延迟", "磁盘 IO", "IO "),
    }
    for resource, markers in keywords.items():
        if any(marker in title_and_body for marker in markers):
            resources.add(resource)
    return resources


def recommendations(event: dict[str, Any]) -> list[str]:
    """Return conservative operator guidance for a normalized alert event.

    Guidance intentionally starts with read-only attribution.  It never tells an
    operator to send raw signals or to change kernel limits before identifying
    the owner of the pressure.
    """

    source = str(event.get("source", ""))
    title = str(event.get("title", ""))
    resources = _resources(event)
    advice: list[str] = []

    if source == "disk":
        advice.extend(
            [
                "先用 df -h 确认触发挂载点，再对告警中列出的目录做定向 du 排查。",
                "联系占用者清理可再生成的 build、缓存和旧 checkpoint；重要数据先迁移，不要直接批量删除。",
                "清理后重新检查可用空间和 inode；仍持续下降时排查仍在写入的进程。",
            ]
        )

    if "memory" in resources or "reclaim" in resources:
        advice.extend(
            [
                "运行 pto-system-doctor resource victims，确认受管任务的匿名内存、用户和任务归属。",
                "优先减少新任务并联系高占用任务的所有者；需要处置时只通过 pto-task，避免直接发送信号。",
                "复查 MemAvailable、直接回收速率和 OOM 记录；若无受管候选，再排查非受管进程和容器。",
            ]
        )
    if "fd" in resources:
        advice.extend(
            [
                "检查 /proc/sys/fs/file-nr，并按进程统计 /proc/<pid>/fd，定位持续增长的文件句柄持有者。",
                "先修复或重启发生泄漏的任务/服务；确认业务确有容量需求后再评估提高限制。",
            ]
        )
    if "threads" in resources:
        advice.extend(
            [
                "用 ps -eLf 和 cgroup pids.current/pids.max 定位线程或子进程异常增长的任务。",
                "降低 worker 并发或停止失控任务，并确认子进程被完整回收后再恢复调度。",
            ]
        )
    if "io" in resources:
        advice.extend(
            [
                "用 iostat -xz 1 和 iotop -oPa 确认高延迟设备及主要读写进程。",
                "错峰 checkpoint、构建和批量扫描；磁盘 IO 压力只应暂停调度，不应据此终止任务。",
            ]
        )

    if "没有可处置的受管任务" in title:
        advice.extend(
            [
                "保持新任务调度暂停，立即检查非 pto-task 进程、容器和系统服务的资源占用。",
                "确认责任进程和数据安全后再人工处置；不要为了恢复调度而直接解除维护状态。",
            ]
        )
    if "终止失败" in title:
        advice.extend(
            [
                "检查 pto-task 的 running 状态、任务进程树和 NPU 锁是否一致，并查看是否存在 D 状态进程。",
                "自动处置已锁定时先完成状态核对；不要重复强杀或手工删除任务状态文件。",
            ]
        )
    if "速率上限" in title or "停止自动终止" in title:
        advice.append(
            "暂停追加自动处置，比较处置前后的主机指标，并由值班人员确认下一步任务选择。"
        )
    if "自身负载过高" in title:
        advice.extend(
            [
                "查看 resource service 日志和自身 RSS/CPU，确认是否存在频繁深度扫描或异常循环。",
                "守卫降级期间自动动作会暂停，应人工关注主机压力，修复后再确认守卫恢复。",
            ]
        )
    if "资源状态已恢复" in title:
        advice.append("继续观察一个任务周期，并确认本次事件持有的调度维护状态已经释放。")

    if not advice and source == "resource":
        advice.extend(
            [
                "先运行 pto-system-doctor resource status 确认当前水位和动作模式。",
                "需要任务归属时再运行 pto-system-doctor resource evaluate，确认责任任务后处理。",
            ]
        )

    # Preserve order while preventing repetitive combined-signal guidance.
    return list(dict.fromkeys(advice))[:8]
