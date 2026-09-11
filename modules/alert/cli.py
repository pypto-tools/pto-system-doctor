from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from modules.alert.feishu import load_feishu_config
from modules.alert.queue import enqueue, pending_count
from modules.alert.worker import run_worker
from modules.common import DoctorError, runtime_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pto-system-doctor alert")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show local alert queue status")
    worker = sub.add_parser("worker", help="deliver queued alerts")
    worker.add_argument("--once", action="store_true")
    emit = sub.add_parser("enqueue", help="queue an alert without network I/O")
    emit.add_argument("--source", required=True)
    emit.add_argument(
        "--severity", choices=("info", "warning", "critical"), required=True
    )
    emit.add_argument("--title", required=True)
    emit.add_argument("--body", required=True)
    emit.add_argument("--dedupe-key", default="")
    emit.add_argument("--cooldown", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = runtime_paths(Path(__file__))
    config_path = Path(
        os.environ.get("PTO_CONFIG_FILE", paths.config_dir / "system-doctor.conf")
    )
    try:
        if args.command == "status":
            config = load_feishu_config(config_path)
            print(
                json.dumps(
                    {
                        "pending": pending_count(paths.state_dir),
                        "private_message": config.private_enabled,
                        "document_upload": config.document_enabled,
                        "minimum_severity": config.min_severity,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "enqueue":
            path = enqueue(
                paths.state_dir,
                source=args.source,
                severity=args.severity,
                title=args.title,
                body=args.body,
                dedupe_key=args.dedupe_key,
                cooldown_seconds=args.cooldown,
            )
            print(path)
            return 0
        config = load_feishu_config(config_path)
        return run_worker(
            paths.state_dir,
            config,
            once=args.once,
        )
    except DoctorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
