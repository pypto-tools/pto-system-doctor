from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from modules.alert.feishu import FeishuClient, FeishuConfig, severity_allows
from modules.alert.queue import (
    alert_root,
    complete_event,
    load_event,
    replace_event,
)
from modules.common import DoctorError, atomic_write_json, exclusive_lock, read_json


def cooldown_allows(state_dir: Path, event: dict, now: float) -> bool:
    dedupe = read_json(alert_root(state_dir) / "dedupe.json", {})
    if not isinstance(dedupe, dict):
        dedupe = {}
    last = float(dedupe.get(str(event.get("dedupe_key", "")), 0) or 0)
    return now - last >= int(event.get("cooldown_seconds", 0) or 0)


def record_delivery(state_dir: Path, event: dict, now: float) -> None:
    path = alert_root(state_dir) / "dedupe.json"
    dedupe = read_json(path, {})
    if not isinstance(dedupe, dict):
        dedupe = {}
    dedupe[str(event.get("dedupe_key", ""))] = now
    cutoff = now - 30 * 86400
    dedupe = {key: value for key, value in dedupe.items() if float(value) >= cutoff}
    atomic_write_json(path, dedupe)


def _delivery(event: dict[str, Any]) -> dict[str, str]:
    value = event.get("delivery")
    if not isinstance(value, dict):
        value = {}
        event["delivery"] = value
    return value


def _save_delivery(
    path: Path, event: dict[str, Any], channel: str, status: str
) -> None:
    _delivery(event)[channel] = status
    replace_event(path, event)


def process_one(
    state_dir: Path,
    config: FeishuConfig,
    now: float | None = None,
    *,
    client: FeishuClient | None = None,
) -> str:
    now = time.time() if now is None else now
    pending = alert_root(state_dir) / "pending"
    try:
        paths = sorted(path for path in pending.iterdir() if path.suffix == ".json")
    except OSError:
        return "empty"
    for path in paths:
        try:
            event = load_event(path)
        except (OSError, ValueError) as exc:
            event = {"event_id": path.stem, "error": str(exc)}
            complete_event(state_dir, path, event, "failed")
            return "failed"
        if float(event.get("next_attempt_at", 0) or 0) > now:
            continue
        client = client or FeishuClient(config)
        delivery = _delivery(event)
        try:
            notify = config.private_enabled and severity_allows(config, event)
            if notify and delivery.get("private") not in {"sent", "suppressed"}:
                if cooldown_allows(state_dir, event, now):
                    client.send_private(event)
                    _save_delivery(path, event, "private", "sent")
                    record_delivery(state_dir, event, now)
                else:
                    _save_delivery(path, event, "private", "suppressed")
            elif not notify and "private" not in delivery:
                _save_delivery(path, event, "private", "not-required")

            if config.document_enabled and delivery.get("document") != "appended":
                client.append_document(event)
                _save_delivery(path, event, "document", "appended")
            elif not config.document_enabled and "document" not in delivery:
                _save_delivery(path, event, "document", "not-configured")
        except DoctorError as exc:
            attempts = int(event.get("attempts", 0) or 0) + 1
            event["attempts"] = attempts
            event["last_error"] = str(exc)
            if attempts >= config.max_attempts:
                complete_event(state_dir, path, event, "failed")
                return "failed"
            delays = config.retry_delays or (0,)
            event["next_attempt_at"] = now + delays[min(attempts - 1, len(delays) - 1)]
            replace_event(path, event)
            return "retry"
        status = "sent" if delivery.get("private") == "sent" else "recorded"
        complete_event(state_dir, path, event, status)
        return status
    return "empty"


def run_worker(
    state_dir: Path,
    config: FeishuConfig,
    *,
    once: bool,
    poll_seconds: int = 2,
) -> int:
    lock = alert_root(state_dir) / "worker.lock"
    client = FeishuClient(config)
    try:
        with exclusive_lock(lock, nonblocking=True):
            while True:
                result = process_one(state_dir, config, client=client)
                if once:
                    return 1 if result == "failed" else 0
                if result in {"empty", "retry"}:
                    time.sleep(poll_seconds)
    except DoctorError:
        raise
    except KeyboardInterrupt:
        return 130
