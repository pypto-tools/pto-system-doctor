---
name: pto-system-doctor
description: Diagnose shared Linux host network and disk-health problems with pto-system-doctor. Use when an AI needs to investigate slow or broken DNS, proxies, HTTP egress, routes, Tailscale, network throughput, CPU pressure, disk capacity, disk alert configuration, Feishu disk reports, or the tool's systemd timers; also use when installing, configuring, or checking this tool.
---

# PTO System Doctor

Use the installed `pto-system-doctor` command when available. From a source checkout, use
`./pto-system-doctor`. Keep diagnosis read-only unless the user explicitly authorizes a change.

## Choose the workflow

### Diagnose network problems

1. Start with `pto-system-doctor network --quick --no-color` for a low-latency snapshot.
2. Treat exit code `0` as healthy, `1` as warnings, and `2` as at least one failure. Do not mistake a
   diagnostic exit code for a tool crash.
3. Run `pto-system-doctor network --no-color` only when deeper egress, proxy, or bandwidth checks are
   useful. The full check makes outbound requests and downloads a small test file.
4. Add `--fix` only to print consolidated repair suggestions. It does not apply them.
5. Explain the observed evidence and distinguish host configuration faults from upstream instability.
   Do not execute printed repair commands without separate authorization.

Read `docs/NETWORK_RUNBOOK.md` from the repository or installed `app/` only when detailed host-specific
background or repair guidance is needed.

### Inspect disk space without sending messages

Use read-only system commands such as `df -h` and scoped `du` commands. Do not use
`pto-system-doctor disk report` as a harmless inspection command: it always sends a Feishu message.
Avoid broad or expensive `du` scans unless the user requests consumer analysis.

### Send or test disk notifications

- Run `pto-system-doctor disk report` only when the user explicitly requests a Feishu disk report or an
  end-to-end send test.
- Run `pto-system-doctor disk alert` only when the user explicitly requests the configured alert check or
  when an already-authorized timer workflow calls for it. It sends only below threshold and honors the
  cooldown state.
- Never display, log, or reproduce `FEISHU_WEBHOOK`.
- Summarize send success without quoting credentials or complete HTTP payloads.

### Manage installation and timers

- The canonical repository and deployment directory are both named `pto-system-doctor`.
- The installed layout is `/home/pto-tools/pto-system-doctor/{app,config,state,logs,tmp}` and the only
  public command is `/usr/local/bin/pto-system-doctor`.
- Use `sudo ./install.sh --init-config` for a first installation and `sudo ./install.sh` for upgrades.
  Installation updates only `app/`; it must preserve `config/` and `state/` and must not run diagnostics,
  scan disks, send Feishu messages, or enable timers.
- Treat `pto-system-doctor systemd status` as read-only. Require explicit authorization and root for
  `install`, `enable`, `disable`, or `uninstall`.
- Before replacing legacy disk-monitor timers, inspect their enabled/active state. Enable the new timers
  before disabling old ones, then verify there is exactly one report timer and one alert timer to prevent
  missed or duplicate Feishu messages.

## Configuration and reporting

Use `/home/pto-tools/pto-system-doctor/config/system-doctor.conf` when installed and
`runtime/config/system-doctor.conf` in source mode. Preserve mode `0600`. Never commit the real config.
Report the command used, exit code, key PASS/WARN/FAIL evidence, and any state change. Mention when a full
network check generated traffic or when a disk action sent an external message.
