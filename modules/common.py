from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DoctorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    tool_root: Path
    config_dir: Path
    state_dir: Path
    logs_dir: Path
    tmp_dir: Path
    installed: bool


def runtime_paths(anchor: Path) -> RuntimePaths:
    app_dir = anchor.resolve()
    while (
        app_dir.name not in {"app", "pto-system-doctor"} and app_dir.parent != app_dir
    ):
        app_dir = app_dir.parent
    installed = app_dir.name == "app"
    tool_root = app_dir.parent if installed else app_dir / "runtime"
    override = os.environ.get("PTO_TOOL_ROOT")
    if override:
        tool_root = Path(override).expanduser().resolve()
    return RuntimePaths(
        app_dir=app_dir,
        tool_root=tool_root,
        config_dir=tool_root / "config",
        state_dir=tool_root / "state",
        logs_dir=tool_root / "logs",
        tmp_dir=tool_root / "tmp",
        installed=installed,
    )


def ensure_dirs(*directories: Path) -> None:
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)


def parse_data_config(
    path: Path, *, allowed: set[str], ignore_unknown: bool = False
) -> dict[str, str]:
    """Parse restricted KEY=value data without executing shell syntax."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DoctorError(f"cannot read configuration {path}: {exc}") from exc
    for lineno, original in enumerate(lines, 1):
        stripped = original.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            if ignore_unknown:
                continue
            raise DoctorError(f"{path}:{lineno}: expected KEY=value")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise DoctorError(f"{path}:{lineno}: invalid key {key!r}")
        if key not in allowed:
            if ignore_unknown:
                continue
            raise DoctorError(f"{path}:{lineno}: unknown key {key!r}")
        if key in values:
            raise DoctorError(f"{path}:{lineno}: duplicate key {key}")
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise DoctorError(f"{path}:{lineno}: {exc}") from exc
        if len(tokens) > 1:
            raise DoctorError(f"{path}:{lineno}: value must be one token")
        value = tokens[0] if tokens else ""
        if any(marker in value for marker in ("$(`", "$(", "${", "`")):
            raise DoctorError(f"{path}:{lineno}: shell expansion is not allowed")
        values[key] = value
    return values


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DoctorError(f"invalid boolean: {value!r}")


def parse_size(value: str) -> int:
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?)(?:i?[Bb]?)?", value.strip(), re.I
    )
    if not match:
        raise DoctorError(f"invalid size: {value!r}")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise DoctorError(f"invalid size: {value!r}") from exc
    power = "KMGTPE".find(match.group(2).upper()) + 1 if match.group(2) else 0
    return int(number * 1024**power)


def parse_duration(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smhd]?)", value.strip().lower())
    if not match:
        raise DoctorError(f"invalid duration: {value!r}")
    return (
        int(match.group(1))
        * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    )


def format_size(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(amount) < 1024 or unit == "PiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} PiB"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise DoctorError(f"cannot read state {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    ensure_dirs(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    ensure_dirs(path.parent)
    payload = dict(event)
    payload.setdefault("timestamp", time.time())
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o640)
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


@contextlib.contextmanager
def exclusive_lock(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    ensure_dirs(path.parent)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise DoctorError(f"lock is already held: {path}") from exc
        yield


def require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise DoctorError(f"{action} requires root")
