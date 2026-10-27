ALTER TABLE puzzle_versions ADD COLUMN source_ref text NOT NULL DEFAULT '';
ALTER TABLE puzzle_versions ADD COLUMN scoring_artifact jsonb NOT NULL DEFAULT '{}';
CREATE TABLE wordbank_jobs (
 id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
 actor_id text NOT NULL, kind text NOT NULL CHECK(kind IN ('import','evaluation')),
 status text NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
 version bigint NOT NULL DEFAULT 1, progress bigint NOT NULL DEFAULT 0,
 input jsonb NOT NULL DEFAULT '{}', result jsonb NOT NULL DEFAULT '{}', error_code text,
 claim_boot text, claim_token text, lease_until timestamptz, annotations_locked_at timestamptz
);
CREATE INDEX wordbank_jobs_claim ON wordbank_jobs(status,created_at);
