from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from modules.alert.feishu import (
    FeishuClient,
    FeishuConfig,
    event_blocks,
    load_feishu_config,
    render_message,
)
from modules.alert.queue import enqueue, pending_count
from modules.alert.worker import process_one
from modules.common import DoctorError, RuntimePaths, parse_data_config
from modules.resource.collectors import (
    FDMetrics,
    HostSnapshot,
    IOMetrics,
    TaskUsage,
    ThreadMetrics,
    attribute_tasks,
    collect_host,
    counter_rates,
    read_meminfo,
)
from modules.resource.config import DEFAULTS, load_settings
from modules.resource.daemon import default_state, iteration, rate_limit_allows
from modules.resource.policy import evaluate, select_victim
from modules.resource.taskqueue import (
    MAINTENANCE_REASON_NOTICE,
    TaskQueueController,
    recover_owned_incident,
)


ROOT = Path(__file__).resolve().parents[1]


def paths(root: Path) -> RuntimePaths:
    app = root / "app"
    app.mkdir(parents=True)
    return RuntimePaths(
        app_dir=app,
        tool_root=root,
        config_dir=root / "config",
        state_dir=root / "state",
        logs_dir=root / "logs",
        tmp_dir=root / "tmp",
        installed=False,
    )


def settings(root: Path, **overrides):
    runtime = paths(root)
    config = root / "resource.conf"
    values = {"TASKQUEUE_ROOT": str(root / "queue"), **overrides}
    config.write_text(
        "\n".join(f'{key}="{value}"' for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    loaded, _ = load_settings(runtime, str(config))
    return runtime, loaded


def make_proc(root: Path, *, available_gib: int = 1024) -> Path:
    proc = root / "proc"
    (proc / "sys/fs").mkdir(parents=True)
    (proc / "sys/kernel").mkdir(parents=True)
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text(
        f"MemTotal: {2048 * 1024 * 1024} kB\n"
        f"MemAvailable: {available_gib * 1024 * 1024} kB\n"
        f"Cached: {900 * 1024 * 1024} kB\n",
        encoding="ascii",
    )
    (proc / "vmstat").write_text(
        "pgscan_kswapd 100\npgscan_direct 10\nallocstall 0\n"
        "pswpin 0\npswpout 0\noom_kill 0\n",
        encoding="ascii",
    )
    (proc / "sys/fs/file-nr").write_text("1000 100 10000\n", encoding="ascii")
    (proc / "sys/kernel/threads-max").write_text("100000\n", encoding="ascii")
    (proc / "loadavg").write_text("1 1 1 1/1000 42\n", encoding="ascii")
    (proc / "diskstats").write_text(
        "8 0 sda 100 0 0 100 200 0 0 200 0 300 400\n", encoding="ascii"
    )
    (proc / "self/status").write_text("VmRSS: 1000 kB\n", encoding="ascii")
    return proc


def fake_process(
    proc: Path,
    pid: int,
    session: int,
    marker: str,
    *,
    anon_kib: int,
    file_kib: int,
    state: str = "S",
) -> None:
    target = proc / str(pid)
    (target / "fd").mkdir(parents=True)
    (target / "fd/0").write_text("", encoding="ascii")
    (target / "stat").write_text(
        f"{pid} (worker) {state} 1 {session} {session} 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="ascii",
    )
    (target / "environ").write_bytes(f"TASKQUEUE_TASK_ID={marker}\0".encode())
    (target / "smaps_rollup").write_text(
        f"Pss: {anon_kib + file_kib} kB\n"
        f"Pss_Anon: {anon_kib} kB\nPss_File: {file_kib} kB\n"
        "Pss_Shmem: 0 kB\nSwapPss: 10 kB\n",
        encoding="ascii",
    )
    (target / "oom_score").write_text("500\n", encoding="ascii")
    (target / "oom_score_adj").write_text("100\n", encoding="ascii")


class ResourceTests(unittest.TestCase):
    def test_resource_maintenance_reason_is_explicit_and_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, configured = settings(root)
            queue_state = configured.taskqueue_state_dir
            queue_state.mkdir(parents=True)
            controller = TaskQueueController(runtime, configured)
            incident = "20260819T180000-example"

            def create_marker(arguments, _timeout):
                (queue_state / "maintenance").write_text(
                    arguments[2] + "\n", encoding="utf-8"
                )
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(controller, "_run", side_effect=create_marker):
                self.assertTrue(controller.enter_maintenance(incident))

            reason = (queue_state / "maintenance").read_text(encoding="utf-8")
            self.assertIn("由资源守护自动触发", reason)
            self.assertIn("若长时间未解除，请联系管理员", reason)
            self.assertTrue(reason.rstrip().endswith(MAINTENANCE_REASON_NOTICE))

            recovered = recover_owned_incident(
                runtime, configured, default_state()
            )
            self.assertEqual(recovered["incident_id"], incident)

    def test_resource_maintenance_recovery_accepts_legacy_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, configured = settings(root)
            queue_state = configured.taskqueue_state_dir
            queue_state.mkdir(parents=True)
            runtime.state_dir.mkdir(parents=True)
            incident = "20260819T180000-legacy"
            reason = f"system-doctor-resource:{incident}"
            (queue_state / "maintenance").write_text(reason + "\n", encoding="utf-8")
            (runtime.state_dir / "resource-maintenance-owner.json").write_text(
                json.dumps({"incident_id": incident, "reason": reason}),
                encoding="utf-8",
            )

            recovered = recover_owned_incident(
                runtime, configured, default_state()
            )
            self.assertEqual(recovered["incident_id"], incident)

    def test_config_is_data_not_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            path.write_text("ACTION_MODE=$(touch /tmp/nope)\n", encoding="utf-8")
            with self.assertRaises(DoctorError):
                parse_data_config(path, allowed=set(DEFAULTS))

    def test_memavailable_is_required_and_cache_is_not_pressure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = make_proc(root, available_gib=700)
            runtime, configured = settings(root)
            snapshot = collect_host(proc_root=proc)
            self.assertEqual(
                evaluate(snapshot, configured, io_sustained=False).level, "healthy"
            )
            (proc / "meminfo").write_text(
                "MemFree: 1 kB\nCached: 100 kB\n", encoding="ascii"
            )
            with self.assertRaises(DoctorError):
                read_meminfo(proc)

    def test_vmstat_uses_rate_not_lifetime_total(self):
        rates = counter_rates(
            {"pgscan_kswapd": 1_000_100},
            {"timestamp": 10, "values": {"pgscan_kswapd": 1_000_000}},
            20,
        )
        self.assertEqual(rates["pgscan_kswapd"], 10)

    def test_task_attribution_separates_file_pss_and_records_oom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = make_proc(root)
            task_id = "task_20260813_1"
            fake_process(proc, 100, 100, task_id, anon_kib=100, file_kib=5000)
            task = TaskUsage(task_id, "alice", 100, "0", "")
            attribute_tasks([task], proc)
            self.assertEqual(task.pss_anon, 100 * 1024)
            self.assertEqual(task.pss_file, 5000 * 1024)
            self.assertEqual(task.kill_memory, 110 * 1024)
            self.assertEqual(task.oom_score_adj, 100)
            self.assertEqual(task.fd_count, 1)

    def test_memory_victim_uses_anon_not_file_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime, configured = settings(Path(temporary))
            file_heavy = TaskUsage(
                "task_1", "a", 1, "", "", pss=600, pss_anon=10, pss_file=590
            )
            anon_heavy = TaskUsage(
                "task_2", "b", 2, "", "", pss=200, pss_anon=190, pss_file=10
            )
            victim = select_victim(
                [file_heavy, anon_heavy], configured, metric="memory", emergency=True
            )
            self.assertIs(victim, anon_heavy)

    def test_pause_level_io_does_not_enter_critical_only_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary))
            snapshot = HostSnapshot(
                timestamp=1,
                meminfo={"MemAvailable": 1024**4},
                vmstat={},
                vmstat_rates={},
                fd=FDMetrics(1, 0, 1, 100, 0.01),
                threads=ThreadMetrics(100, 10000, 0.01),
                diskstats={},
                io=[IOMetrics("sda", 99, 200, 10)],
            )
            decision = evaluate(snapshot, configured, io_sustained=True)
            self.assertEqual(decision.level, "pause")
            self.assertFalse(decision.should_pause)
            self.assertFalse(decision.should_kill)

    def test_pause_level_can_be_configured_to_enter_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(
                Path(temporary), MAINTENANCE_MIN_LEVEL="pause"
            )
            snapshot = HostSnapshot(
                timestamp=1,
                meminfo={"MemAvailable": 1024**4},
                vmstat={},
                vmstat_rates={},
                fd=FDMetrics(1, 0, 1, 100, 0.01),
                threads=ThreadMetrics(100, 10000, 0.01),
                diskstats={},
                io=[IOMetrics("sda", 99, 200, 10)],
            )
            self.assertTrue(
                evaluate(snapshot, configured, io_sustained=True).should_pause
            )

    def test_critical_pressure_enters_critical_only_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary))
            snapshot = HostSnapshot(
                timestamp=1,
                meminfo={"MemAvailable": configured.kill_available},
                vmstat={},
                vmstat_rates={},
                fd=FDMetrics(1, 0, 1, 100, 0.01),
                threads=ThreadMetrics(100, 10000, 0.01),
                diskstats={},
                io=[],
            )
            self.assertTrue(
                evaluate(snapshot, configured, io_sustained=False).should_pause
            )

    def test_fd_and_thread_critical_choose_specific_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary))
            base = dict(
                timestamp=1,
                meminfo={"MemAvailable": 1024**4},
                vmstat={},
                vmstat_rates={},
                diskstats={},
                io=[],
            )
            fd = HostSnapshot(
                **base,
                fd=FDMetrics(3_000_000, 0, 3_000_000, 10_000_000, 0.3),
                threads=ThreadMetrics(100, 10000, 0.01),
            )
            self.assertEqual(
                evaluate(fd, configured, io_sustained=False).victim_metric, "fd"
            )
            threads = HostSnapshot(
                **base,
                fd=FDMetrics(1, 0, 1, 100, 0.01),
                threads=ThreadMetrics(9500, 10000, 0.95),
            )
            self.assertEqual(
                evaluate(threads, configured, io_sustained=False).victim_metric,
                "processes",
            )

    def test_emergency_memory_has_victim_priority_over_other_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary))
            snapshot = HostSnapshot(
                timestamp=1,
                meminfo={"MemAvailable": 100 * 1024**3},
                vmstat={},
                vmstat_rates={},
                fd=FDMetrics(3_000_000, 0, 3_000_000, 10_000_000, 0.3),
                threads=ThreadMetrics(9_500, 10_000, 0.95),
                diskstats={},
                io=[],
            )
            decision = evaluate(snapshot, configured, io_sustained=False)
            self.assertEqual(decision.victim_metric, "memory")

    def test_observe_iteration_never_calls_taskqueue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = make_proc(root, available_gib=100)
            runtime, configured = settings(root, ACTION_MODE="observe")
            with mock.patch(
                "modules.resource.daemon.TaskQueueController"
            ) as controller:
                state, _ = iteration(
                    runtime,
                    configured,
                    default_state(),
                    proc_root=proc,
                    perform_actions=True,
                    now=1000,
                )
            controller.assert_not_called()
            self.assertEqual(state["decision_level"], "critical")
            self.assertEqual(pending_count(runtime.state_dir), 1)

    def test_kill_rate_limit_is_global_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary))
            configured = replace(
                configured, kill_rate_limit_count=2, kill_rate_limit_window_seconds=60
            )
            state = {"kill_timestamps": [950, 980, 900]}
            self.assertFalse(rate_limit_allows(state, configured, 1000))
            self.assertEqual(state["kill_timestamps"], [950.0, 980.0])

    def test_emergency_kill_uses_taskqueue_and_audits_oom_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = make_proc(root, available_gib=100)
            runtime, configured = settings(
                root, ACTION_MODE="kill", MIN_VICTIM_ANON_MEMORY="1K"
            )
            task_id = "task_20260813_99"
            running = configured.taskqueue_state_dir / "running"
            running.mkdir(parents=True)
            (running / task_id).write_text(
                "SUBMIT_USER=alice\nTASK_PID=100\nDEVICE=0\n", encoding="utf-8"
            )
            fake_process(proc, 100, 100, task_id, anon_kib=100, file_kib=5000)
            with mock.patch(
                "modules.resource.daemon.TaskQueueController"
            ) as controller_class:
                controller = controller_class.return_value
                controller.enter_maintenance.return_value = True
                controller.kill_task.return_value = (True, "confirmed")
                state, _ = iteration(
                    runtime,
                    configured,
                    default_state(),
                    proc_root=proc,
                    perform_actions=True,
                    now=1000,
                )
            controller.enter_maintenance.assert_called_once()
            controller.kill_task.assert_called_once()
            self.assertEqual(state["incident_kills"], 1)
            events = [
                json.loads(line)
                for line in (runtime.logs_dir / "resource-actions.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            kill = next(event for event in events if event["event"] == "kill")
            self.assertEqual(kill["task"]["oom_score_adj"], 100)
            self.assertEqual(kill["task"]["pss_file"], 5000 * 1024)

    def test_fd_victim_requires_meaningful_managed_contribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, configured = settings(Path(temporary), MIN_VICTIM_FD="100")
            task = TaskUsage("task_1", "a", 1, "", "", fd_count=99)
            self.assertIsNone(
                select_victim([task], configured, metric="fd", emergency=True)
            )


class AlertTests(unittest.TestCase):
    def test_resource_alerts_can_be_disabled_without_disabling_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = make_proc(root, available_gib=400)
            runtime, configured = settings(root, ALERT_ENABLED="false")
            iteration(
                runtime,
                configured,
                default_state(),
                proc_root=proc,
                perform_actions=False,
                now=1000,
            )
            self.assertEqual(pending_count(runtime.state_dir), 0)

    def test_feishu_message_uses_chinese_labels(self):
        message = render_message(
            {
                "source": "resource",
                "severity": "critical",
                "title": "资源压力严重",
                "body": "可用内存已降至紧急水位",
            }
        )
        self.assertEqual(
            message,
            "[严重] 资源压力严重\n来源: 资源守护\n可用内存已降至紧急水位",
        )

    def test_queue_adds_resource_specific_advice(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = enqueue(
                Path(temporary),
                source="resource",
                severity="critical",
                title="主机资源状态已变为严重",
                body="可用内存已降至紧急水位",
                details={
                    "decision": {
                        "signals": [{"resource": "memory", "level": "critical"}]
                    }
                },
                now=1000,
            )
            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(any("resource victims" in item for item in event["advice"]))

    def test_noncritical_event_only_appends_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = FeishuConfig(
                app_id="app",
                app_secret="secret",
                receive_id="ou_user",
                docx_token="doc",
                min_severity="critical",
                max_attempts=2,
                retry_delays=(0,),
            )
            enqueue(
                state,
                source="resource",
                severity="warning",
                title="pressure",
                body="test",
                now=1000,
            )
            client = mock.Mock()
            self.assertEqual(
                process_one(state, config, now=1000, client=client), "recorded"
            )
            client.send_private.assert_not_called()
            client.append_document.assert_called_once()

    def test_critical_private_message_is_deduplicated_but_document_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = FeishuConfig(
                app_id="app",
                app_secret="secret",
                receive_id="ou_user",
                docx_token="doc",
                min_severity="critical",
                max_attempts=2,
                retry_delays=(0,),
            )
            enqueue(
                state,
                source="resource",
                severity="critical",
                title="pressure",
                body="test",
                dedupe_key="same",
                cooldown_seconds=60,
                now=1000,
            )
            client = mock.Mock()
            self.assertEqual(
                process_one(state, config, now=1000, client=client), "sent"
            )
            enqueue(
                state,
                source="resource",
                severity="critical",
                title="pressure",
                body="again",
                dedupe_key="same",
                cooldown_seconds=60,
                now=1010,
            )
            self.assertEqual(
                process_one(state, config, now=1010, client=client), "recorded"
            )
            client.send_private.assert_called_once()
            self.assertEqual(client.append_document.call_count, 2)

    def test_document_retry_does_not_duplicate_successful_private_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = FeishuConfig(
                app_id="app",
                app_secret="secret",
                receive_id="ou_user",
                docx_token="doc",
                max_attempts=2,
                retry_delays=(0,),
            )
            enqueue(
                state,
                source="resource",
                severity="critical",
                title="pressure",
                body="test",
                now=1000,
            )
            client = mock.Mock()
            client.append_document.side_effect = [DoctorError("offline"), None]
            self.assertEqual(
                process_one(state, config, now=1000, client=client), "retry"
            )
            self.assertEqual(
                process_one(state, config, now=1001, client=client), "sent"
            )
            client.send_private.assert_called_once()
            self.assertEqual(client.append_document.call_count, 2)

    def test_worker_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = FeishuConfig(
                app_id="app",
                app_secret="secret",
                receive_id="ou_user",
                max_attempts=2,
                retry_delays=(0,),
            )
            enqueue(
                state,
                source="resource",
                severity="critical",
                title="pressure",
                body="test",
                now=1000,
            )
            client = mock.Mock()
            client.send_private.side_effect = DoctorError("offline")
            self.assertEqual(
                process_one(state, config, now=1000, client=client), "retry"
            )
            self.assertEqual(
                process_one(state, config, now=1001, client=client), "failed"
            )
            self.assertEqual(pending_count(state), 0)
            failed = list((state / "alerts/failed").glob("*.json"))
            self.assertEqual(len(failed), 1)
            self.assertEqual(json.loads(failed[0].read_text())["attempts"], 2)

    def test_simpler_recipient_aliases_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "system-doctor.conf"
            config_path.write_text(
                'FEISHU_APP_ID="app"\n'
                'FEISHU_APP_SECRET="secret"\n'
                'NOTIFY_RECEIVE_ID="ou_user"\n'
                'NOTIFY_RECEIVE_TYPE="open_id"\n',
                encoding="utf-8",
            )
            config = load_feishu_config(config_path)
            self.assertEqual(config.receive_id, "ou_user")
            self.assertTrue(config.private_enabled)
            self.assertEqual(config.min_severity, "critical")

    def test_document_blocks_include_advice_and_key_metrics(self):
        blocks = event_blocks(
            {
                "created_at": 1000,
                "source": "resource",
                "severity": "critical",
                "title": "资源压力严重",
                "body": "可用内存已降至紧急水位",
                "advice": ["检查责任任务"],
                "details": {
                    "snapshot": {"meminfo": {"MemAvailable": 1024**3}},
                    "decision": {"level": "critical"},
                },
            }
        )
        text = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("处理建议", text)
        self.assertIn("MemAvailable=1.0 GiB", text)

    def test_app_bot_private_message_request_uses_receive_id(self):
        config = FeishuConfig(
            app_id="app",
            app_secret="secret",
            receive_id="ou_user",
            receive_type="open_id",
        )
        client = FeishuClient(config)
        with mock.patch.object(
            client,
            "_api",
            side_effect=[
                {"tenant_access_token": "token", "expire": 7200},
                {"code": 0},
            ],
        ) as api:
            client.send_private(
                {
                    "severity": "critical",
                    "title": "资源压力严重",
                    "body": "内存不足",
                    "advice": ["检查责任任务"],
                }
            )

        self.assertEqual(api.call_count, 2)
        send_call = api.call_args_list[1]
        self.assertIn("receive_id_type=open_id", send_call.args[1])
        self.assertEqual(send_call.kwargs["body"]["receive_id"], "ou_user")
        card = json.loads(send_call.kwargs["body"]["content"])
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("处理建议", card["elements"][0]["text"]["content"])

    def test_docx_append_uses_current_root_child_count(self):
        config = FeishuConfig(
            app_id="app", app_secret="secret", docx_token="doc_123"
        )
        client = FeishuClient(config)
        client._token = "token"
        client._token_refresh_at = float("inf")
        with mock.patch.object(
            client,
            "_api",
            side_effect=[
                {"data": {"items": [{"block_id": "old"}], "has_more": False}},
                {"code": 0},
            ],
        ) as api:
            client.append_document(
                {
                    "created_at": 1000,
                    "source": "resource",
                    "severity": "warning",
                    "title": "资源预警",
                    "body": "测试",
                }
            )

        self.assertEqual(api.call_count, 2)
        append_call = api.call_args_list[1]
        self.assertTrue(append_call.args[1].endswith("/doc_123/children"))
        self.assertEqual(append_call.kwargs["body"]["index"], 1)


if __name__ == "__main__":
    unittest.main()
