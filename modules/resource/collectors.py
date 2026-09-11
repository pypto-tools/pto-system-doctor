from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from modules.common import DoctorError


TASK_ID_RE = re.compile(r"^task_[0-9][0-9_]*$")


def read_numeric_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def read_meminfo(proc_root: Path = Path("/proc")) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = (
            (proc_root / "meminfo")
            .read_text(encoding="ascii", errors="replace")
            .splitlines()
        )
    except OSError as exc:
        raise DoctorError(f"cannot read meminfo: {exc}") from exc
    for line in lines:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        fields = raw.split()
        if not fields or not fields[0].isdigit():
            continue
        result[key] = int(fields[0]) * (
            1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
        )
    if "MemAvailable" not in result:
        raise DoctorError("MemAvailable is absent; refusing unsafe fallback")
    return result


VMSTAT_KEYS = (
    "pgscan_kswapd",
    "pgscan_direct",
    "allocstall",
    "pswpin",
    "pswpout",
    "oom_kill",
)


def read_vmstat(proc_root: Path = Path("/proc")) -> dict[str, int]:
    values = {key: 0 for key in VMSTAT_KEYS}
    try:
        lines = (
            (proc_root / "vmstat")
            .read_text(encoding="ascii", errors="replace")
            .splitlines()
        )
    except OSError:
        return values
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[0] in values and fields[1].isdigit():
            values[fields[0]] = int(fields[1])
    return values


def counter_rates(
    current: dict[str, int], previous: dict[str, Any] | None, now: float
) -> dict[str, float]:
    if not previous:
        return {key: 0.0 for key in current}
    elapsed = now - float(previous.get("timestamp", 0) or 0)
    old_values = previous.get("values", {})
    if elapsed <= 0 or not isinstance(old_values, dict):
        return {key: 0.0 for key in current}
    return {
        key: max(0, value - int(old_values.get(key, value))) / elapsed
        for key, value in current.items()
    }


@dataclass(frozen=True)
class FDMetrics:
    allocated: int
    unused: int
    used: int
    maximum: int
    ratio: float | None


def read_fd_metrics(proc_root: Path = Path("/proc")) -> FDMetrics:
    try:
        fields = (proc_root / "sys/fs/file-nr").read_text(encoding="ascii").split()
        allocated, unused, maximum = (int(value) for value in fields[:3])
    except (OSError, ValueError, IndexError):
        return FDMetrics(0, 0, 0, 0, None)
    used = max(0, allocated - unused)
    ratio = used / maximum if 0 < maximum < 2**63 - 1 else None
    return FDMetrics(allocated, unused, used, maximum, ratio)


@dataclass(frozen=True)
class ThreadMetrics:
    current: int
    maximum: int
    ratio: float | None
    cgroup_current: int | None = None
    cgroup_maximum: int | None = None
    cgroup_ratio: float | None = None


def read_thread_metrics(proc_root: Path = Path("/proc")) -> ThreadMetrics:
    current = 0
    try:
        field = (proc_root / "loadavg").read_text(encoding="ascii").split()[3]
        current = int(field.split("/", 1)[1])
    except (OSError, ValueError, IndexError):
        pass
    maximum = read_numeric_file(proc_root / "sys/kernel/threads-max") or 0
    ratio = current / maximum if maximum > 0 else None
    cgroup = read_pids_cgroup_limit()
    cgroup_current, cgroup_maximum = cgroup if cgroup else (None, None)
    cgroup_ratio = (
        cgroup_current / cgroup_maximum
        if cgroup_current is not None and cgroup_maximum
        else None
    )
    return ThreadMetrics(
        current, maximum, ratio, cgroup_current, cgroup_maximum, cgroup_ratio
    )


def read_pids_cgroup_limit() -> tuple[int, int | None] | None:
    """Return the current cgroup's pids usage/limit when the limit is finite."""
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    relative = None
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and "pids" in fields[1].split(","):
            relative = fields[2].lstrip("/")
            break
    if relative is None:
        return None
    root = Path("/sys/fs/cgroup/pids") / relative
    current = read_numeric_file(root / "pids.current")
    try:
        raw_max = (root / "pids.max").read_text(encoding="ascii").strip()
    except OSError:
        raw_max = ""
    maximum = int(raw_max) if raw_max.isdigit() else None
    return (current, maximum) if current is not None else None


@dataclass(frozen=True)
class DiskCounter:
    reads: int
    read_ms: int
    writes: int
    write_ms: int
    io_ms: int
    weighted_ms: int


@dataclass(frozen=True)
class IOMetrics:
    device: str
    util_pct: float
    await_ms: float
    weighted_queue: float


