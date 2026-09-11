from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from modules.common import DoctorError, atomic_write_json, read_json
from modules.resource.collectors import TASK_ID_RE


def load_leases(state_dir: Path, now: float | None = None) -> dict[str, dict[str, Any]]:
    now = time.time() if now is None else now
    raw = read_json(state_dir / "resource-leases.json", {})
    if not isinstance(raw, dict):
        raise DoctorError("lease state must be an object")
    active: dict[str, dict[str, Any]] = {}
    for task_id, lease in raw.items():
        if not TASK_ID_RE.fullmatch(task_id) or not isinstance(lease, dict):
            continue
        try:
            if float(lease.get("expires_at", 0)) > now:
                int(lease.get("floor", 0))
                active[task_id] = lease
        except (TypeError, ValueError) as exc:
            raise DoctorError(f"invalid lease state for {task_id}") from exc
    return active


def save_leases(state_dir: Path, leases: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(state_dir / "resource-leases.json", leases)


def active_override(state_dir: Path, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    value = read_json(state_dir / "resource-override.json", None)
    if not isinstance(value, dict):
        return None
    try:
        return value if float(value.get("expires_at", 0)) > now else None
    except (TypeError, ValueError) as exc:
        raise DoctorError("invalid override state") from exc


def save_override(state_dir: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir / "resource-override.json", value)
