# System Doctor

> 面向共享 Linux 开发机的网络与磁盘健康诊断工具。

`system-doctor` 合并了原 `net-doctor` 和 `disk_monitor`：网络诊断负责检查 DNS、代理、
公网出口、路由及主机负载；磁盘监控负责生成容量报告、列出大目录，并在空间低于阈值时
继续通过飞书机器人发送告警。网络诊断只读，磁盘消息只会在显式执行 `disk report` 或
满足告警条件的 `disk alert` 时发送。

## 命令

```bash
pto-system-doctor network              # 完整网络诊断
pto-system-doctor network --quick      # 跳过慢速出口、代理和测速探测
pto-system-doctor network --fix        # 汇总建议修复命令，但不自动执行
pto-system-doctor disk report          # 发送磁盘空间飞书报告
pto-system-doctor disk alert           # 低于阈值时发送飞书告警
```

退出码：网络诊断 `0` 表示通过、`1` 表示存在警告、`2` 表示存在失败。详细网络故障背景见
[网络运维手册](docs/NETWORK_RUNBOOK.md)。

## 安装

源码模式直接使用 `./pto-system-doctor`，配置和状态位于被 Git 忽略的 `runtime/`。
正式安装默认布局：

```text
/home/pypto-tools/system-doctor/
├── app/       # 程序，重装时更新
├── config/    # system-doctor.conf，升级不覆盖
├── state/     # 飞书告警冷却状态
├── logs/      # 磁盘监控日志
└── tmp/       # 网络探测临时文件
```

首次安装：

```bash
sudo ./install.sh --init-config
sudoedit /home/pypto-tools/system-doctor/config/system-doctor.conf
pto-system-doctor --help
```

安装器支持 `--tools-root DIR`，只更新 `app/` 并维护唯一公开命令
`/usr/local/bin/pto-system-doctor`。它不会运行诊断、扫描磁盘、发送飞书或启用 timer。

## 磁盘飞书配置

编辑 `config/system-doctor.conf`：

```bash
MOUNT_POINT="/"
SCAN_DIR="/home"
THRESHOLD_PCT=10
EXTRA_ALERT_MOUNTS="/home:10"
TOP_N=20
FEISHU_WEBHOOK=""
FEISHU_KEYWORD=""
DU_TIMEOUT=1800
ALERT_COOLDOWN_HOURS=20
```

`FEISHU_WEBHOOK` 属于凭据，配置文件默认权限为 `0600`，不得提交或输出。重复安装不会
覆盖配置，也不会删除 `state/last_alert`。

## 定时报告与告警

systemd 操作必须显式执行。安装 unit 不会自动启用：

```bash
sudo pto-system-doctor systemd install
sudo pto-system-doctor systemd enable
pto-system-doctor systemd status
```

默认每周二发送一次磁盘报告，并在工作日检查低空间告警。可在启用前编辑安装包中的
timer 模板；`disable` 会停止 timer，`uninstall` 会移除 unit，但保留配置和状态。

## 验证

```bash
bash tests/test_install.sh
git diff --check
```
