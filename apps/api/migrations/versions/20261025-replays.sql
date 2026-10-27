CREATE TABLE replay_jobs (
 id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),actor_id text NOT NULL,
 session_id text NOT NULL UNIQUE REFERENCES live_sessions(id),status text NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
 state text NOT NULL,version bigint NOT NULL DEFAULT 1,input jsonb NOT NULL DEFAULT '{}',input_hash text NOT NULL,
 checkpoint jsonb NOT NULL DEFAULT '{}',cursor bigint NOT NULL DEFAULT 0,virtual_ms bigint NOT NULL DEFAULT 0,
 events_applied bigint NOT NULL DEFAULT 0,events_rejected bigint NOT NULL DEFAULT 0,boot_id text,claim_token text,lease_until timestamptz,error_code text
);
CREATE TABLE replay_events (
 id text PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now(),job_id text NOT NULL REFERENCES replay_jobs(id),ordinal bigint NOT NULL DEFAULT 0,
 source_id text NOT NULL,offset_ms bigint NOT NULL DEFAULT 0,status text NOT NULL,feedback_id text,reason_code text,
 UNIQUE(job_id,ordinal),UNIQUE(job_id,source_id)
);
CREATE TABLE message_trace_steps (
 id text PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now(),message_id text NOT NULL,stage text NOT NULL,state text NOT NULL,
 time timestamptz NOT NULL DEFAULT now(),reason_code text
);
CREATE INDEX message_trace_steps_message ON message_trace_steps(message_id,time,id);
ALTER TABLE guesses ADD COLUMN admitted_virtual_ms bigint;