def read_diskstats(
    proc_root: Path = Path("/proc"), devices: tuple[str, ...] = ()
) -> dict[str, DiskCounter]:
    selected = set(devices)
    result: dict[str, DiskCounter] = {}
    try:
        lines = (
            (proc_root / "diskstats")
            .read_text(encoding="ascii", errors="replace")
            .splitlines()
        )
    except OSError:
        return result
    for line in lines:
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if selected and name not in selected:
            continue
        if not selected and re.match(r"^(loop|ram|zram|fd|sr|dm-|md)", name):
            continue
        try:
            result[name] = DiskCounter(
                reads=int(fields[3]),
                read_ms=int(fields[6]),
                writes=int(fields[7]),
                write_ms=int(fields[10]),
                io_ms=int(fields[12]),
                weighted_ms=int(fields[13]),
            )
        except ValueError:
            continue
    return result


def io_rates(
    current: dict[str, DiskCounter], previous: dict[str, Any] | None, now: float
) -> list[IOMetrics]:
    if not previous:
        return []
    elapsed = now - float(previous.get("timestamp", 0) or 0)
    values = previous.get("values", {})
    if elapsed <= 0 or not isinstance(values, dict):
        return []
    metrics: list[IOMetrics] = []
    for name, counter in current.items():
        old_raw = values.get(name)
        if not isinstance(old_raw, dict):
            continue
        try:
            old = DiskCounter(
                **{key: int(old_raw[key]) for key in DiskCounter.__dataclass_fields__}
            )
        except (KeyError, TypeError, ValueError):
            continue
        completed = max(0, counter.reads - old.reads) + max(
            0, counter.writes - old.writes
        )
        service_ms = max(0, counter.read_ms - old.read_ms) + max(
            0, counter.write_ms - old.write_ms
        )
        metrics.append(
            IOMetrics(
                device=name,
                util_pct=min(100.0, max(0, counter.io_ms - old.io_ms) / (elapsed * 10)),
                await_ms=service_ms / completed if completed else 0.0,
                weighted_queue=max(0, counter.weighted_ms - old.weighted_ms)
                / (elapsed * 1000),
            )
        )
    return sorted(
        metrics, key=lambda item: (item.util_pct, item.await_ms), reverse=True
    )


def read_key_value_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


@dataclass
class TaskUsage:
    task_id: str
    user: str
    task_pid: int | None
    device: str
    started: str
    pids: tuple[int, ...] = ()
    process_count: int = 0
    fd_count: int = 0
    pss: int = 0
    pss_anon: int = 0
    pss_file: int = 0
    pss_shmem: int = 0
    swap_pss: int = 0
    oom_score: int = 0
    oom_score_adj: int = -1000
    d_state_count: int = 0
    zombie_count: int = 0
    leased: bool = False
    lease_floor: int | None = None
    protected_reason: str = ""

    @property
    def kill_memory(self) -> int:
        return self.pss_anon + self.swap_pss

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    session: int
    state: str


def parse_proc_stat(text: str) -> ProcInfo | None:
    left, right = text.find("("), text.rfind(")")
    if left <= 0 or right <= left:
        return None
    try:
        pid = int(text[:left].strip())
        fields = text[right + 1 :].split()
        return ProcInfo(pid, int(fields[1]), int(fields[3]), fields[0])
    except (ValueError, IndexError):
        return None


def enumerate_processes(proc_root: Path) -> dict[int, ProcInfo]:
    result: dict[int, ProcInfo] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            info = parse_proc_stat(
                (entry / "stat").read_text(encoding="ascii", errors="replace")
            )
        except OSError:
            continue
        if info:
            result[info.pid] = info
    return result


def task_marker(proc_root: Path, pid: int) -> str | None:
    try:
        data = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for item in data.split(b"\0"):
        if item.startswith(b"TASKQUEUE_TASK_ID="):
            value = item.split(b"=", 1)[1].decode("ascii", errors="ignore")
            return value if TASK_ID_RE.fullmatch(value) else None
    return None


