#!/usr/bin/env bash
#
# net-doctor.sh —— 共享开发机（liteserver-hps-148e-00001）一键网络诊断
#
# 自动检测本机已知的网络故障模式，指出根本原因，并打印每一项的具体修复命令。
#
# 设计原则：实时探测当前状态，绝不假设。本机配置会漂移（dnsmasq 被增删、
# resolv.conf 被 NetworkManager 改写、借用的 ssh 隧道代理来来去去），所以每项
# 检测都读取真实的当前状态，而不是依赖历史笔记。
#
# 用法：
#   ./net-doctor.sh              # 完整检测，人类可读
#   ./net-doctor.sh --quick      # 跳过较慢的出口探测
#   ./net-doctor.sh --fix        # 末尾额外打印可复制的修复命令块
#   ./net-doctor.sh --no-color   # 纯文本输出（用于日志/管道/cron）
#
# 退出码：0 = 全部通过，1 = 至少一个 WARN，2 = 至少一个 FAIL。

set -u

# ---------------------------------------------------------------- 选项
QUICK=0; SHOW_FIX=0; USE_COLOR=1
for arg in "$@"; do
  case "$arg" in
    --quick)    QUICK=1 ;;
    --fix)      SHOW_FIX=1 ;;
    --no-color) USE_COLOR=0 ;;
    -h|--help)
      sed -n '3,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $arg（用 --help 查看帮助）" >&2; exit 2 ;;
  esac
done
[ -t 1 ] || USE_COLOR=0

if [ "$USE_COLOR" = 1 ]; then
  R='\033[31m'; G='\033[32m'; Y='\033[33m'; B='\033[34m'; C='\033[36m'; D='\033[2m'; Z='\033[0m'; BOLD='\033[1m'
else
  R=''; G=''; Y=''; B=''; C=''; D=''; Z=''; BOLD=''
fi

# ---------------------------------------------------------------- 状态
PASS_N=0; WARN_N=0; FAIL_N=0
declare -a FIXES=()      # --fix 时打印的修复命令
declare -a ROOTCAUSE=()  # 汇总时打印的根因短句

hdr()  { printf "\n${BOLD}${B}== %s ==${Z}\n" "$1"; }
pass() { PASS_N=$((PASS_N+1)); printf "  ${G}通过${Z}  %s\n" "$1"; }
info() { printf "  ${D}····  %s${Z}\n" "$1"; }
warn() { # warn <说明> <原因> [修复命令]
  WARN_N=$((WARN_N+1)); printf "  ${Y}警告${Z}  %s\n" "$1"
  [ -n "${2:-}" ] && printf "        ${D}原因:${Z} %s\n" "$2"
  if [ -n "${3:-}" ]; then printf "        ${D}修复:${Z} ${C}%s${Z}\n" "$3"; FIXES+=("$3"); fi
}
fail() { # fail <说明> <原因> [修复命令]
  FAIL_N=$((FAIL_N+1)); printf "  ${R}失败${Z}  %s\n" "$1"
  [ -n "${2:-}" ] && printf "        ${D}原因:${Z} %s\n" "$2"
  if [ -n "${3:-}" ]; then printf "        ${D}修复:${Z} ${C}%s${Z}\n" "$3"; FIXES+=("$3"); fi
}
have() { command -v "$1" >/dev/null 2>&1; }

