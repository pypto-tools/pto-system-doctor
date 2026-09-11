from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from modules.alert.advice import recommendations
from modules.common import append_jsonl, atomic_write_json, ensure_dirs


def alert_root(state_dir: Path) -> Path:
    return state_dir / "alerts"


def enqueue(
    state_dir: Path,
    *,
    source: str,
    severity: str,
    title: str,
    body: str,
    details: dict[str, Any] | None = None,
    dedupe_key: str = "",
    cooldown_seconds: int = 0,
    now: float | None = None,
) -> Path:
    root = alert_root(state_dir)
    pending = root / "pending"
    ensure_dirs(pending, root / "failed")
    now = time.time() if now is None else now
    event_id = (
        time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
        + "-"
        + uuid.uuid4().hex[:10]
    )
    if not dedupe_key:
        dedupe_key = hashlib.sha256(f"{source}\0{title}".encode()).hexdigest()[:20]
    payload = {
        "schema": 1,
        "event_id": event_id,
        "created_at": now,
        "source": source,
        "severity": severity,
        "title": title,
        "body": body,
        "details": details or {},
        "dedupe_key": dedupe_key,
        "cooldown_seconds": max(0, int(cooldown_seconds)),
        "attempts": 0,
        "next_attempt_at": now,
    }
    payload["advice"] = recommendations(payload)
    target = pending / f"{event_id}.json"
    atomic_write_json(target, payload)
    append_jsonl(root / "events.jsonl", {"event": "queued", **payload})
    return target


def pending_count(state_dir: Path) -> int:
    try:
        return sum(
            1
            for path in (alert_root(state_dir) / "pending").iterdir()
            if path.suffix == ".json"
        )
    except OSError:
        return 0


def load_event(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("unsupported alert event")
    return value


def replace_event(path: Path, event: dict[str, Any]) -> None:
    atomic_write_json(path, event)


def complete_event(
    state_dir: Path, path: Path, event: dict[str, Any], status: str
) -> None:
    root = alert_root(state_dir)
    append_jsonl(root / "events.jsonl", {"event": status, **event})
    if status == "failed":
        target = root / "failed" / path.name
        ensure_dirs(target.parent)
        atomic_write_json(target, event)
        path.unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)
