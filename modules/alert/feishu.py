from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.common import DoctorError, format_size, parse_bool, parse_data_config


BASE = "https://open.feishu.cn/open-apis"
SEVERITY = {"info": 0, "warning": 1, "critical": 2}
RECEIVE_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}
ALERT_KEYS = {
    # Legacy custom-bot keys remain accepted because disk.sh still supports
    # direct delivery for old installations.  The asynchronous worker does not
    # use the webhook and therefore cannot accidentally post resource events to
    # the old group.
    "FEISHU_WEBHOOK",
    "FEISHU_KEYWORD",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_RECEIVE_ID",
    "FEISHU_RECEIVE_TYPE",
    "NOTIFY_RECEIVE_ID",
    "NOTIFY_RECEIVE_TYPE",
    "FEISHU_DOCX_TOKEN",
    "FEISHU_WIKI_TOKEN",
    "FEISHU_LOG_UPLOAD",
    "FEISHU_MIN_SEVERITY",
    "FEISHU_MAX_ATTEMPTS",
    "FEISHU_RETRY_DELAYS",
    "ALERT_COOLDOWN_HOURS",
    "ALERT_DELIVERY",
}


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    receive_id: str = ""
    receive_type: str = "open_id"
    docx_token: str = ""
    wiki_token: str = ""
    upload_log: bool = True
    min_severity: str = "critical"
    keyword: str = ""
    max_attempts: int = 4
    retry_delays: tuple[int, ...] = (5, 30, 120)

    @property
    def private_enabled(self) -> bool:
        return bool(self.receive_id)

    @property
    def document_enabled(self) -> bool:
        return self.upload_log and bool(self.docx_token or self.wiki_token)

    @property
    def remote_enabled(self) -> bool:
        return self.private_enabled or self.document_enabled


def load_feishu_config(path: Path) -> FeishuConfig:
    values = parse_data_config(path, allowed=ALERT_KEYS, ignore_unknown=True)
    try:
        max_attempts = int(values.get("FEISHU_MAX_ATTEMPTS", "4"))
        delays = tuple(
            int(value)
            for value in values.get("FEISHU_RETRY_DELAYS", "5 30 120").split()
        )
        upload_log = parse_bool(values.get("FEISHU_LOG_UPLOAD", "true"))
    except ValueError as exc:
        raise DoctorError("invalid Feishu retry configuration") from exc
    if (
        max_attempts < 1
        or max_attempts > 10
        or any(delay < 0 or delay > 3600 for delay in delays)
    ):
        raise DoctorError("Feishu retry configuration is outside safe bounds")

    receive_id = values.get("FEISHU_RECEIVE_ID") or values.get(
        "NOTIFY_RECEIVE_ID", ""
    )
    receive_type = (
        values.get("FEISHU_RECEIVE_TYPE")
        or values.get("NOTIFY_RECEIVE_TYPE")
        or "open_id"
    ).lower()
    min_severity = values.get("FEISHU_MIN_SEVERITY", "critical").lower()
    if receive_type not in RECEIVE_TYPES:
        raise DoctorError(
            "FEISHU_RECEIVE_TYPE must be open_id, user_id, union_id, email, or chat_id"
        )
    if min_severity not in SEVERITY:
        raise DoctorError("FEISHU_MIN_SEVERITY must be info, warning, or critical")

    config = FeishuConfig(
        app_id=values.get("FEISHU_APP_ID", ""),
        app_secret=values.get("FEISHU_APP_SECRET", ""),
        receive_id=receive_id,
        receive_type=receive_type,
        docx_token=values.get("FEISHU_DOCX_TOKEN", ""),
        wiki_token=values.get("FEISHU_WIKI_TOKEN", ""),
        upload_log=upload_log,
        min_severity=min_severity,
        keyword=values.get("FEISHU_KEYWORD", ""),
        max_attempts=max_attempts,
        retry_delays=delays,
    )
    if bool(config.app_id) != bool(config.app_secret):
        raise DoctorError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set together")
    if config.remote_enabled and not config.app_id:
        raise DoctorError(
            "Feishu private message/document target is configured without app credentials"
        )
    return config


