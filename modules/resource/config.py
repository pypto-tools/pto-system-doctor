from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modules.common import (
    DoctorError,
    RuntimePaths,
    parse_data_config,
    parse_size,
)


DEFAULTS: dict[str, str] = {
    "TASKQUEUE_ROOT": "/home/pypto-tools/pto-task",
    "PTO_TASK_COMMAND": "/usr/local/bin/pto-task",
    "ACTION_MODE": "observe",
    "MAINTENANCE_MIN_LEVEL": "critical",
    "WARN_AVAILABLE": "512G",
    "KILL_AVAILABLE": "256G",
    "EMERGENCY_AVAILABLE": "128G",
    "RECOVERY_AVAILABLE": "512G",
    "VMSTAT_PREWARN_PAGES_PER_SECOND": "500000",
    "VMSTAT_DIRECT_PAGES_PER_SECOND": "50000",
    "FD_WARN_ABSOLUTE": "500000",
    "FD_PAUSE_ABSOLUTE": "1000000",
    "FD_CRITICAL_ABSOLUTE": "2000000",
    "FD_MAX_RATIO": "0.80",
    "THREAD_WARN_ABSOLUTE": "500000",
    "THREAD_PAUSE_RATIO": "0.80",
    "THREAD_CRITICAL_RATIO": "0.90",
    "IO_DEVICES": "",
    "IO_PAUSE_UTIL_PCT": "90",
    "IO_PAUSE_AWAIT_MS": "100",
    "IO_SUSTAINED_SECONDS": "30",
    "HEALTHY_POLL_SECONDS": "10",
    "PRESSURE_POLL_SECONDS": "2",
    "TASK_SCAN_SECONDS": "15",
    "KILL_SUSTAINED_SECONDS": "15",
    "RECOVERY_SUSTAINED_SECONDS": "60",
    "KILL_COOLDOWN_SECONDS": "30",
    "KILL_CONFIRM_SECONDS": "12",
    "MAX_KILLS_PER_INCIDENT": "3",
    "KILL_RATE_LIMIT_COUNT": "2",
    "KILL_RATE_LIMIT_WINDOW_SECONDS": "60",
    "KILL_MIN_IMPROVEMENT": "8G",
    "MIN_VICTIM_ANON_MEMORY": "32G",
    "MIN_VICTIM_FD": "10000",
    "MIN_VICTIM_PROCESSES": "1000",
    "PROTECTED_USERS": "",
    "LEASE_DEFAULT_SECONDS": "3600",
    "LEASE_MAX_SECONDS": "14400",
    "OVERRIDE_MAX_SECONDS": "1800",
    "SELF_MEMORY_LIMIT": "50M",
    "SELF_CPU_LIMIT_PCT": "5",
    "SELF_DEGRADED_POLL_SECONDS": "30",
    "ALERT_ENABLED": "true",
    "ALERT_COOLDOWN_SECONDS": "900",
}


def positive_int(values: dict[str, str], key: str, *, allow_zero: bool = False) -> int:
    try:
        value = int(values[key])
    except (KeyError, ValueError) as exc:
        raise DoctorError(f"{key} must be an integer") from exc
    if value < 0 or (not allow_zero and value == 0):
        raise DoctorError(
            f"{key} must be {'non-negative' if allow_zero else 'positive'}"
        )
    return value


def ratio(values: dict[str, str], key: str, *, maximum: float = 1.0) -> float:
    try:
        value = float(values[key])
    except (KeyError, ValueError) as exc:
        raise DoctorError(f"{key} must be numeric") from exc
    if value < 0 or value > maximum:
        raise DoctorError(f"{key} must be between 0 and {maximum}")
    return value


def boolean(values: dict[str, str], key: str) -> bool:
    value = values[key].strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise DoctorError(f"{key} must be a boolean")


