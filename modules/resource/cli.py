from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from modules.common import (
    DoctorError,
    append_jsonl,
    exclusive_lock,
    format_size,
    parse_duration,
    parse_size,
    require_root,
    runtime_paths,
)
from modules.resource import VERSION
from modules.resource.collectors import TASK_ID_RE, TaskUsage
from modules.resource.config import Settings, load_settings
from modules.resource.daemon import evaluate_read_only, iteration, load_state
from modules.resource.exemptions import (
    active_override,
    load_leases,
    save_leases,
    save_override,
)
from modules.resource.taskqueue import recover_owned_incident


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pto-system-doctor resource")
    root.add_argument("--version", action="version", version=VERSION)
    root.add_argument("--config")
    root.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    sub = root.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--json", action="store_true")
    sub.add_parser("victims")
    daemon = sub.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    sub.add_parser("config-validate")

    lease = sub.add_parser("lease")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)
    lease_sub.add_parser("list")
    create = lease_sub.add_parser("create")
    create.add_argument("--task", dest="task_id", required=True)
    create.add_argument("--duration")
    create.add_argument("--floor")
    create.add_argument("--reason", required=True)
    revoke = lease_sub.add_parser("revoke")
    revoke.add_argument("--task", dest="task_id", required=True)

    override = sub.add_parser("override", aliases=["break-glass"])
    override_sub = override.add_subparsers(dest="override_action", required=True)
    override_sub.add_parser("status")
    enable = override_sub.add_parser("enable")
    enable.add_argument("--duration", required=True)
    enable.add_argument("--reason", required=True)
    enable.add_argument("--ack-host-may-become-unresponsive", action="store_true")
    override_sub.add_parser("disable")
    history = sub.add_parser("history")
    history.add_argument("--lines", type=int, default=50)
    return root


def snapshot_payload(result: dict) -> dict:
    snapshot = result["snapshot"]
    decision = result["decision"]
    victim = result["victim"]
    return {
        "version": VERSION,
        "snapshot": snapshot.to_dict(),
        "decision": decision.to_dict(),
        "tasks": [task.to_dict() for task in result["tasks"]],
        "victim": victim.to_dict() if victim else None,
        "override": result["override"],
    }


def cmd_status(args: argparse.Namespace, paths, settings: Settings) -> int:
    result = evaluate_read_only(
        paths, settings, proc_root=Path(args.proc_root), deep=False
    )
    payload = snapshot_payload(result)
    payload["mode"] = settings.action_mode
    try:
        payload["watch_state"] = (
            load_state(paths)
            if (paths.state_dir / "resource-state.json").exists()
            else {}
        )
    except DoctorError:
        payload["watch_state"] = {"status": "unavailable"}
    try:
        payload["active_leases"] = len(load_leases(paths.state_dir))
    except DoctorError:
        payload["active_leases"] = None
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        snapshot = result["snapshot"]
        print(f"mode:             {settings.action_mode}")
        print(f"pressure:         {result['decision'].level}")
        print(f"memory available: {format_size(snapshot.meminfo['MemAvailable'])}")
        print(f"file handles:     {snapshot.fd.used}")
        print(f"threads:          {snapshot.threads.current}")
        if snapshot.threads.cgroup_current is not None:
            maximum = snapshot.threads.cgroup_maximum or "max"
            print(f"cgroup pids:      {snapshot.threads.cgroup_current}/{maximum}")
        if snapshot.io:
            worst = snapshot.io[0]
            print(
                f"worst disk IO:    {worst.device} util={worst.util_pct:.1f}% await={worst.await_ms:.1f}ms"
            )
        print(f"override:         {'active' if result['override'] else 'inactive'}")
    return 0