# proxy_kind <host> <port> —— 端到端探测一个端口究竟是不是能用的代理。
# 关键点：端口 listen（TCP accept）不代表代理能用——ssh -L 隧道的主人断开后，
# 本地转发端口往往还开着，但后端已死，curl/pip 会卡到超时。所以这里真的经它
# 发一次 CONNECT，打一个又小又快、且必须走国际出口的目标（generate_204）。
# 回显 http / socks5 / dead。
PROXY_PROBE_URL="https://www.gstatic.com/generate_204"
proxy_kind() {  # <host> <port> [max-time]
  local h="$1" p="$2" mt="${3:-6}" code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time "$mt" \
         -x "http://$h:$p" "$PROXY_PROBE_URL" 2>/dev/null)
  case "$code" in 200|204|301|302) echo http; return ;; esac
  code=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time "$mt" \
         -x "socks5h://$h:$p" "$PROXY_PROBE_URL" 2>/dev/null)
  case "$code" in 200|204|301|302) echo socks5; return ;; esac
  echo dead
}
# classify_ports <port...> —— 并行分类多个端口，回显 "<port> <kind>" 行（按端口号排序）。
# 分批并发（每批 PROXY_BATCH 个），避免一次性对几百个端口拉起 curl 风暴。
PROXY_BATCH=32
classify_ports() {  # <max-time> <port...>
  local mt="$1"; shift
  local tmp port n=0; tmp=$(mktemp -d) || return 1
  for port in "$@"; do
    ( echo "$port $(proxy_kind 127.0.0.1 "$port" "$mt")" >"$tmp/$port" ) &
    n=$((n+1)); [ $((n % PROXY_BATCH)) -eq 0 ] && wait
  done
  wait
  cat "$tmp"/* 2>/dev/null | sort -n
  rm -rf "$tmp"
}

# ================================================================ 横幅
printf "${BOLD}net-doctor 网络诊断${Z}  ${D}%s  主机=%s${Z}\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$(hostname)"

# ================================================================ 1. DNS 解析
hdr "1. DNS 解析（'网络变慢'的头号症状）"
DNS_HOT_OK=0
for dom in github.com pypi.org api.anthropic.com; do
  t0=$(date +%s%N)
  if getent hosts "$dom" >/dev/null 2>&1; then
    ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    if [ "$ms" -gt 1500 ]; then
      warn "$dom 能解析，但很慢（${ms}ms）" \
           "冷缓存未命中，走了抖动的直连 UDP/53 出口" \
           "确认 dnsmasq 缓存 + /etc/hosts 固定项在解析链路前面（见检测 3/4）"
    else
      pass "$dom -> 解析成功（${ms}ms）"; DNS_HOT_OK=$((DNS_HOT_OK+1))
    fi
  else
    fail "$dom 解析失败" \
         "未命中缓存的查询打到了正在中断的直连公网出口" \
         "确认 dnsmasq 在运行（检测 2）后重试；若持续失败 => 联系网络团队（上游出口）"
  fi
done
# google.com 解析失败属正常（GFW），仅作信息提示
if getent hosts google.com >/dev/null 2>&1; then
  info "google.com 可解析（当前到国际 DNS 的出口正常）"
else
  info "google.com 无法解析 —— 这里属正常（GFW），不是本机故障"
fi

# ================================================================ 2. dnsmasq 缓存层
hdr "2. 本地 DNS 缓存（dnsmasq @ 127.0.0.1:53）"
DNSMASQ_ACTIVE=0
if have systemctl && systemctl is-active dnsmasq >/dev/null 2>&1; then
  DNSMASQ_ACTIVE=1; pass "dnsmasq.service 正在运行"
else
  warn "dnsmasq.service 未运行" \
       "用于吸收抖动出口的缓存层挂了；所有查询将走直连（曾被 kill -9 -> 全机每次查询 12 秒）" \
       "sudo systemctl reset-failed dnsmasq && sudo systemctl restart dnsmasq"
  ROOTCAUSE+=("dnsmasq 挂掉 -> 所有 DNS 走抖动的直连出口")
fi
LISTEN53=$( (ss -lnup 2>/dev/null || netstat -lnup 2>/dev/null) | grep -c '127.0.0.1:53' )
if [ "${LISTEN53:-0}" -ge 1 ]; then
  pass "127.0.0.1:53 正在监听（UDP）"
else
  [ "$DNSMASQ_ACTIVE" = 1 ] && warn "dnsmasq 在运行，但 127.0.0.1:53 没有监听" \
    "dnsmasq 可能绑定失败" \
    "sudo systemctl restart dnsmasq && ss -lnup | grep :53"
fi

# ================================================================ 3. resolv.conf 检查
hdr "3. /etc/resolv.conf 是否指向缓存"
RC=/etc/resolv.conf
if [ -r "$RC" ]; then
  NS=$(grep -E '^\s*nameserver' "$RC" | awk '{print $2}' | tr '\n' ' ')
  info "nameserver: ${NS:-<无>}"
  if echo "$NS" | grep -q '127.0.0.1'; then
    pass "resolv.conf 使用本地 dnsmasq 缓存（127.0.0.1）"
  elif [ "$DNSMASQ_ACTIVE" = 1 ]; then
    warn "resolv.conf 绕过了正在运行的 dnsmasq 缓存（直接指向公网 DNS）" \
         "可能被 NetworkManager 改写；冷查询跳过缓存、直接打到抖动的 UDP/53，而非本地缓存" \
         "sudo sed -i 's/^nameserver.*/nameserver 127.0.0.1/' /etc/resolv.conf  # 或修 NM 的 dns= 配置；请先备份"
    ROOTCAUSE+=("resolv.conf 绕过了正在运行的 dnsmasq 缓存")
  else
    info "resolv.conf 直接使用公网 DNS（当前没有运行缓存可指向）"
  fi
  if grep -q 'timeout:' "$RC"; then
    pass "resolv.conf 设置了超时上限（$(grep -o 'options.*' "$RC")）"
  else
    warn "resolv.conf 没有 'options timeout:' 上限" \
         "单次冷查询未命中时可能卡好几秒、没有及早放弃" \
         "echo 'options timeout:2 attempts:2 single-request-reopen' | sudo tee -a /etc/resolv.conf"
  fi
else
  fail "无法读取 $RC" "" ""
fi

# ================================================================ 4. /etc/hosts 固定项
hdr "4. /etc/hosts 热门域名固定项"
if grep -q 'dns-hotfix' /etc/hosts 2>/dev/null; then
  pass "存在 dns-hotfix 段（固定 github/pypi/anthropic 等热门域名，即便 UDP/53 中断也能秒级解析）"
else
  info "/etc/hosts 中没有 dns-hotfix 段（可选；完全依赖 dnsmasq 缓存）"
fi

# ================================================================ 5. 直连出口（根因）
if [ "$QUICK" = 0 ]; then
  hdr "5. 直连公网出口健康度（通常的根本原因）"
  EGRESS_UDP=0
  # 绕过本地缓存、直接对公网 DNS 发 UDP/53，测的就是这条抖动链路本身
  if have dig; then
    OK=0
    for i in 1 2 3 4; do
      if dig +time=2 +tries=1 @223.5.5.5 github.com >/dev/null 2>&1; then OK=$((OK+1)); fi
    done
    if [ "$OK" -ge 3 ]; then
      pass "直连 UDP/53 到 223.5.5.5：${OK}/4 成功"; EGRESS_UDP=1
    elif [ "$OK" -ge 1 ]; then
      warn "直连 UDP/53 到 223.5.5.5：仅 ${OK}/4 成功（抖动中）" \
           "本机直连公网出口不稳定（几分钟内在 0/x <-> 正常间抖动）—— 属基础设施/上游层面，本地配置无法修复" \
           "依靠 dnsmasq 缓存 + /etc/hosts 掩盖；若长期抖动 => 上报网络团队"
      ROOTCAUSE+=("直连公网出口在抖动（上游）—— DNS 缓存只能掩盖")
    else
      fail "直连 UDP/53 到 223.5.5.5：0/4 —— 公网出口当前已断" \
           "共享 WAN 出口中断（同时也会断掉中转转发的 SSH）；非本地配置问题" \
           "若有可用代理/SSH 隧道则改走它（检测 6）；WAN 中断请上报网络团队"
      ROOTCAUSE+=("公网出口当前完全中断（WAN/上游）")
    fi
  else
    info "未安装 dig，跳过原始 UDP/53 探测"
  fi
  info "本机禁止 ping 公网（策略封禁）—— 不要把 ping 丢包当成故障信号"
fi

# ================================================================ 6. 代理 / ssh 隧道
hdr "6. 代理与 SSH 隧道出口（借用的，非基础设施）"
PROXY_ALIVE=""      # 可用代理 host:port（供检测 7 复用）
WORKING_PROXY_URL=""  # 带正确 scheme 的可用代理 URL（供修复建议用）
KNOWN_PROXY_PORTS="4780 7890 7891 11898"
# 当前在 listen 的本地端口里、不属于已知集合的「其它候选」——用来发现没写死的新隧道。
# 已知端口会逐项汇报（含 dead），其它候选只在确实可用时才显示，避免临时端口刷屏。
DISCOVERED=$( (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) \
  | grep -oE '(127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|\*):[0-9]+' | sed -E 's/.*:([0-9]+)$/\1/' \
  | awk '$1>1024 && $1!=53' | sort -un )
EXTRA_PORTS=$(comm -23 <(printf '%s\n' $DISCOVERED | sort -u) \
                       <(printf '%s\n' $KNOWN_PROXY_PORTS | sort -u))

# 环境变量声称的代理
PENV="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-${all_proxy:-${ALL_PROXY:-}}}}}}"
if [ -n "$PENV" ]; then
  info "环境变量代理 = $PENV"
  PSCHEME=$(echo "$PENV" | grep -oiE '^[a-z0-9]+://' | sed 's#://##' | tr 'A-Z' 'a-z')
  PHOST=$(echo "$PENV" | sed -E 's#^[a-zA-Z0-9]+://##; s#/.*$##'); PP=${PHOST##*:}; PH=${PHOST%:*}
  [ "$PH" = "$PHOST" ] && PH=127.0.0.1
  [ "$PH" = "localhost" ] && PH=127.0.0.1
  if [ "$QUICK" = 1 ] || ! have curl; then
    # 快速模式无法端到端验证：只能探 TCP，并明确标注「未验证代理可用性」
    if have nc && nc -z -w2 "$PH" "$PP" 2>/dev/null; then
      info "环境变量代理 $PH:$PP 的 TCP 端口在 listen（--quick 未验证代理是否真能用）"
    else
      fail "环境变量代理 $PH:$PP 的 TCP 端口都连不上" \
           "指向的隧道端口根本没在 listen；所有 curl/pip 会卡住再失败" \
           "unset http_proxy https_proxy all_proxy"
      ROOTCAUSE+=("http(s)_proxy 指向的端口未在 listen")
    fi
  else
    EKIND=$(proxy_kind "$PH" "$PP")
    if [ "$EKIND" = dead ]; then
      fail "环境变量代理 $PH:$PP 端口在 listen，但代理是死的（http/socks5 都不通）" \
           "这是本机最隐蔽的故障：ssh -L 隧道主人断开后端口仍 accept TCP，但后端已死。所有尊重 http(s)_proxy 的工具（curl/pip/git）会先卡住再超时——而 nc/端口扫描会误报「通」" \
           "unset http_proxy https_proxy all_proxy   # 或改指向下方分类为可用的端口"
      ROOTCAUSE+=("http(s)_proxy 指向的端口 TCP 开着但代理已死（隧道后端断开）")
    else
      pass "环境变量代理 $PH:$PP 端到端可用（类型：$EKIND）"
      PROXY_ALIVE="$PH:$PP"; WORKING_PROXY_URL="${EKIND}://$PH:$PP"
      # scheme 与实际类型不符：curl/pip 会按 scheme（或默认 http）去连，导致明明活着却用不了
      if [ "$EKIND" = socks5 ] && [ "$PSCHEME" != socks5 ] && [ "$PSCHEME" != socks5h ]; then
        warn "代理实际是 SOCKS5，但 http(s)_proxy 没写 socks5h:// 前缀（当前 scheme=${PSCHEME:-<无,默认http>}）" \
             "curl/pip 会把它当 HTTP 代理用 -> CONNECT 失败；端口活着也用不了" \
             "export all_proxy=socks5h://$PH:$PP; unset http_proxy https_proxy   # SOCKS 用 all_proxy 最稳"
        ROOTCAUSE+=("代理是 SOCKS5 但 env 当成 HTTP 用")
      fi
    fi
  fi
else
  info "环境变量里没有 http(s)_proxy / all_proxy"
fi

# 端到端分类（并行；--quick 跳过这步较慢的探测）。
# 已知端口逐项汇报；其它在 listen 的端口只在确实是可用代理时才列出（否则刷屏）。
remember_usable() {  # <port> <kind>
  USABLE="$USABLE $1($2)"
  if [ -z "$PROXY_ALIVE" ]; then
    PROXY_ALIVE="127.0.0.1:$1"; WORKING_PROXY_URL="$2://127.0.0.1:$1"
  fi
}
if [ "$QUICK" = 0 ] && have curl; then
  USABLE=""
  while read -r port kind; do
    [ -z "$port" ] && continue
    case "$kind" in
      http|socks5) printf "  ${G}可用${Z}  127.0.0.1:%s  ->  %s 代理（已知端口）\n" "$port" "$kind"
                   remember_usable "$port" "$kind" ;;
      dead) info "127.0.0.1:$port 在 listen，但代理是死的（端口开着 ≠ 代理能用）" ;;
      *)    info "已知代理端口 127.0.0.1:$port 当前没在 listen" ;;
    esac
  done <<EOF
