# System Doctor

> 面向共享 Linux 开发机的网络、磁盘与主机资源健康诊断/保护工具。

`pto-system-doctor` 使用一个公开命令管理彼此隔离的模块：网络诊断检查 DNS、代理、
公网出口与路由；磁盘模块生成容量报告；资源模块低开销监控内存、回收压力、文件句柄、
线程数和磁盘 IO，并按配置观察、暂停调度或处置 `pto-task` 受管任务；告警 worker 独立
消费本地事件，把严重事件通过应用机器人私聊，并可将全部事件追加到飞书文档。磁盘扫描、
资源 daemon 和网络发送不在同一进程中运行。

## 命令

```bash
pto-system-doctor network              # 完整网络诊断
pto-system-doctor network --quick      # 跳过慢速出口、代理和测速探测
pto-system-doctor network --fix        # 汇总建议修复命令，但不自动执行
pto-system-doctor disk report          # 生成并排队（兼容配置可直发）磁盘报告
pto-system-doctor disk alert           # 低于阈值时生成并排队告警
pto-system-doctor resource status      # 轻量只读资源快照
pto-system-doctor resource evaluate    # 深度任务归属和候选预览
pto-system-doctor resource victims     # 列出受管任务资源占用
pto-system-doctor resource daemon      # 资源守护（默认 observe）
pto-system-doctor alert status         # 本地异步告警队列状态
pto-system-doctor alert worker --once  # 发送一个排队事件
```

退出码：网络诊断 `0` 表示通过、`1` 表示存在警告、`2` 表示存在失败。详细网络故障背景见
[网络运维手册](docs/NETWORK_RUNBOOK.md)。

## 安装

源码模式直接使用 `./pto-system-doctor`，配置和状态位于被 Git 忽略的 `runtime/`。
正式安装默认布局：

```text
/home/pypto-tools/pto-system-doctor/
├── app/       # 程序，重装时更新
├── config/    # 磁盘/飞书与资源配置，升级不覆盖
├── state/     # 资源状态、lease、override 和告警队列
├── logs/      # 磁盘与资源审计日志
└── tmp/       # 网络探测等临时文件
```

首次安装：

```bash
sudo ./install.sh --init-config
sudoedit /home/pypto-tools/pto-system-doctor/config/system-doctor.conf
pto-system-doctor --help
```

安装器支持 `--tools-root DIR`，只更新 `app/` 并维护唯一公开命令
`/usr/local/bin/pto-system-doctor`。它不会运行诊断、扫描磁盘、发送飞书或启用任何
timer/service。

## 模块边界

- `modules/resource/collectors.py`：只读采样 `/proc` 和任务资源归属。
- `modules/resource/policy.py`：阈值、状态和受害任务排序，不执行系统动作。
- `modules/resource/taskqueue.py`：唯一的 taskqueue 变更边界，只调用
  `pto-task --maintenance/--kill`，不直接发送信号。
- `modules/resource/daemon.py`：状态机、速率限制、处置效果锁和审计。
- `modules/alert/`：本地队列、飞书客户端与独立 worker。
- `disk.sh`、`network.sh`：继续保持定时/按需短进程，不进入资源轮询。

详细设计见 [RESOURCE_GUARD.md](docs/RESOURCE_GUARD.md)。

## 磁盘和飞书配置

编辑 `config/system-doctor.conf`：

```bash
MOUNT_POINT="/"
SCAN_DIR="/home"
THRESHOLD_PCT=10
EXTRA_ALERT_MOUNTS="/home:10"
TOP_N=20
FEISHU_APP_ID=""
FEISHU_APP_SECRET=""
FEISHU_RECEIVE_ID=""
FEISHU_RECEIVE_TYPE="open_id"
FEISHU_MIN_SEVERITY="critical"
FEISHU_LOG_UPLOAD=true
FEISHU_DOCX_TOKEN=""
# FEISHU_WIKI_TOKEN=""
FEISHU_MAX_ATTEMPTS=4
FEISHU_RETRY_DELAYS="5 30 120"
DU_TIMEOUT=1800
ALERT_COOLDOWN_HOURS=20
ALERT_DELIVERY="queue"
```

磁盘报告默认统计 `SCAN_DIR` 下的一级目录；若其中存在 `pyptouser`，则展开统计该目录下
每个用户子目录的占用，不把所有用户合并为一个 `pyptouser` 项。