def cmd_evaluate(args: argparse.Namespace, paths, settings: Settings) -> int:
    result = evaluate_read_only(
        paths, settings, proc_root=Path(args.proc_root), deep=True
    )
    payload = snapshot_payload(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        decision = result["decision"]
        print(
            f"pressure={decision.level} available={format_size(result['snapshot'].meminfo['MemAvailable'])}"
        )
        if result["override"]:
            print("action=suppressed reason=override")
        elif result["victim"]:
            victim: TaskUsage = result["victim"]
            print(
                f"candidate={victim.task_id} user={victim.user} "
                f"anon+swap={format_size(victim.kill_memory)} pss-file={format_size(victim.pss_file)} "
                f"fds={victim.fd_count} processes={victim.process_count}"
            )
        else:
            print("candidate=none")
    return {"healthy": 0, "prewarn": 1, "warn": 1, "pause": 2, "critical": 2}[
        result["decision"].level
    ]


def cmd_victims(args: argparse.Namespace, paths, settings: Settings) -> int:
    result = evaluate_read_only(
        paths, settings, proc_root=Path(args.proc_root), deep=True
    )
    tasks = sorted(result["tasks"], key=lambda task: task.kill_memory, reverse=True)
    print(
        f"{'TASK':40} {'USER':14} {'ANON+SWAP':>12} {'FILE PSS':>12} {'FD':>8} {'PROC':>8} PROTECTION"
    )
    for task in tasks:
        print(
            f"{task.task_id:40.40} {task.user:14.14} {format_size(task.kill_memory):>12} "
            f"{format_size(task.pss_file):>12} {task.fd_count:8} {task.process_count:8} "
            f"{task.protected_reason or '-'}"
        )
    return 0


def cmd_daemon(
    args: argparse.Namespace, paths, settings: Settings, config_path: Path
) -> int:
    if paths.installed and not config_path.exists():
        raise DoctorError(f"installed daemon requires configuration: {config_path}")
    if settings.action_mode != "observe":
        require_root(f"resource daemon ACTION_MODE={settings.action_mode}")
    state = recover_owned_incident(paths, settings, load_state(paths))
    with exclusive_lock(paths.state_dir / "resource-daemon.lock", nonblocking=True):
        print(f"resource daemon started mode={settings.action_mode}", flush=True)
        while True:
            state, delay = iteration(
                paths,
                settings,
                state,
                proc_root=Path(args.proc_root),
                perform_actions=True,
            )
            if args.once:
                return 0
            time.sleep(delay)


def known_task(settings: Settings, task_id: str) -> bool:
    return bool(TASK_ID_RE.fullmatch(task_id)) and any(
        (settings.taskqueue_state_dir / directory / task_id).is_file()
        for directory in ("pending", "running")
    )


def audit_admin(paths, event: dict) -> None:
    append_jsonl(paths.logs_dir / "resource-actions.jsonl", event)


def cmd_lease(args: argparse.Namespace, paths, settings: Settings) -> int:
    now = time.time()
    if args.lease_action == "list":
        for task_id, lease in sorted(load_leases(paths.state_dir, now).items()):
            print(
                f"{task_id} remaining={max(0, int(lease['expires_at'] - now))}s "
                f"floor={format_size(int(lease['floor']))} reason={lease.get('reason', '')}"
            )
        return 0
    require_root(f"resource lease {args.lease_action}")
    with exclusive_lock(paths.state_dir / "resource-admin.lock"):
        leases = load_leases(paths.state_dir, now)
        if args.lease_action == "revoke":
            existed = leases.pop(args.task_id, None) is not None
            save_leases(paths.state_dir, leases)
            audit_admin(
                paths,
                {"event": "lease-revoke", "task_id": args.task_id, "existed": existed},
            )
            print("lease revoked" if existed else "lease was not active")
            return 0
        if not known_task(settings, args.task_id):
            raise DoctorError(f"task is not pending or running: {args.task_id}")
        duration = (
            parse_duration(args.duration)
            if args.duration
            else settings.lease_default_seconds
        )
        if duration <= 0 or duration > settings.lease_max_seconds:
            raise DoctorError("lease duration exceeds configured limit")
        floor = parse_size(args.floor) if args.floor else settings.emergency_available
        if floor < settings.emergency_available or floor >= settings.kill_available:
            raise DoctorError(
                "lease floor must be at emergency or between emergency and kill waterlines"
            )
        reason = args.reason.strip()
        if not reason:
            raise DoctorError("lease reason must not be empty")
        leases[args.task_id] = {
            "created_at": now,
            "expires_at": now + duration,
            "floor": floor,
            "reason": reason,
            "created_by": os.environ.get("SUDO_USER")
            or os.environ.get("USER")
            or str(os.geteuid()),
        }
        save_leases(paths.state_dir, leases)
        audit_admin(
            paths,
            {"event": "lease-create", "task_id": args.task_id, **leases[args.task_id]},
        )
        print(f"lease created: {args.task_id}")
        return 0


def cmd_override(args: argparse.Namespace, paths, settings: Settings) -> int:
    now = time.time()
    target = paths.state_dir / "resource-override.json"
    if args.override_action == "status":
        value = active_override(paths.state_dir, now)
        if value:
            print(
                f"override: active remaining={max(0, int(value['expires_at'] - now))}s "
                f"reason={value.get('reason', '')}"
            )
        else:
            print("override: inactive")
        return 0
    require_root(f"resource override {args.override_action}")
    with exclusive_lock(paths.state_dir / "resource-admin.lock"):
        if args.override_action == "disable":
            target.unlink(missing_ok=True)
            audit_admin(paths, {"event": "override-disable"})
            print("override disabled")
            return 0
        if not args.ack_host_may_become_unresponsive:
            raise DoctorError("enable requires --ack-host-may-become-unresponsive")
        duration = parse_duration(args.duration)
        if duration <= 0 or duration > settings.override_max_seconds:
            raise DoctorError("override duration exceeds configured limit")
        reason = args.reason.strip()
        if not reason:
            raise DoctorError("override reason must not be empty")
        value = {
            "created_at": now,
            "expires_at": now + duration,
            "reason": reason,
            "created_by": os.environ.get("SUDO_USER")
            or os.environ.get("USER")
            or str(os.geteuid()),
        }
        save_override(paths.state_dir, value)
        audit_admin(paths, {"event": "override-enable", **value})
        print(f"override enabled for {duration}s")
        return 0


def cmd_history(args: argparse.Namespace, paths) -> int:
    if args.lines <= 0:
        raise DoctorError("--lines must be positive")
    try:
        lines = (
            (paths.logs_dir / "resource-actions.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except FileNotFoundError:
        return 0
    for line in lines[-args.lines :]:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    paths = runtime_paths(Path(__file__))
    try:
        settings, config_path = load_settings(paths, args.config)
        if args.command == "status":
            return cmd_status(args, paths, settings)
        if args.command == "evaluate":
            return cmd_evaluate(args, paths, settings)
        if args.command == "victims":
            return cmd_victims(args, paths, settings)
        if args.command == "daemon":
            return cmd_daemon(args, paths, settings, config_path)
        if args.command == "config-validate":
            print(f"configuration valid: {config_path}")
            return 0
        if args.command == "lease":
            return cmd_lease(args, paths, settings)
        if args.command in {"override", "break-glass"}:
            return cmd_override(args, paths, settings)
        if args.command == "history":
            return cmd_history(args, paths)
    except DoctorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
