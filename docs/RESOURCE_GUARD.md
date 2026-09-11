# Resource Guard Design

## Runtime isolation

The repository and public CLI are unified, but runtime failure domains remain
separate. `pto-system-doctor-resource.service` polls host counters and performs
local protection. Disk capacity checks run from existing timers. Resource and
disk producers append small JSON events to a local spool;
`pto-system-doctor-alert.service` alone performs network delivery.

## Signals and actions

`MemAvailable` is the memory waterline because it includes reclaimable cache.
Deep attribution reads `smaps_rollup`; victim memory is `Pss_Anon + SwapPss`,
while `Pss_File` and `Pss_Shmem` remain audit fields. `/proc/vmstat` counters are
converted to rates, never compared as lifetime totals.

File handle pressure uses `file-nr` absolute limits. Its ratio is ignored when
the kernel advertises an effectively unlimited `file-max`. Thread pressure uses
the runnable/total entity count, `threads-max`, and the current cgroup's finite
`pids.current/pids.max` when available; a numeric PID value is never treated as
current process usage. Disk latency and utilization come from
successive `diskstats` snapshots. Sustained IO can pause scheduling but never
selects a victim.

The policy has `observe`, `pause`, and `kill` modes. Only the taskqueue adapter
may mutate task state. It enters maintenance before a kill and delegates cleanup
to `pto-task --kill`; it never sends raw signals. It confirms that the running
marker and task processes are gone before recording success.

`MAINTENANCE_MIN_LEVEL` controls when task submission is interrupted. It
defaults to `critical`, so pause-level signals such as sustained IO or direct
reclaim remain visible in status, audit, and alerts but do not enter task-submit
maintenance. Operators can explicitly set it to `pause` for a more conservative
host.

Resource-originated notifications can be paused independently with
`ALERT_ENABLED=false`. This leaves resource sampling and protection running and
does not disable the shared alert worker used by disk notifications.

Every normalized resource event is retained in the local alert event log.  The
worker defaults to `FEISHU_MIN_SEVERITY=critical`: critical events are delivered
to one configured user by a self-built application bot, while lower severities
are not sent as chat messages.  When a Docx or Wiki target is configured, all
severities are appended there, including recovery.  Private-message and document
delivery checkpoints are persisted separately so retrying one sink does not
duplicate a sink that already succeeded.

Events carry conservative operator guidance derived from their signals.  Advice
starts with attribution and current-state checks, keeps task cleanup behind
`pto-task`, never recommends raw signals, and does not suggest raising kernel
limits before finding the resource owner.

## Storm and deadlock protection

Kills are bounded per incident and per rolling time window. After a confirmed
kill, the daemon waits for a configured `MemAvailable` improvement. If pressure
persists without enough improvement, automatic kills lock and an alert is
queued. A failed `pto-task` cleanup also locks further kills. D-state processes
are recorded; zombies are audit information because zombies hold no address
space or file descriptors and cannot be force-reaped by an unrelated daemon.

The daemon never releases maintenance unless its ownership state and the exact
taskqueue marker agree. A single-instance lock prevents duplicate action loops.
Self RSS/CPU limits suspend automatic actions and reduce polling frequency.

## Exemptions

Leases apply to one known pending/running task, expire automatically, and stop
protecting the task at their floor. Protected-user rules also yield at the
emergency memory floor. A time-bounded `override` freezes all automatic pause
and kill actions for attended operations; expiry restores normal policy but
does not indiscriminately kill every task started during the window.

## Rollout

Run `observe` first and validate signals and alert volume. Enable `pause` next
and inspect queue buildup. Enable `kill` only after test workloads demonstrate
correct attribution, task cleanup, NPU lock release, rate limiting, and recovery.
