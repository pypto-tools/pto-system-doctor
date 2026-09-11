---
name: pto-system-doctor
description: Diagnose and safely operate shared Linux host network, disk, resource-pressure, and alert workflows with pto-system-doctor. Use for DNS/proxy/route problems, disk capacity, Feishu alerts, MemAvailable/reclaim/FD/thread/IO pressure, managed-task victim previews, resource leases or overrides, audit history, installation, configuration, or systemd units.
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
`pto-system-doctor disk report` as a harmless inspection command: it queues an external report, or
directly sends one under a compatibility configuration.
Avoid broad or expensive `du` scans unless the user requests consumer analysis.

### Send or test disk notifications

- Run `pto-system-doctor disk report` only when the user explicitly requests a Feishu disk report or an
  end-to-end queue/send test.
- Run `pto-system-doctor disk alert` only when the user explicitly requests the configured alert check or
  when an already-authorized timer workflow calls for it. It sends only below threshold and honors the
  cooldown state. Queue delivery is not proof that the alert worker sent the message.
- Never display, log, or reproduce `FEISHU_WEBHOOK`, `FEISHU_APP_SECRET`, or
  access tokens.
- Summarize send success without quoting credentials or complete HTTP payloads.

### Inspect resource pressure

1. Run `pto-system-doctor resource status` first. It is lightweight and read-only.
2. Run `resource evaluate` or `resource victims` only when task attribution is needed; they scan
   process metadata, file descriptors, and `smaps_rollup` and therefore cost more.
3. Report `MemAvailable`, reclaim rates, FD/thread/IO signals, action mode, candidate task attribution,
   active lease/override, and whether an action actually occurred.
4. Treat diagnostic exit codes 1/2 as pressure levels, not necessarily program failures.

Read `docs/RESOURCE_GUARD.md` when changing thresholds or reasoning about automatic actions.

### Mutate resource protection

- Treat `ACTION_MODE=pause` and `ACTION_MODE=kill`, daemon activation, lease/override changes, and
  task termination as production-impacting. Require explicit authorization and root.
- Never send raw signals to a task. Let the resource module call `pto-task --kill` so task state,
  descendants, and NPU locks are reconciled.
- Use a lease for one exceptional task. Use `override enable` only for attended emergency operations,
  with a short duration, concrete reason, and the acknowledgement flag.
- Never remove taskqueue maintenance unless resource state proves this exact incident owns it.

### Inspect alerts

Use `pto-system-doctor alert status` to inspect the local spool. `alert worker --once` performs an
external Feishu write when a private-message or document target is configured and therefore requires
user authorization. By default only critical events are private-messaged; lower severities remain in
the local event log and may be appended to the configured Docx/Wiki. Resource protection must not wait
for the worker or network delivery. Treat a queued event, a successful private message, and a successful
document append as three distinct states.

### Manage installation and timers

- The canonical repository and deployment directory are both named `pto-system-doctor`.
- The installed layout is `/home/pypto-tools/pto-system-doctor/{app,config,state,logs,tmp}` and the only
  public command is `/usr/local/bin/pto-system-doctor`.
- Use `sudo ./install.sh --init-config` for a first installation and `sudo ./install.sh` for upgrades.
  Installation updates only `app/`; it must preserve `config/` and `state/` and must not run diagnostics,
  scan disks, send Feishu messages, or enable timers/services.
- Treat `pto-system-doctor systemd status [disk|resource|alert|all]` as read-only. Require explicit
  authorization and root for `install`, `enable`, `disable`, or `uninstall`. Enable resource, alert,
  and disk scopes separately during rollout.
- Before replacing legacy disk-monitor timers, inspect their enabled/active state. Enable the new timers
  before disabling old ones, then verify there is exactly one report timer and one alert timer to prevent
  missed or duplicate Feishu messages.

## Configuration and reporting

Use `/home/pypto-tools/pto-system-doctor/config/system-doctor.conf` when installed and
`runtime/config/system-doctor.conf` in source mode. Preserve mode `0600`. Never commit the real config.
The asynchronous worker uses `FEISHU_APP_ID`/`FEISHU_APP_SECRET` with an application bot;
`FEISHU_RECEIVE_ID` selects the private recipient and `FEISHU_DOCX_TOKEN` or `FEISHU_WIKI_TOKEN`
selects the optional event-log document. The legacy group webhook is only for old direct disk delivery.
Resource configuration is the separate `config/resource-guard.conf`; preserve its strict data-only
format. Report the command used, exit code, key evidence, queued/external notifications, and every state
change. Mention when a full network check generated traffic or a worker sent an external message.
