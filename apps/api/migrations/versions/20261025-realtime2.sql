CREATE TABLE IF NOT EXISTS guess_rejections (
 id varchar PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
 session_id varchar NOT NULL REFERENCES live_sessions(id),
 round_id varchar REFERENCES rounds(id), reason_code varchar NOT NULL,
 source_id varchar NOT NULL, UNIQUE(session_id,source_id)
);
