-- Separate anonymous 30-day trace payload from the 90-day business aggregate.
-- Existing released revisions remain unchanged. Upgrade before enabling the transaction-tail port.
CREATE TABLE IF NOT EXISTS analytics_round_traces (
 round_id text PRIMARY KEY REFERENCES rounds(id) ON DELETE CASCADE,
 session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
 trace_ref text NOT NULL UNIQUE, expires_at timestamptz NOT NULL, payload jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS analytics_round_trace_expiry ON analytics_round_traces(expires_at);
CREATE INDEX IF NOT EXISTS analytics_round_fact_trace_ref ON analytics_round_facts ((payload->>'trace_ref'));