def severity_allows(config: FeishuConfig, event: dict[str, Any]) -> bool:
    level = str(event.get("severity", "warning")).lower()
    return SEVERITY.get(level, 0) >= SEVERITY[config.min_severity]


def _source_name(event: dict[str, Any]) -> str:
    source_key = str(event.get("source", "system-doctor"))
    return {
        "resource": "资源守护",
        "disk": "磁盘检测",
        "system-doctor": "系统诊断",
    }.get(source_key, source_key)


def _severity_name(event: dict[str, Any]) -> str:
    severity_key = str(event.get("severity", "warning")).lower()
    return {
        "info": "信息",
        "warning": "警告",
        "critical": "严重",
    }.get(severity_key, severity_key)


def render_message(event: dict[str, Any]) -> str:
    title = str(event.get("title", "系统诊断告警"))
    body = str(event.get("body", ""))
    text = f"[{_severity_name(event)}] {title}\n来源: {_source_name(event)}\n{body}"
    advice = event.get("advice")
    if isinstance(advice, list) and advice:
        lines = "\n".join(f"{index}. {item}" for index, item in enumerate(advice, 1))
        text += f"\n\n处理建议:\n{lines}"
    return text[:12000]


def _detail_summary(event: dict[str, Any]) -> str:
    details = event.get("details")
    if not isinstance(details, dict):
        return ""
    snapshot = details.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    parts: list[str] = []
    meminfo = snapshot.get("meminfo")
    if isinstance(meminfo, dict) and isinstance(meminfo.get("MemAvailable"), int):
        parts.append(f"MemAvailable={format_size(meminfo['MemAvailable'])}")
    fd = snapshot.get("fd")
    if isinstance(fd, dict) and fd.get("used") is not None:
        parts.append(f"FD={fd.get('used')}/{fd.get('maximum', 'unknown')}")
    threads = snapshot.get("threads")
    if isinstance(threads, dict) and threads.get("current") is not None:
        parts.append(
            f"threads={threads.get('current')}/{threads.get('maximum', 'unknown')}"
        )
    task = details.get("task")
    if isinstance(task, dict) and task.get("task_id"):
        parts.append(f"task={task['task_id']} user={task.get('user', 'unknown')}")
    decision = details.get("decision")
    if isinstance(decision, dict):
        parts.append(f"level={decision.get('level', 'unknown')}")
    return "; ".join(parts)


def _elements(text: str) -> list[dict[str, Any]]:
    return [{"text_run": {"content": text}}]


def _text_blocks(text: str, *, size: int = 3000, limit: int = 9000) -> list[dict]:
    text = text[:limit]
    return [
        {"block_type": 2, "text": {"elements": _elements(text[index : index + size])}}
        for index in range(0, len(text), size)
        if text[index : index + size]
    ]