$(classify_ports 6 $KNOWN_PROXY_PORTS)
EOF
  # 只有在已知端口全不可用时，才去全端口扫描找替代隧道（这步较慢，短超时投机探测）。
  if [ -z "$PROXY_ALIVE" ] && [ -n "$EXTRA_PORTS" ]; then
    info "已知端口无可用代理，扫描其它 listen 端口寻找替代隧道……"
    EXTRA_HIT=0
    while read -r port kind; do
      [ -z "$port" ] && continue
      case "$kind" in
        http|socks5) printf "  ${G}可用${Z}  127.0.0.1:%s  ->  %s 代理（自动发现的新隧道）\n" "$port" "$kind"
                     remember_usable "$port" "$kind"; EXTRA_HIT=$((EXTRA_HIT+1)) ;;
      esac
    done <<EOF
$(classify_ports 3 $EXTRA_PORTS)
EOF
    NEXTRA=$(printf '%s\n' $EXTRA_PORTS | grep -c .)
    info "另探测了 $NEXTRA 个其它 listen 端口，发现 ${EXTRA_HIT:-0} 个可用代理"
  fi
  if [ -n "$USABLE" ]; then
    info "可用代理端口：$USABLE  （ssh -L 隧道——主人一断开就消失）"
  else
    info "当前没有任何可用的本地代理端口（已端到端验证）"
  fi
