BEGIN;

CREATE TABLE IF NOT EXISTS eval_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at timestamptz NOT NULL DEFAULT now(),
    prompt_version text NOT NULL,
    scenarios_passed integer NOT NULL DEFAULT 0 CHECK (scenarios_passed >= 0),
    scenarios_failed integer NOT NULL DEFAULT 0 CHECK (scenarios_failed >= 0),
    total_cost_usd numeric(10, 4) NOT NULL DEFAULT 0 CHECK (total_cost_usd >= 0),
    git_sha text,
    notes text
);

CREATE TABLE IF NOT EXISTS eval_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    scenario_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('pass', 'fail', 'skipped')),
    judge_verdicts jsonb NOT NULL DEFAULT '[]'::jsonb,
    tool_calls jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_run_at ON eval_runs (run_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_results_run_id ON eval_results (run_id, scenario_name);

ALTER TABLE eval_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE eval_results ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_eval_runs ON eval_runs FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_eval_results ON eval_results FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