def event_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    created = float(event.get("created_at", 0) or 0)
    when = (
        dt.datetime.fromtimestamp(created)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    title = str(event.get("title", "系统诊断事件"))
    heading = f"[{_severity_name(event)}] {title} · {when}"
    blocks: list[dict[str, Any]] = [
        {"block_type": 5, "heading3": {"elements": _elements(heading[:1000])}}
    ]
    blocks.extend(
        _text_blocks(
            f"来源：{_source_name(event)}\n{str(event.get('body', ''))}"
        )
    )
    summary = _detail_summary(event)
    if summary:
        blocks.extend(_text_blocks(f"关键指标：{summary}", limit=3000))
    advice = event.get("advice")
    if isinstance(advice, list) and advice:
        guidance = "\n".join(
            f"{index}. {item}" for index, item in enumerate(advice, 1)
        )
        blocks.extend(_text_blocks(f"处理建议：\n{guidance}", limit=6000))
    return blocks


class FeishuClient:
    """Small app-bot client with per-worker token and wiki resolution caches."""

    def __init__(self, config: FeishuConfig, *, timeout: int = 15):
        self.config = config
        self.timeout = timeout
        self._token: str | None = None
        self._token_refresh_at = 0.0
        self._document_id: str | None = None
        # Match the simpler perf tracker: Feishu traffic must not inherit an
        # unrelated shell proxy used for GitHub or package mirrors.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _api(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn":
            raise DoctorError("refusing non-Feishu API URL")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            url, data=payload, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_000)
        except urllib.error.HTTPError as exc:
            raise DoctorError(f"Feishu API HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DoctorError(f"Feishu request failed: {type(exc).__name__}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DoctorError("Feishu returned invalid JSON") from exc
        if result.get("code", 0) != 0:
            message = str(result.get("msg", "unknown error"))[:200]
            if token and (
                str(result.get("code", "")).startswith("999916")
                or "token" in message.lower()
            ):
                self._token = None
                self._token_refresh_at = 0.0
            raise DoctorError(f"Feishu API rejected request: {message}")
        return result

    def token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_refresh_at:
            result = self._api(
                "POST",
                f"{BASE}/auth/v3/tenant_access_token/internal",
                body={
                    "app_id": self.config.app_id,
                    "app_secret": self.config.app_secret,
                },
            )
            token = result.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise DoctorError("Feishu token response omitted tenant_access_token")
            self._token = token
            try:
                expires_in = int(result.get("expire", 7200))
            except (TypeError, ValueError):
                expires_in = 7200
            self._token_refresh_at = time.monotonic() + max(60, expires_in - 300)
        return self._token

    def send_private(self, event: dict[str, Any]) -> None:
        if not self.config.private_enabled:
            raise DoctorError("FEISHU_RECEIVE_ID is empty")
        title = str(event.get("title", "系统诊断告警"))
        if self.config.keyword:
            title = f"{self.config.keyword} {title}"
        content = render_message(event)
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red"
                if str(event.get("severity", "")).lower() == "critical"
                else "orange",
                "title": {"tag": "plain_text", "content": title[:1000]},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        }
        url = (
            f"{BASE}/im/v1/messages?receive_id_type={self.config.receive_type}"
        )
        self._api(
            "POST",
            url,
            token=self.token(),
            body={
                "receive_id": self.config.receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )

    def document_id(self) -> str:
        if self._document_id:
            return self._document_id
        if self.config.docx_token:
            self._document_id = self.config.docx_token
            return self._document_id
        if not self.config.wiki_token:
            raise DoctorError("FEISHU_DOCX_TOKEN or FEISHU_WIKI_TOKEN is required")
        result = self._api(
            "GET",
            f"{BASE}/wiki/v2/spaces/get_node?token={self.config.wiki_token}",
            token=self.token(),
        )
        node = result.get("data", {}).get("node", {})
        if node.get("obj_type") != "docx" or not node.get("obj_token"):
            raise DoctorError("configured Feishu wiki node is not a docx document")
        self._document_id = str(node["obj_token"])
        return self._document_id

    def _children_count(self, document_id: str) -> int:
        count = 0
        page_token = ""
        while True:
            url = (
                f"{BASE}/docx/v1/documents/{document_id}/blocks/{document_id}"
                "/children?page_size=500"
            )
            if page_token:
                url += f"&page_token={urllib.parse.quote(page_token)}"
            data = self._api("GET", url, token=self.token()).get("data", {})
            count += len(data.get("items", []))
            if not data.get("has_more"):
                return count
            page_token = str(data.get("page_token", ""))

    def append_document(self, event: dict[str, Any]) -> None:
        document_id = self.document_id()
        index = self._children_count(document_id)
        url = (
            f"{BASE}/docx/v1/documents/{document_id}/blocks/{document_id}/children"
        )
        self._api(
            "POST",
            url,
            token=self.token(),
            body={"children": event_blocks(event), "index": index},
        )


def send_once(config: FeishuConfig, event: dict[str, Any], timeout: int = 15) -> None:
    """Compatibility helper for callers that only need one private message."""

    FeishuClient(config, timeout=timeout).send_private(event)
