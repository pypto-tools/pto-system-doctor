from __future__ import annotations

import subprocess
import time
from pathlib import Path

from modules.common import DoctorError, RuntimePaths, atomic_write_json, read_json
from modules.resource.collectors import TaskUsage, enumerate_processes, task_marker
from modules.resource.config import Settings


MAINTENANCE_REASON_PREFIX = "system-doctor-resource:"
MAINTENANCE_REASON_NOTICE = "；由资源守护自动触发；若长时间未解除，请联系管理员"


def maintenance_reason(incident_id: str) -> str:
    return f"{MAINTENANCE_REASON_PREFIX}{incident_id}{MAINTENANCE_REASON_NOTICE}"


def is_owned_maintenance_reason(reason: object, incident_id: str) -> bool:
    legacy_reason = f"{MAINTENANCE_REASON_PREFIX}{incident_id}"
    return reason in {legacy_reason, maintenance_reason(incident_id)}


class TaskQueueController:
    def __init__(self, paths: RuntimePaths, settings: Settings):
        self.paths = paths
        self.settings = settings

    def _run(
        self, arguments: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        command = self.settings.pto_task_command
        if not command.is_file() or not command.stat().st_mode & 0o111:
            raise DoctorError(f"pto-task command is not executable: {command}")
        try:
            return subprocess.run(
                [str(command), *arguments],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DoctorError(f"pto-task {' '.join(arguments[:2])} timed out") from exc

    def enter_maintenance(self, incident_id: str) -> bool:
        marker = self.settings.taskqueue_state_dir / "maintenance"
        if marker.exists():
            return False
        reason = maintenance_reason(incident_id)
        result = self._run(["--maintenance", "on", reason], 10)
        if result.returncode != 0:
            raise DoctorError(
                f"maintenance command failed: {result.stderr.strip()[:300]}"
            )
        try:
            current = marker.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise DoctorError("cannot verify maintenance marker") from exc
        if current != reason:
            return False
        atomic_write_json(
            self.paths.state_dir / "resource-maintenance-owner.json",
            {"incident_id": incident_id, "reason": reason, "created_at": time.time()},
        )
        return True

    def release_maintenance(self, incident_id: str) -> bool:
        owner_path = self.paths.state_dir / "resource-maintenance-owner.json"
        owner = read_json(owner_path, None)
        if not isinstance(owner, dict) or owner.get("incident_id") != incident_id:
            return False
        marker = self.settings.taskqueue_state_dir / "maintenance"
        try:
            current = marker.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return False
        if current != owner.get("reason"):
            return False
        result = self._run(["--maintenance", "off"], 10)
        if result.returncode != 0:
            raise DoctorError(
                f"maintenance release failed: {result.stderr.strip()[:300]}"
            )
        owner_path.unlink(missing_ok=True)
        return True

    def kill_task(
        self, task: TaskUsage, proc_root: Path = Path("/proc")
    ) -> tuple[bool, str]:
        result = self._run(
            ["--kill", task.task_id], self.settings.kill_confirm_seconds + 8
        )
        running = self.settings.taskqueue_state_dir / "running" / task.task_id
        deadline = time.monotonic() + self.settings.kill_confirm_seconds
        while running.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        descendants = [
            pid
            for pid, info in enumerate_processes(proc_root).items()
            if info.session == task.task_pid
            or task_marker(proc_root, pid) == task.task_id
        ]
        success = result.returncode == 0 and not running.exists() and not descendants
        detail = (result.stdout or result.stderr).strip().replace("\n", " ")[:500]
        if descendants:
            detail = f"{detail} remaining_pids={','.join(map(str, descendants[:20]))}".strip()
        return success, detail


def recover_owned_incident(
    paths: RuntimePaths, settings: Settings, state: dict
) -> dict:
    if state.get("incident_id"):
        return state
    owner = read_json(paths.state_dir / "resource-maintenance-owner.json", None)
    if not isinstance(owner, dict):
        return state
    incident = owner.get("incident_id")
    reason = owner.get("reason")
    if not isinstance(incident, str) or not is_owned_maintenance_reason(
        reason, incident
    ):
        return state
    try:
        current = (
            (settings.taskqueue_state_dir / "maintenance")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
    except (OSError, IndexError):
        return state
    if current == reason:
        state["incident_id"] = incident
    return state
