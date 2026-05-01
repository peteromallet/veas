# Scheduler Operations

The scheduler runs in-process with the FastAPI app. This keeps Railway
deployment simple: one service owns transport ingress, agentic turns, and due
scheduled jobs, all backed by the same Postgres tables. Disable it with the
existing `SCHEDULER_ENABLED=false` setting only when another deployed app
instance is intentionally taking over scheduler work.

## Runtime Model

`app.main` starts `ScheduledJobWorker.run_forever()` during FastAPI lifespan
startup when `SCHEDULER_ENABLED` is true. Each tick polls every
`SCHEDULER_POLL_INTERVAL_S` seconds and claims up to `SCHEDULER_BATCH_SIZE`
pending rows from `scheduled_jobs`.

The claim query uses `SELECT ... FOR UPDATE SKIP LOCKED` inside a single
`UPDATE ... RETURNING` statement. That is the horizontal-safety boundary:
multiple Railway instances may tick at the same time, but a due job row can be
claimed by only one worker. If a worker dies after claiming a row, startup
recovery clears stale `claimed_at` state for recent due jobs, marks 1-24 hour
late jobs as delayed, and cancels jobs more than 24 hours stale.

When global pause is enabled, the worker claims only `heartbeat` jobs. User
facing jobs remain pending unless `/pause` explicitly supersedes them.

## Heartbeat

Heartbeat is a `scheduled_jobs` row of type `heartbeat`, not a separate timer.
When a heartbeat job fires, the worker writes the normal scheduler heartbeat log
line and seeds the next heartbeat using `HEARTBEAT_INTERVAL_HOURS`. The worker
keeps heartbeat active during global pause because it is operational telemetry,
not mediation.

Alerting for missing heartbeat is external to the app. On Railway, configure a
log-based or cron-style monitor that expects a scheduler heartbeat log within
the `HEARTBEAT_INTERVAL_HOURS` cadence plus deployment slack. Alert if the log
goes missing while `/health` still reports healthy, because that indicates the
web process is up but scheduler work may not be running.

## Weekly Summaries

Weekly summaries are seeded on startup and after `/resume` from durable per-user
fields:

- `users.weekly_summary_day`
- `users.weekly_summary_time`
- `users.timezone`

The recurrence calculation converts the configured local day and time to an
absolute UTC `scheduled_for` instant. The default configuration values
`WEEKLY_SUMMARY_DEFAULT_DAY` and `WEEKLY_SUMMARY_DEFAULT_TIME` are only defaults
for user rows; recurrence should not depend on a superseded pending job's JSON
context.

## Pause And Resume

`/pause` sets `system_state.global_pause`, supersedes pending user-facing jobs,
and preserves heartbeat jobs. `/resume` clears the global pause and reseeds
weekly summaries from the durable user fields above. Stored paused inbound
messages are not replayed automatically.
