# 开发机网络运维手册

`liteserver-hps-148e-00001` 的网络故障排查参考——这是一台 320 核、约 44 人
**共享**的开发机。本文是 [`net-doctor.sh`](net-doctor.sh) 背后的知识库：先跑脚本
拿到实时诊断，再来这里看**原因**和更深入的修复方法。

> **一句话心智模型：** 本机的**直连公网出口在基础设施/上游层面不稳定**。
> 其它一切（dnsmasq 缓存、`/etc/hosts` 固定项、ssh 隧道代理、Tailscale）都只是
> **掩盖**它——谁都无法**根治**。拿不准时，根因就是它。

---

## 0. 第一反应——永远先做这个

```bash
./net-doctor.sh --fix
```

看 **汇总 → 最可能的根本原因** 那一段。如果它是空的、且全部"通过"，那慢就是
上游出口的瞬时冷缓存未命中——等一会儿重试即可，本地无法修复。

---

## 1. 网络架构（实际是什么样的）

```
                 ssh 中转/堡垒机                      Tailscale 覆盖网
                 119.3.119.49（单点）                100.78.35.56（备份）
                       │                                   │
   互联网 ──抖动──▶    │ ◀── 入站 SSH                      │ WireGuard
   （上游出口）        ▼                                   ▼
            ┌───────────────────────────────────────────────────┐
            │  liteserver-hps-148e-00001   （无公网 IP）          │
            │  LAN 192.168.0.10/24  ·  网关 192.168.0.1          │
            │                                                    │
            │  /etc/resolv.conf ─▶ 127.0.0.1:53  (dnsmasq 缓存)  │
            │                       │  上游: 223.5.5.5 …          │
            │  /etc/hosts dns-hotfix│  固定 github/pypi/anthropic │
            │                                                    │
            │  用户的 ssh -L 隧道（来去无常，HTTP 或 SOCKS）：    │
            │     127.0.0.1:4780 / 7890 / 7891 / 11898           │
            └───────────────────────────────────────────────────┘
```

关键事实：

- **无公网 IP。** 入站 SSH 经中转机进来（主：`119.3.119.49`，华为云
  `124.70.231.x`）。中转机是单点故障源。
- **没有全机范围的代理。** `4780/7890/7891/11898` 端口是*各个用户自己*的
  `ssh -L` 隧道，指向他们各自的远端代理。它们随主人主连/断开而出现或消失。
  **不要**把它们当作全机出口来依赖。
- **dnsmasq（`127.0.0.1:53`）** 是维护者加的缓存层，用来吸收抖动出口。这是
  正确做法，会**保留**。
- **Tailscale**（`100.78.35.56`）是绕过中转机的备份 SSH 通道。

---

## 2. 故障模式、原因与修复

### 2.1 "网络慢" / DNS 卡顿

**现象：** `pip`、`git clone`、`curl` 卡几秒后或许才通。

| 可能原因 | 如何确认 | 修复 |
| ------- | ------- | --- |
| dnsmasq 缓存挂了 | `systemctl is-active dnsmasq` 非 active；`ss -lnup \| grep :53` 为空 | `sudo systemctl reset-failed dnsmasq && sudo systemctl restart dnsmasq` |
| `resolv.conf` 绕过了缓存 | `grep nameserver /etc/resolv.conf` 显示公网 IP 而非 `127.0.0.1`，**且 dnsmasq 在运行** | 先备份，再指回 `127.0.0.1`（见 §3.1）；或修 NetworkManager 的 `dns=` 让它别再改写 |
| 没有 `timeout` 上限 | `grep options /etc/resolv.conf` 为空 | `echo 'options timeout:2 attempts:2 single-request-reopen' \| sudo tee -a /etc/resolv.conf` |
| 上游出口抖动 | `dig +time=2 +tries=1 @223.5.5.5 github.com` 间歇失败 | **本地无解**——缓存只能掩盖；若持续则上报网络团队 |

> dnsmasq 曾被 `kill -9`，导致全机**每次查询 12 秒**。现已加 `Restart=on-failure`
> 自愈。别去 kill 它。

### 2.2 `curl`/`pip` 卡住后报代理错误

**原因：** `http_proxy`/`https_proxy` 指向了某个借用的 `ssh -L` 隧道，但其主人
已断开。**注意：隧道主人断开后，本地转发端口常常还在 `listen`**——`nc -z` 看着
"存活"，但后端已死，经它的 `curl`/`pip` 会先卡住再超时。**别用 `nc -z` 判断代理
能不能用，要真的经它发一次请求。**