@dataclass(frozen=True)
class Settings:
    taskqueue_root: Path
    pto_task_command: Path
    action_mode: str
    maintenance_min_level: str
    warn_available: int
    kill_available: int
    emergency_available: int
    recovery_available: int
    vmstat_prewarn_rate: float
    vmstat_direct_rate: float
    fd_warn_absolute: int
    fd_pause_absolute: int
    fd_critical_absolute: int
    fd_max_ratio: float
    thread_warn_absolute: int
    thread_pause_ratio: float
    thread_critical_ratio: float
    io_devices: tuple[str, ...]
    io_pause_util_pct: float
    io_pause_await_ms: float
    io_sustained_seconds: int
    healthy_poll_seconds: int
    pressure_poll_seconds: int
    task_scan_seconds: int
    kill_sustained_seconds: int
    recovery_sustained_seconds: int
    kill_cooldown_seconds: int
    kill_confirm_seconds: int
    max_kills_per_incident: int
    kill_rate_limit_count: int
    kill_rate_limit_window_seconds: int
    kill_min_improvement: int
    min_victim_anon_memory: int
    min_victim_fd: int
    min_victim_processes: int
    protected_users: frozenset[str]
    lease_default_seconds: int
    lease_max_seconds: int
    override_max_seconds: int
    self_memory_limit: int
    self_cpu_limit_pct: float
    self_degraded_poll_seconds: int
    alert_enabled: bool
    alert_cooldown_seconds: int

    @property
    def taskqueue_state_dir(self) -> Path:
        return self.taskqueue_root / "state"


