BEGIN;

ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_job_type_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_job_type_check
    CHECK (job_type IN (
        'checkin',
        'weekly_summary',
        'oob_review',
        'watch_item_due',
        'heartbeat',
        'deferred_turn',
        'scheduled_task'
    ));

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_pending_scheduled_task_lookup
    ON scheduled_jobs (user_id, scheduled_for)
    WHERE status = 'pending' AND job_type = 'scheduled_task';

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_jobs_pending_scheduled_task_source_job
    ON scheduled_jobs ((context->>'source_job_id'))
    WHERE status = 'pending'
      AND job_type = 'scheduled_task'
      AND context->>'source_job_id' IS NOT NULL;

COMMIT;
