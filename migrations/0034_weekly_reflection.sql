BEGIN;

UPDATE scheduled_jobs
   SET status = 'cancelled',
       cancellation_reason = 'weekly_summary retired in favor of weekly_reflection scheduled_task',
       updated_at = now()
 WHERE job_type = 'weekly_summary'
   AND status = 'pending';

ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_job_type_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_job_type_check
    CHECK (job_type IN (
        'checkin',
        'oob_review',
        'watch_item_due',
        'heartbeat',
        'deferred_turn',
        'scheduled_task'
    ));

DROP INDEX IF EXISTS idx_users_weekly_summary_schedule;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_weekly_summary_day_check;
ALTER TABLE users
    DROP COLUMN IF EXISTS weekly_summary_enabled,
    DROP COLUMN IF EXISTS weekly_summary_day,
    DROP COLUMN IF EXISTS weekly_summary_time;

COMMIT;