def read_memory_fields(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        fields = raw.split()
        if fields and fields[0].isdigit():
            result[key] = int(fields[0]) * (
                1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
            )
    return result


def read_running_tasks(taskqueue_state_dir: Path) -> list[TaskUsage]:
    running = taskqueue_state_dir / "running"
    try:
        paths = sorted(
            path
            for path in running.iterdir()
            if path.is_file() and TASK_ID_RE.fullmatch(path.name)
        )
    except OSError:
        return []
    tasks: list[TaskUsage] = []
    for path in paths:
        values = read_key_value_file(path)
        pid_text = values.get("TASK_PID", "")
        tasks.append(
            TaskUsage(
                task_id=path.name,
                user=values.get("SUBMIT_USER", "?"),
                task_pid=int(pid_text) if pid_text.isdigit() else None,
                device=values.get("DEVICE", ""),
                started=values.get("START_TIME", ""),
            )
        )
    return tasks


def process_fd_count(proc_root: Path, pid: int) -> int:
    try:
        return sum(1 for _ in (proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return 0


def attribute_tasks(
    tasks: list[TaskUsage], proc_root: Path = Path("/proc")
) -> list[TaskUsage]:
    processes = enumerate_processes(proc_root)
    by_id = {task.task_id: task for task in tasks}
    session_owner = {task.task_pid: task.task_id for task in tasks if task.task_pid}
    owners: dict[int, str] = {}
    for pid, info in processes.items():
        owner = session_owner.get(info.session)
        if owner:
            owners[pid] = owner
    for pid in processes:
        marker = task_marker(proc_root, pid)
        if marker in by_id:
            owners[pid] = marker
    for task in tasks:
        task.pids = tuple(
            sorted(pid for pid, owner in owners.items() if owner == task.task_id)
        )
        task.process_count = len(task.pids)
        for pid in task.pids:
            info = processes[pid]
            task.d_state_count += info.state == "D"
            task.zombie_count += info.state == "Z"
            task.fd_count += process_fd_count(proc_root, pid)
            memory = read_memory_fields(proc_root / str(pid) / "smaps_rollup")
            task.pss += memory.get("Pss", 0)
            task.pss_anon += memory.get("Pss_Anon", 0)
            task.pss_file += memory.get("Pss_File", 0)
            task.pss_shmem += memory.get("Pss_Shmem", 0)
            task.swap_pss += memory.get("SwapPss", 0)
            task.oom_score = max(
                task.oom_score,
                read_numeric_file(proc_root / str(pid) / "oom_score") or 0,
            )
            adj = read_numeric_file(proc_root / str(pid) / "oom_score_adj")
            if adj is not None:
                task.oom_score_adj = max(task.oom_score_adj, adj)
    return tasks


@dataclass
class HostSnapshot:
    timestamp: float
    meminfo: dict[str, int]
    vmstat: dict[str, int]
    vmstat_rates: dict[str, float]
    fd: FDMetrics
    threads: ThreadMetrics
    diskstats: dict[str, DiskCounter]
    io: list[IOMetrics]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "meminfo": self.meminfo,
            "vmstat": self.vmstat,
            "vmstat_rates": self.vmstat_rates,
            "fd": asdict(self.fd),
            "threads": asdict(self.threads),
            "io": [asdict(value) for value in self.io],
        }


def collect_host(
    *,
    proc_root: Path = Path("/proc"),
    previous: dict[str, Any] | None = None,
    devices: tuple[str, ...] = (),
    now: float | None = None,
) -> HostSnapshot:
    now = time.time() if now is None else now
    previous = previous or {}
    vmstat = read_vmstat(proc_root)
    diskstats = read_diskstats(proc_root, devices)
    return HostSnapshot(
        timestamp=now,
        meminfo=read_meminfo(proc_root),
        vmstat=vmstat,
        vmstat_rates=counter_rates(vmstat, previous.get("vmstat"), now),
        fd=read_fd_metrics(proc_root),
        threads=read_thread_metrics(proc_root),
        diskstats=diskstats,
        io=io_rates(diskstats, previous.get("diskstats"), now),
    )


def sample_state(snapshot: HostSnapshot) -> dict[str, Any]:
    return {
        "vmstat": {"timestamp": snapshot.timestamp, "values": snapshot.vmstat},
        "diskstats": {
            "timestamp": snapshot.timestamp,
            "values": {
                name: asdict(counter) for name, counter in snapshot.diskstats.items()
            },
        },
    }


@dataclass
class SelfUsage:
    rss: int
    cpu_pct: float


def collect_self_usage(
    previous: dict[str, float] | None,
    now: float | None = None,
    proc_root: Path = Path("/proc"),
) -> tuple[SelfUsage, dict[str, float]]:
    now = time.monotonic() if now is None else now
    memory = read_memory_fields(proc_root / "self/status")
    rss = memory.get("VmRSS", 0)
    cpu = time.process_time()
    cpu_pct = 0.0
    if previous:
        elapsed = now - float(previous.get("wall", now))
        if elapsed > 0:
            cpu_pct = max(0.0, cpu - float(previous.get("cpu", cpu))) / elapsed * 100
    return SelfUsage(rss, cpu_pct), {"wall": now, "cpu": cpu}