elif [ "$QUICK" = 1 ]; then
  LIVE_TCP=$( for p in $KNOWN_PROXY_PORTS; do nc -z -w1 127.0.0.1 "$p" 2>/dev/null && echo "$p"; done | tr '\n' ' ')
  info "在 listen 的已知端口：${LIVE_TCP:-<无>}  （--quick 仅探 TCP，未验证代理是否真能用）"
fi

# ================================================================ 7. HTTP 端到端出口
if [ "$QUICK" = 0 ] && have curl; then
  hdr "7. HTTP(S) 端到端出口"
  probe_http() { # probe_http <url> [proxy]  —— 先试 http 代理，再退回 socks5（隧道常是 SOCKS）
    local url="$1" px="${2:-}" code
    if [ -n "$px" ]; then
      code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 -x "http://$px" "$url" 2>/dev/null)
      if [ "${code:-000}" = 000 ]; then
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 -x "socks5h://$px" "$url" 2>/dev/null)
      fi
    else
      code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 --noproxy '*' "$url" 2>/dev/null)
    fi
    echo "${code:-000}"
  }
  D_CODE=$(probe_http https://mirrors.aliyun.com)   # 国内源，直连应能通
  if [ "$D_CODE" = 200 ] || [ "$D_CODE" = 301 ] || [ "$D_CODE" = 302 ]; then
    pass "直连国内镜像（aliyun）HTTPS：HTTP $D_CODE"
  else
    warn "直连国内镜像失败（HTTP $D_CODE）" \
         "直连出口已劣化；国内源正常情况下不走代理也应能通" ""
  fi
  I_CODE=$(probe_http https://github.com)
  if [ "$I_CODE" = 200 ] || [ "$I_CODE" = 301 ] || [ "$I_CODE" = 302 ]; then
    pass "直连 github.com HTTPS：HTTP $I_CODE"
  else
    if [ -n "$PROXY_ALIVE" ]; then
      P_CODE=$(probe_http https://github.com "$PROXY_ALIVE")
      if [ "$P_CODE" = 200 ] || [ "$P_CODE" = 301 ] || [ "$P_CODE" = 302 ]; then
        warn "github.com 直连不通（HTTP $I_CODE），但经代理 $PROXY_ALIVE 可达（HTTP $P_CODE）" \
             "国际出口需要走代理；直连被 GFW/抖动挡住" \
             "export https_proxy=${WORKING_PROXY_URL:-http://$PROXY_ALIVE} http_proxy=\$https_proxy   # 已按实测类型选好 scheme"
      else
        fail "github.com 直连（$I_CODE）和经代理（$P_CODE）都不通" \
             "当前没有可用的国际出口路径" \
             "找一个在线的隧道主人，或改用国内镜像；若全 WAN 范围则上报"
      fi
    else
      info "github.com 直连不通（HTTP $I_CODE），且没有存活的代理可试 —— 没有隧道在线时属正常"
    fi
  fi
fi

# ================================================================ 8. 吞吐 / 带宽占用
hdr "8. 网络吞吐与带宽占用"
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
if [ -n "$IFACE" ] && [ -r "/sys/class/net/$IFACE/statistics/rx_bytes" ]; then
  R1=$(cat "/sys/class/net/$IFACE/statistics/rx_bytes"); T1=$(cat "/sys/class/net/$IFACE/statistics/tx_bytes")
  sleep 1
  R2=$(cat "/sys/class/net/$IFACE/statistics/rx_bytes"); T2=$(cat "/sys/class/net/$IFACE/statistics/tx_bytes")
  RXK=$(( (R2-R1)/1024 )); TXK=$(( (T2-T1)/1024 ))
  info "网卡 $IFACE 实时占用：↓ ${RXK} KB/s   ↑ ${TXK} KB/s  （1 秒采样，是全机所有进程的合计）"
  if [ "$RXK" -gt 80000 ] || [ "$TXK" -gt 80000 ]; then
    warn "网卡吞吐接近饱和（↓${RXK} ↑${TXK} KB/s）" \
         "可能有别的进程/用户在大量传输，共享带宽被占满——你的拉取会变慢但不是故障" \
         "看谁在占：sudo iftop -i $IFACE  或  ss -tnp | sort"
  fi
else
  info "无法读取网卡计数器，跳过实时速率"
fi
if have ss; then
  PEERS=$(ss -tn state established 2>/dev/null | awk 'NR>1{print $4}' | sed -E 's/:[0-9]+$//')
  TOTAL_CONN=$(printf '%s\n' "$PEERS" | grep -c .)
  EXT_PEERS=$(printf '%s\n' "$PEERS" | grep -vE '^(127\.|\[?::1)' )
  EXT_N=$(printf '%s\n' "$EXT_PEERS" | grep -c .)
  info "已建立 TCP 连接：${TOTAL_CONN} 条（外网 ${EXT_N} 条，其余为本机环回/各用户代理端口）"
  TOP=$(printf '%s\n' "$EXT_PEERS" | grep . | sort | uniq -c | sort -rn | head -3 \
        | awk '{printf "%s×%s  ", $2, $1}')
  [ -n "$TOP" ] && info "外网对端连接数 top3：$TOP"
fi
# 主动下行测速：拉一个 ~1.8MB 的 CN 镜像文件，测真实下载带宽（--quick 跳过）
if [ "$QUICK" = 0 ] && have curl; then
  SPEEDTEST_URL="https://mirrors.aliyun.com/ubuntu/dists/jammy/main/binary-amd64/Packages.gz"
  SP=$(curl -sS -o /dev/null --noproxy '*' --max-time 8 -w '%{speed_download}' "$SPEEDTEST_URL" 2>/dev/null)
  SP=${SP%%.*}; SP=${SP:-0}
  MBPS=$(awk "BEGIN{printf \"%.1f\", ${SP}/1048576}")
  if [ "$SP" -le 0 ]; then
    warn "直连下行测速失败（aliyun 镜像没下到数据）" \
         "直连出口当前劣化或在抖动；国内源正常应能下到" ""
  elif awk "BEGIN{exit !(${SP}<262144)}"; then   # < 0.25 MB/s
    warn "直连下行很慢（${MBPS} MB/s）" \
         "出口拥塞或被别的传输占满带宽（见上方网卡占用）；未必是断网" ""
  else
    pass "直连下行带宽：${MBPS} MB/s（CN 镜像 aliyun）"
  fi
  # 代理延迟：经推荐代理打一次 generate_204，看国际出口往返快不快
  if [ -n "$WORKING_PROXY_URL" ]; then
    PT=$(curl -sS -o /dev/null --max-time 8 -x "$WORKING_PROXY_URL" \
         -w '%{time_total}' "$PROXY_PROBE_URL" 2>/dev/null)
    PTMS=$(awk "BEGIN{printf \"%d\", ${PT:-0}*1000}")
    if [ "${PTMS:-0}" -gt 0 ]; then
      if [ "$PTMS" -gt 2000 ]; then
        info "经代理 ${WORKING_PROXY_URL##*//} 国际往返较慢（${PTMS}ms）—— 隧道拥塞或绕远"
      else
        info "经代理 ${WORKING_PROXY_URL##*//} 国际往返 ${PTMS}ms（正常）"
      fi
    fi
  fi
fi

# ================================================================ 9. LAN / 路由
hdr "9. LAN 网关与默认路由"
GW=$(ip route show default 2>/dev/null | awk '/default/{print $3; exit}')
if [ -n "$GW" ]; then
  pass "默认路由经 $GW"
  if have ping && ping -c1 -W2 "$GW" >/dev/null 2>&1; then
    pass "LAN 网关 $GW 可达（LAN 内允许 ICMP）"
  else
    info "$GW 无 ICMP 应答（可能被过滤；未必是故障）"
  fi
else
  fail "没有默认路由" "enp* 网卡的路由/DHCP 出问题" \
       "ip route; sudo dhclient -v <网卡>  # 或检查 NetworkManager"
fi

# ================================================================ 10. Tailscale 备份
if have tailscale; then
  hdr "10. Tailscale 备份 SSH 通道（不依赖中转机）"
  if tailscale status >/dev/null 2>&1; then
    if tailscale status 2>/dev/null | grep -q '100.78.35.56.*offline'; then
      warn "tailscaled 报告本机离线" \
           "控制面同步丢失 —— tailscaled 经 HTTPS_PROXY=127.0.0.1:7890（某用户隧道）连控制面；若它挂了则同步停止" \
           "sudo systemctl restart tailscaled   # 请先确认 7890 隧道在线，或修 proxy drop-in 配置"
      ROOTCAUSE+=("Tailscale 备份通道离线（控制面代理很可能已失效）")
    else
      pass "tailscale 在线 —— 备份通道 'ssh user@100.78.35.56' 可用"
    fi
  else
    info "已安装 tailscale 但无法获取状态（tailscaled 挂了？）"
  fi
fi

# ================================================================ 11. 主机 CPU 占用
hdr "11. 主机 CPU 占用与高耗进程/线程"
CORES=$(nproc 2>/dev/null || echo 0)
read -r L1 L5 L15 _ < /proc/loadavg 2>/dev/null
info "CPU 核数：${CORES}   负载(1/5/15min)：${L1} / ${L5} / ${L15}"
# 仅当真正过载（任务数 > 核数）才告警；高 CPU 在多核计算机上属常态，不是故障
if [ "$CORES" -gt 0 ] && awk "BEGIN{exit !(${L1:-0} > ${CORES})}"; then
  warn "1 分钟负载 ${L1} 超过核数 ${CORES}（CPU 过载）" \
       "可运行任务数多于核数，所有计算都在抢核——你的编译/测试会变慢" \
       "用下方 top 列表定位吃满 CPU 的进程；必要时降并发或排队（task-submit）"
  ROOTCAUSE+=("CPU 过载：1min 负载 ${L1} > 核数 ${CORES}")
fi
if [ "$QUICK" = 0 ] && have top; then
  # 取第二次采样（top 首次的 %CPU 是自开机均值，第二次才是瞬时区间值）
  PROC_TOP=$(top -bn2 -d 0.5 -w 512 2>/dev/null)
  CPU_LINE=$(printf '%s\n' "$PROC_TOP" | awk '/%Cpu/{l=$0} END{print l}')
  [ -n "$CPU_LINE" ] && info "整体 CPU：${CPU_LINE#*:}"
  info "CPU 占用 top5 进程（瞬时）："
  printf '%s\n' "$PROC_TOP" \
    | awk '/^[[:space:]]*PID[[:space:]]+USER/{s++; next} s>=2{print}' \
    | sort -k9 -nr | head -5 \
    | awk '{printf "        %5s%% CPU  PID %-8s %-10s %s\n", $9, $1, $2, $12}'
  info "CPU 占用 top5 线程（瞬时；TID + 所属进程）："
  top -bHn2 -d 0.5 -w 512 2>/dev/null \
    | awk '/^[[:space:]]*PID[[:space:]]+USER/{s++; next} s>=2{print}' \
    | sort -k9 -nr | head -5 \
    | while read -r tid user pr ni virt res shr st cpu rest; do
        tgid=$(awk '/^Tgid:/{print $2}' "/proc/$tid/status" 2>/dev/null)
        pcomm=$(cat "/proc/${tgid:-0}/comm" 2>/dev/null)
        comm=$(echo "$rest" | awk '{print $NF}')
        printf "        %5s%% CPU  TID %-8s %-10s %-16s <- 进程 %s/%s\n" \
               "$cpu" "$tid" "$user" "$comm" "${tgid:-?}" "${pcomm:-?}"
      done
else
  [ "$QUICK" = 1 ] && info "（--quick 跳过 top 采样；负载见上）"
fi

# ================================================================ 汇总
hdr "汇总"
printf "  ${G}%d 通过${Z}   ${Y}%d 警告${Z}   ${R}%d 失败${Z}\n" "$PASS_N" "$WARN_N" "$FAIL_N"
if [ "${#ROOTCAUSE[@]}" -gt 0 ]; then
  printf "\n  ${BOLD}最可能的根本原因：${Z}\n"
  for rc in "${ROOTCAUSE[@]}"; do printf "    ${R}•${Z} %s\n" "$rc"; done
elif [ "$WARN_N" = 0 ] && [ "$FAIL_N" = 0 ]; then
  printf "  ${G}网络看起来正常。${Z} 若仍觉得慢，多半是上游出口的瞬时冷缓存未命中（只能缓解、无法根治）。\n"
fi

if [ "$SHOW_FIX" = 1 ] && [ "${#FIXES[@]}" -gt 0 ]; then
  printf "\n${BOLD}建议的修复命令（执行前请先审阅；多数需要 sudo）：${Z}\n"
  # 去重并保持顺序
  printf '%s\n' "${FIXES[@]}" | awk '!seen[$0]++{print "  $ "$0}'
fi

printf "\n${D}提示：本机出口抖动属上游/基础设施层面。本地修复（dnsmasq、/etc/hosts、代理）只能掩盖，无法根治。${Z}\n"

if [ "$FAIL_N" -gt 0 ]; then exit 2
elif [ "$WARN_N" -gt 0 ]; then exit 1
else exit 0; fi
