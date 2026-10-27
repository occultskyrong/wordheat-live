-- Query copies expire within 24h; anonymous final aggregates have a separate 90d lifetime.
CREATE TABLE IF NOT EXISTS analytics_snapshots (
 id text PRIMARY KEY, session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
 actor_id text NOT NULL REFERENCES operators(id) ON DELETE CASCADE, watermark bigint NOT NULL CHECK(watermark>=0),
 captured_at timestamptz NOT NULL, expires_at timestamptz NOT NULL, last_access timestamptz NOT NULL,
 payload jsonb, size_bytes bigint NOT NULL CHECK(size_bytes>=0 AND size_bytes<=4194304), reason text,
 pin_first timestamptz, pin_until timestamptz, pin_count integer NOT NULL DEFAULT 0 CHECK(pin_count>=0),
 CHECK(expires_at<=captured_at+interval '24 hours'), CHECK(reason IS NULL OR reason IN ('EXPIRED','EVICTED')));
CREATE INDEX IF NOT EXISTS analytics_snapshot_scope ON analytics_snapshots(actor_id,session_id,captured_at);
CREATE TABLE IF NOT EXISTS analytics_final_aggregates (
 session_id text PRIMARY KEY REFERENCES live_sessions(id) ON DELETE CASCADE, payload jsonb NOT NULL, expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_session_facts (
 session_id text PRIMARY KEY REFERENCES live_sessions(id) ON DELETE CASCADE, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_round_facts (
 round_id text PRIMARY KEY REFERENCES rounds(id) ON DELETE CASCADE,
 session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_attempt_observations (
 id text PRIMARY KEY, session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE, payload jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS analytics_attempt_scope ON analytics_attempt_observations(session_id);
CREATE TABLE IF NOT EXISTS analytics_latency_observations (
 guess_id text PRIMARY KEY REFERENCES guesses(id) ON DELETE CASCADE,
 session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_exports (id text PRIMARY KEY,
 actor_id text NOT NULL REFERENCES operators(id) ON DELETE CASCADE, expires_at timestamptz NOT NULL);
