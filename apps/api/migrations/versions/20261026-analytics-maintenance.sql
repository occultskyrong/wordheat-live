-- Durable local diagnostics and bounded final materialization; no cross-service imports.
CREATE TABLE IF NOT EXISTS analytics_delivery_observations (
 session_id text PRIMARY KEY REFERENCES live_sessions(id) ON DELETE CASCADE,
 observation_id text NOT NULL, revision bigint NOT NULL CHECK(revision>=0), payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_final_rows (
 session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
 section text NOT NULL CHECK(section IN ('minutes','rounds')), row_key text NOT NULL,
 payload jsonb NOT NULL, size_bytes bigint NOT NULL CHECK(size_bytes>=0),
 PRIMARY KEY(session_id,section,row_key));
CREATE INDEX IF NOT EXISTS analytics_maintenance_pending ON analytics_session_facts
 ((payload->>'materialization_pending_reason')) WHERE payload ? 'materialization_pending_reason';
CREATE TABLE IF NOT EXISTS analytics_maintenance_schedule (
 session_id text PRIMARY KEY REFERENCES live_sessions(id) ON DELETE CASCADE, next_run timestamptz NOT NULL);

CREATE TABLE IF NOT EXISTS analytics_event_retention (
 session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
 audience text NOT NULL CHECK(audience IN ('DISPLAY','STUDIO')), through_seq bigint NOT NULL,
 payload_hash text NOT NULL, PRIMARY KEY(session_id,audience));
