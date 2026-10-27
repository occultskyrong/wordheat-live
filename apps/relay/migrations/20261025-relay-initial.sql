CREATE SCHEMA IF NOT EXISTS relay;
CREATE TABLE IF NOT EXISTS relay.schema_versions(version integer PRIMARY KEY);
INSERT INTO relay.schema_versions VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS relay.room_owners(
 app_id text NOT NULL,room_id text NOT NULL,installation_id text NOT NULL,session_id text NOT NULL,
 lease_id text NOT NULL,owner_generation bigint NOT NULL,delivery_generation bigint NOT NULL DEFAULT 0,
 connection_nonce text,expires_at timestamptz NOT NULL,authorization_expires_at timestamptz NOT NULL,
 run_purpose text NOT NULL CHECK(run_purpose IN ('test_live','production')),window_epoch bigint NOT NULL DEFAULT 0,
 last_trusted_at timestamptz NOT NULL,PRIMARY KEY(app_id,room_id));
CREATE TABLE IF NOT EXISTS relay.relay_leases(
 lease_id text PRIMARY KEY,app_id text NOT NULL,room_id text NOT NULL,owner_generation bigint NOT NULL,
 installation_id text NOT NULL,expires_at timestamptz NOT NULL,revoked_at timestamptz,
 FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_streams(
 app_id text NOT NULL,room_id text NOT NULL,stream_id text NOT NULL UNIQUE,last_seq bigint NOT NULL DEFAULT 0,
 payload_bytes bigint NOT NULL DEFAULT 0,retained_from bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(app_id,room_id),FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_windows(
 app_id text NOT NULL,room_id text NOT NULL,epoch bigint NOT NULL,round_id text NOT NULL,
 original_generation bigint NOT NULL,version integer NOT NULL,state text NOT NULL,
 opened_at timestamptz NOT NULL,deadline timestamptz NOT NULL,remaining_ms bigint,
 close_seq bigint,barrier_seq bigint,reason text,winner_seq bigint,
 PRIMARY KEY(app_id,room_id,epoch),FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_frames(
 app_id text NOT NULL,room_id text NOT NULL,stream_seq bigint NOT NULL,body bytea,
 immutable_sha256 text NOT NULL,received_at timestamptz NOT NULL,original_generation bigint NOT NULL,
 window_epoch bigint NOT NULL,eligible boolean NOT NULL,
 PRIMARY KEY(app_id,room_id,stream_seq),FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_inbox(
 app_id text NOT NULL,room_id text NOT NULL,event_type text NOT NULL,source_msg_id text NOT NULL,
 source_hash text NOT NULL,raw_hash text NOT NULL,raw_body bytea,stream_seq bigint NOT NULL,received_at timestamptz NOT NULL,
 PRIMARY KEY(app_id,room_id,event_type,source_msg_id),
 FOREIGN KEY(app_id,room_id,stream_seq) REFERENCES relay.relay_frames ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_checkpoints(
 app_id text NOT NULL,room_id text NOT NULL,installation_id text NOT NULL,persisted_seq bigint NOT NULL DEFAULT 0,
 delivered_seq bigint NOT NULL DEFAULT 0,connection_nonce text NOT NULL,
 PRIMARY KEY(app_id,room_id,installation_id),FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.delivery_stages(
 app_id text NOT NULL,room_id text NOT NULL,stream_seq bigint NOT NULL,stage text NOT NULL,receipt_id text NOT NULL,
 recorded_at timestamptz NOT NULL,PRIMARY KEY(app_id,room_id,stream_seq,stage),
 FOREIGN KEY(app_id,room_id,stream_seq) REFERENCES relay.relay_frames ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS relay.relay_outbox(
 job_id text PRIMARY KEY,app_id text NOT NULL,room_id text NOT NULL,stream_seq bigint,kind text NOT NULL,
 status text NOT NULL DEFAULT 'pending',attempts integer NOT NULL DEFAULT 0,last_error text,
 UNIQUE(app_id,room_id,stream_seq,kind),FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
CREATE OR REPLACE FUNCTION relay.reject_frame_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.app_id<>OLD.app_id OR NEW.room_id<>OLD.room_id OR NEW.stream_seq<>OLD.stream_seq
 OR NEW.immutable_sha256<>OLD.immutable_sha256 OR NEW.received_at<>OLD.received_at
 OR NEW.original_generation<>OLD.original_generation OR NEW.window_epoch<>OLD.window_epoch
 OR NEW.eligible<>OLD.eligible OR (NEW.body IS NOT NULL AND NEW.body IS DISTINCT FROM OLD.body)
 THEN RAISE EXCEPTION 'IMMUTABLE_FRAME'; END IF; RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_frame ON relay.relay_frames;
CREATE TRIGGER immutable_frame BEFORE UPDATE ON relay.relay_frames FOR EACH ROW EXECUTE FUNCTION relay.reject_frame_mutation();

ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS revoked_at timestamptz;