```bash
# 确认：端口"能用"≠端口"开着"。真的经它发一次 CONNECT 才算数：
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 6 \
     -x http://127.0.0.1:4780 https://www.gstatic.com/generate_204
# 返回 204 = HTTP 代理可用；超时/000 = 死的（即使 nc -z 说"存活"）。
# 试 SOCKS5：把 -x 换成 socks5h://127.0.0.1:4780

# 一键判断所有端口是 http / socks5 / 死的：
./net-doctor.sh        # 检测 6 会逐项端到端验证

# 修法 A —— 直接去掉代理（国内镜像不走代理也能用）：
unset http_proxy https_proxy all_proxy

# 修法 B —— 改指向 net-doctor 标为"可用"的端口（按实测类型选 scheme）：
export https_proxy=http://127.0.0.1:<可用的http端口>
export http_proxy=$https_proxy
# 若实测是 SOCKS5：用 all_proxy 最稳，并清掉 http(s)_proxy：
export all_proxy=socks5h://127.0.0.1:<可用的socks端口>; unset http_proxy https_proxy
```

### 2.3 访问不了国际站点（github、anthropic、huggingface）

- **直连**通常会失败（GFW/抖动）——这是预期内的。
- 改走一个**存活的**隧道（§2.2）。隧道可能是 HTTP 也可能是 SOCKS5——若
  `http://` 报连接错误，试 `socks5h://`。
- Python 包优先用**国内镜像**（无需代理）：

  ```bash
  pip install -i https://mirrors.aliyun.com/pypi/simple/ <包名>
  ```

### 2.4 SSH 连不上这台机器

| 原因 | 修复 |
| ---- | --- |
| 中转机（`119.3.119.49`）被 WAN 抖动拖垮 | 改用 **Tailscale 备份**：`ssh <用户>@100.78.35.56` |
| 同一子网内 | 直连 LAN：`ssh <用户>@192.168.0.10` |
| Tailscale 显示**离线** | 它的控制面经 `HTTPS_PROXY=127.0.0.1:7890`（某用户隧道）连 `controlplane.tailscale.com`。若 7890 挂了则同步停止。先确认 7890 在线，再 `sudo systemctl restart tailscaled` |

> sshd 已设 `ClientAliveInterval 30` + `ClientAliveCountMax 2`，让中转机上掉线/丢包
> 的会话约 60 秒内被回收（曾消灭了 88 次/10 分钟的转发端口冲突风暴）。`UseDNS no`
> 已设。这些都是缓解措施，根因仍是 WAN 出口。

### 2.5 这些**不是**本机故障

- **ping 公网 IP 失败**——ICMP 被策略封禁。绝不要用 ping 公网的丢包当健康信号
  （ping LAN 网关是没问题的）。
- **`google.com` 解析不了**——预期（GFW），不是本机问题。
- **某个 `4780/7890/...` 端口不见了**——只是某用户断开了隧道，不是故障。

---

## 3. 安全的修复操作

> 改系统文件前务必先备份。多数步骤需要 `sudo`。

### 3.1 把 resolv.conf 指回 dnsmasq 缓存

```bash
sudo cp /etc/resolv.conf /etc/resolv.conf.bak.$(date +%Y%m%d-%H%M%S)
printf 'nameserver 127.0.0.1\noptions timeout:2 attempts:2 single-request-reopen\n' \
  | sudo tee /etc/resolv.conf
# 若 NetworkManager 反复覆盖，在 /etc/NetworkManager/NetworkManager.conf 里设
# `dns=none`（或 `dns=dnsmasq`），再 `sudo systemctl restart NetworkManager`。
```

### 3.2 重启 / 自愈 dnsmasq

```bash
sudo systemctl reset-failed dnsmasq
sudo systemctl restart dnsmasq
ss -lnup | grep 127.0.0.1:53        # 确认已重新绑定
```

### 3.3 重启 Tailscale 备份通道

```bash
nc -z -w2 127.0.0.1 7890 || echo "7890 控制面代理挂了——先修它"
sudo systemctl restart tailscaled
tailscale status | head            # 本机应不再显示 'offline'
```

> 节点 `KeyExpiry` 约 2026-10-13 —— 在 Tailscale 管理控制台关闭密钥过期，
> 让该备份长期有效。

---

## 4. 何时上报网络团队

只有在 net-doctor 的本地检测全绿、问题仍然存在时才上报——即问题出在
**上游出口**而非本地配置：

- dnsmasq 健康、resolv.conf 已指向它的前提下，`dig @223.5.5.5 github.com`
  在几分钟内 0/x ↔ 正常反复抖动。
- 连国内镜像（`mirrors.aliyun.com`）直连也不通。
- SSH 中转机在全机范围内掉会话（多用户受影响）。

给他们留证据：`./net-doctor.sh --no-color > /tmp/netdoc.log`。

---

## 5. 另见

- [`net-doctor.sh`](net-doctor.sh) —— 实时一键诊断脚本。
- [`README.md`](README.md) —— 工具概览与检测项表。
- `../perf-tracker/` —— 应对相同出口抖动的另一个工具（SSH 优先、代理兜底）。