告警 worker 使用企业自建应用，不使用群自定义机器人。默认只有 `critical` 事件通过
`FEISHU_RECEIVE_ID` 私聊；`info` 和 `warning` 仅保留本地记录并追加到配置的 Docx/Wiki
文档。严重事件同样写入文档。`NOTIFY_RECEIVE_ID` 和 `NOTIFY_RECEIVE_TYPE` 可作为
`FEISHU_RECEIVE_*` 的兼容别名，便于复用 simpler 性能测试工具的接收人配置。

应用需要机器人发消息权限以及 Docx 编辑权限；接收人必须位于应用可用范围，目标文档或
Wiki 空间必须将应用设为可编辑协作者。只配置私聊、只配置文档或完全本地记录均可；
`pto-system-doctor alert status` 会显示当前启用的投递目标，但不会输出凭据。

飞书请求默认最多尝试 4 次，失败后依次等待 5、30、120 秒；私聊和文档分别记录完成
状态，因此文档重试不会重复已经成功的私聊，也不会重新执行磁盘扫描。可通过
`FEISHU_MAX_ATTEMPTS` 和 `FEISHU_RETRY_DELAYS` 调整，重试次数始终有限。

`FEISHU_APP_SECRET` 和旧版 `FEISHU_WEBHOOK` 都属于凭据，配置文件默认权限为 `0600`，
不得提交或输出。重复安装不会覆盖配置，也不会删除 `state/last_alert`。为保证部署目录
统一，旧配置中的 `LOG_FILE` 和 `STATE_DIR` 会被忽略，日志和冷却状态始终写入本工具的
`logs/` 与 `state/`。

新配置使用 `ALERT_DELIVERY=queue`，磁盘模块和资源模块只写本地事件，由 alert worker
异步发送；旧配置没有该字段时暂时保留直接发送行为，避免升级后 worker 尚未启用导致
告警静默积压。旧版 `FEISHU_WEBHOOK` 只供这种磁盘直发兼容路径使用，异步 worker 不会
向群 Webhook 投递。

本地完整投递历史位于 `state/alerts/events.jsonl`，资源动作审计位于
`logs/resource-actions.jsonl`。排队时会按内存、回收、文件句柄、线程/PID、磁盘 IO 或
磁盘容量类型生成保守的处理建议；私聊和文档使用同一份建议，先要求只读归属检查，再给出
降并发、联系任务所有者或安全处置方向。

## 资源保护

资源配置单独位于 `config/resource-guard.conf`，格式是不会被 shell 执行的受限
`KEY=value` 文件。动作分三级：

- `ACTION_MODE=observe`：只采样、审计和排队告警。
- `ACTION_MODE=pause`：压力持续时暂停 pending 调度，不终止任务。
- `ACTION_MODE=kill`：在 pause 基础上，通过 `pto-task --kill` 一次处置一个受管任务。
- `MAINTENANCE_MIN_LEVEL=critical`：只有严重级资源压力才暂停 task-submit；
  pause 级事件仍会记录和告警。

内存主指标为 `MemAvailable`；任务排序主要使用 `Pss_Anon + SwapPss`，文件映射 PSS
单独记录。`pgscan_kswapd/pgscan_direct` 使用增长速率预警。FD 使用绝对阈值并仅在
`file-max` 有实际意义时参考比例；线程容量使用系统线程总数；高磁盘 IO 只暂停和告警，
不触发 Kill。全局 Kill 预算、单事件上限、冷却和“处置后内存未改善锁”共同防止风暴。

临时大任务可创建有期限、有安全水位的 lease；`override` 是有期限、需说明原因并明确
确认风险的人工护盾，会冻结所有自动暂停/Kill。两者都不会改变 `task-submit` 的职责。

## 定时报告与告警

systemd 操作必须显式执行。安装 unit 不会自动启用：

```bash
sudo pto-system-doctor systemd install
sudo pto-system-doctor systemd enable disk
pto-system-doctor systemd status
```

默认每周二发送一次磁盘报告，并在工作日检查低空间告警。可在启用前编辑安装包中的
timer 模板；`disable` 会停止 timer，`uninstall` 会移除 unit，但保留配置和状态。

资源和告警是独立 service，启用时必须明确范围：

```bash
sudo pto-system-doctor systemd install all
sudo pto-system-doctor systemd enable alert
sudo pto-system-doctor systemd enable resource
```

省略范围的 `systemd enable` 只启用原有磁盘 timers，避免升级时意外启动新服务。

## 验证

```bash
bash tests/test_install.sh
git diff --check
```

## AI Skill

仓库内置 [`skills/pto-system-doctor/SKILL.md`](skills/pto-system-doctor/SKILL.md)，用于指导
AI 安全选择网络、磁盘、资源、告警和 systemd 工作流，并明确区分只读操作、队列写入、
外部消息与系统变更。