def resource_config_path(paths: RuntimePaths, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return paths.config_dir / "resource-guard.conf"


def load_settings(
    paths: RuntimePaths, explicit: str | None = None
) -> tuple[Settings, Path]:
    values = dict(DEFAULTS)
    default_path = paths.app_dir / "config" / "resource-default.conf"
    if default_path.exists():
        values.update(parse_data_config(default_path, allowed=set(DEFAULTS)))
    path = resource_config_path(paths, explicit)
    values.update(parse_data_config(path, allowed=set(DEFAULTS)))
    try:
        vmstat_prewarn_rate = float(values["VMSTAT_PREWARN_PAGES_PER_SECOND"])
        vmstat_direct_rate = float(values["VMSTAT_DIRECT_PAGES_PER_SECOND"])
        io_util = float(values["IO_PAUSE_UTIL_PCT"])
        io_await = float(values["IO_PAUSE_AWAIT_MS"])
        self_cpu = float(values["SELF_CPU_LIMIT_PCT"])
    except ValueError as exc:
        raise DoctorError("rate/percentage settings must be numeric") from exc
    settings = Settings(
        taskqueue_root=Path(values["TASKQUEUE_ROOT"]).expanduser(),
        pto_task_command=Path(values["PTO_TASK_COMMAND"]).expanduser(),
        action_mode=values["ACTION_MODE"].lower(),
        maintenance_min_level=values["MAINTENANCE_MIN_LEVEL"].lower(),
        warn_available=parse_size(values["WARN_AVAILABLE"]),
        kill_available=parse_size(values["KILL_AVAILABLE"]),
        emergency_available=parse_size(values["EMERGENCY_AVAILABLE"]),
        recovery_available=parse_size(values["RECOVERY_AVAILABLE"]),
        vmstat_prewarn_rate=vmstat_prewarn_rate,
        vmstat_direct_rate=vmstat_direct_rate,
        fd_warn_absolute=positive_int(values, "FD_WARN_ABSOLUTE"),
        fd_pause_absolute=positive_int(values, "FD_PAUSE_ABSOLUTE"),
        fd_critical_absolute=positive_int(values, "FD_CRITICAL_ABSOLUTE"),
        fd_max_ratio=ratio(values, "FD_MAX_RATIO"),
        thread_warn_absolute=positive_int(values, "THREAD_WARN_ABSOLUTE"),
        thread_pause_ratio=ratio(values, "THREAD_PAUSE_RATIO"),
        thread_critical_ratio=ratio(values, "THREAD_CRITICAL_RATIO"),
        io_devices=tuple(part for part in values["IO_DEVICES"].split(",") if part),
        io_pause_util_pct=io_util,
        io_pause_await_ms=io_await,
        io_sustained_seconds=positive_int(values, "IO_SUSTAINED_SECONDS"),
        healthy_poll_seconds=positive_int(values, "HEALTHY_POLL_SECONDS"),
        pressure_poll_seconds=positive_int(values, "PRESSURE_POLL_SECONDS"),
        task_scan_seconds=positive_int(values, "TASK_SCAN_SECONDS"),
        kill_sustained_seconds=positive_int(values, "KILL_SUSTAINED_SECONDS"),
        recovery_sustained_seconds=positive_int(values, "RECOVERY_SUSTAINED_SECONDS"),
        kill_cooldown_seconds=positive_int(
            values, "KILL_COOLDOWN_SECONDS", allow_zero=True
        ),
        kill_confirm_seconds=positive_int(values, "KILL_CONFIRM_SECONDS"),
        max_kills_per_incident=positive_int(values, "MAX_KILLS_PER_INCIDENT"),
        kill_rate_limit_count=positive_int(values, "KILL_RATE_LIMIT_COUNT"),
        kill_rate_limit_window_seconds=positive_int(
            values, "KILL_RATE_LIMIT_WINDOW_SECONDS"
        ),
        kill_min_improvement=parse_size(values["KILL_MIN_IMPROVEMENT"]),
        min_victim_anon_memory=parse_size(values["MIN_VICTIM_ANON_MEMORY"]),
        min_victim_fd=positive_int(values, "MIN_VICTIM_FD"),
        min_victim_processes=positive_int(values, "MIN_VICTIM_PROCESSES"),
        protected_users=frozenset(
            part.strip()
            for part in values["PROTECTED_USERS"].split(",")
            if part.strip()
        ),
        lease_default_seconds=positive_int(values, "LEASE_DEFAULT_SECONDS"),
        lease_max_seconds=positive_int(values, "LEASE_MAX_SECONDS"),
        override_max_seconds=positive_int(values, "OVERRIDE_MAX_SECONDS"),
        self_memory_limit=parse_size(values["SELF_MEMORY_LIMIT"]),
        self_cpu_limit_pct=self_cpu,
        self_degraded_poll_seconds=positive_int(values, "SELF_DEGRADED_POLL_SECONDS"),
        alert_enabled=boolean(values, "ALERT_ENABLED"),
        alert_cooldown_seconds=positive_int(
            values, "ALERT_COOLDOWN_SECONDS", allow_zero=True
        ),
    )
    validate(settings)
    return settings, path


def validate(settings: Settings) -> None:
    if settings.action_mode not in {"observe", "pause", "kill"}:
        raise DoctorError("ACTION_MODE must be observe, pause, or kill")
    if settings.maintenance_min_level not in {"pause", "critical"}:
        raise DoctorError("MAINTENANCE_MIN_LEVEL must be pause or critical")
    if (
        not settings.taskqueue_root.is_absolute()
        or not settings.pto_task_command.is_absolute()
    ):
        raise DoctorError("taskqueue paths must be absolute")
    if not (
        0
        < settings.emergency_available
        < settings.kill_available
        < settings.warn_available
        <= settings.recovery_available
    ):
        raise DoctorError("memory thresholds are not strictly ordered")
    if not (
        settings.fd_warn_absolute
        < settings.fd_pause_absolute
        < settings.fd_critical_absolute
    ):
        raise DoctorError("FD absolute thresholds are not strictly ordered")
    if settings.thread_pause_ratio >= settings.thread_critical_ratio:
        raise DoctorError("thread pause ratio must be below critical ratio")
    if settings.lease_default_seconds > settings.lease_max_seconds:
        raise DoctorError("default lease exceeds maximum lease")
    if not (0 <= settings.io_pause_util_pct <= 100):
        raise DoctorError("IO_PAUSE_UTIL_PCT must be between 0 and 100")
