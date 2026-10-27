CREATE TABLE IF NOT EXISTS relay.trusted_objects(
 object_id text PRIMARY KEY,issuer_id text NOT NULL,issuer_revision bigint NOT NULL,
 type text NOT NULL,digest text NOT NULL,envelope jsonb NOT NULL,payload jsonb NOT NULL,
 revoked boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS relay.trusted_issuers(issuer_id text PRIMARY KEY,revision bigint NOT NULL);
CREATE TABLE IF NOT EXISTS relay.revocation_state(id text PRIMARY KEY,revision bigint NOT NULL,object_ids jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS relay.key_bundles(
 object_id text PRIMARY KEY,ordinal bigint NOT NULL UNIQUE,digest text NOT NULL,envelope jsonb NOT NULL,
 root_chain jsonb NOT NULL,native_command_id text NOT NULL,confirmation_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS relay.device_pairings(
 pairing_id text PRIMARY KEY,app_id text NOT NULL,room_id text NOT NULL,room_binding_id text NOT NULL,
 installation_id text NOT NULL,actor_id text NOT NULL,device_public_key bytea NOT NULL,
 challenge bytea NOT NULL,status text NOT NULL,expires_at timestamptz NOT NULL,
 room_verified_until timestamptz NOT NULL,verification_id text NOT NULL,provider_test boolean NOT NULL,
 device_token_hash text,device_token_expires_at timestamptz,revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS uq_device_token_hash ON relay.device_pairings(device_token_hash) WHERE device_token_hash IS NOT NULL;
ALTER TABLE relay.device_pairings ADD COLUMN IF NOT EXISTS previous_token_hash text;
ALTER TABLE relay.device_pairings ADD COLUMN IF NOT EXISTS previous_token_expires_at timestamptz;
CREATE TABLE IF NOT EXISTS relay.device_token_challenges(
 challenge_id text PRIMARY KEY,pairing_id text NOT NULL,challenge bytea NOT NULL,
 expires_at timestamptz NOT NULL,consumed_at timestamptz);
CREATE TABLE IF NOT EXISTS relay.pairing_intent_consumptions(
 object_id text PRIMARY KEY,digest text NOT NULL,device_key_sha256 text NOT NULL,pairing_id text NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS relay.freshness_challenges(
 pairing_id text NOT NULL,boot_id text NOT NULL,connection_nonce text NOT NULL,request_nonce text NOT NULL,
 consumed_at timestamptz NOT NULL,PRIMARY KEY(pairing_id,boot_id,connection_nonce,request_nonce));
CREATE TABLE IF NOT EXISTS relay.control_results(
 app_id text NOT NULL,room_id text NOT NULL,installation_id text NOT NULL,session_id text NOT NULL,
 operation text NOT NULL,command_id text NOT NULL,request_hash text NOT NULL,response jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(app_id,room_id,installation_id,session_id,operation,command_id));
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS authorization_id text;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS candidate_digest text;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS live_profile text;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS grant_digest text;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS room_binding_id text;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS revocation_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS max_session_duration_ms bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS relay.authorization_sessions(
 app_id text NOT NULL,room_id text NOT NULL,installation_id text NOT NULL,session_id text NOT NULL,
 authorization_id text NOT NULL,started_at timestamptz NOT NULL,deadline_at timestamptz NOT NULL,
 PRIMARY KEY(app_id,room_id,installation_id,session_id,authorization_id));
CREATE TABLE IF NOT EXISTS relay.raw_batches(
 app_id text NOT NULL,room_id text NOT NULL,raw_hash text NOT NULL,raw_body bytea,
 received_at timestamptz NOT NULL,PRIMARY KEY(app_id,room_id,raw_hash),
 FOREIGN KEY(app_id,room_id) REFERENCES relay.room_owners ON DELETE CASCADE);
ALTER TABLE relay.relay_inbox ADD COLUMN IF NOT EXISTS source_schema_version integer NOT NULL DEFAULT 0;
ALTER TABLE relay.relay_inbox ADD COLUMN IF NOT EXISTS source_metadata jsonb NOT NULL DEFAULT '{}';
ALTER TABLE relay.relay_streams ADD COLUMN IF NOT EXISTS retained_messages bigint NOT NULL DEFAULT 0;
UPDATE relay.relay_streams st SET retained_messages=(SELECT count(*) FROM relay.relay_frames f WHERE f.app_id=st.app_id AND f.room_id=st.room_id AND f.body IS NOT NULL);
ALTER TABLE relay.room_owners ADD COLUMN IF NOT EXISTS device_binding_id text;
ALTER TABLE relay.relay_frames ADD COLUMN IF NOT EXISTS stream_id text;
UPDATE relay.relay_frames f SET stream_id=CASE WHEN f.body IS NOT NULL THEN (convert_from(f.body,'UTF8')::jsonb)->>'stream_id' ELSE st.stream_id END FROM relay.relay_streams st WHERE f.app_id=st.app_id AND f.room_id=st.room_id AND f.stream_id IS NULL;
ALTER TABLE relay.relay_frames ALTER COLUMN stream_id SET NOT NULL;
CREATE TABLE IF NOT EXISTS relay.stream_segments(
 stream_id text PRIMARY KEY,app_id text NOT NULL,room_id text NOT NULL,start_seq bigint NOT NULL,
 end_seq bigint,installation_id text NOT NULL,session_id text NOT NULL,owner_generation bigint NOT NULL,
 previous_stream_id text,bootstrap_seq bigint,bootstrap_command_id text,reason text NOT NULL,
 rpo jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,room_id,installation_id,session_id,bootstrap_command_id));
INSERT INTO relay.stream_segments(stream_id,app_id,room_id,start_seq,installation_id,session_id,owner_generation,reason)
 SELECT st.stream_id,st.app_id,st.room_id,1,o.installation_id,o.session_id,o.owner_generation,'LEGACY_CONTINUOUS_STREAM' FROM relay.relay_streams st JOIN relay.room_owners o USING(app_id,room_id) ON CONFLICT DO NOTHING;
ALTER TABLE relay.relay_checkpoints ADD COLUMN IF NOT EXISTS stream_id text;
ALTER TABLE relay.relay_checkpoints ADD COLUMN IF NOT EXISTS session_id text;
UPDATE relay.relay_checkpoints cp SET stream_id=st.stream_id,session_id=sg.session_id FROM relay.relay_streams st JOIN relay.stream_segments sg USING(stream_id) WHERE cp.app_id=st.app_id AND cp.room_id=st.room_id AND cp.stream_id IS NULL;
CREATE TABLE IF NOT EXISTS relay.checkpoint_history(
 app_id text NOT NULL,room_id text NOT NULL,installation_id text NOT NULL,session_id text NOT NULL,
 stream_id text NOT NULL,persisted_seq bigint NOT NULL,delivered_seq bigint NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(app_id,room_id,installation_id,session_id,stream_id));
CREATE TABLE IF NOT EXISTS relay.delivery_contexts(
 app_id text NOT NULL,room_id text NOT NULL,stream_seq bigint NOT NULL,stream_id text NOT NULL,
 context jsonb NOT NULL,event_type text NOT NULL,eligible boolean NOT NULL,
 PRIMARY KEY(app_id,room_id,stream_seq),FOREIGN KEY(app_id,room_id,stream_seq) REFERENCES relay.relay_frames ON DELETE CASCADE);
CREATE OR REPLACE FUNCTION relay.reject_frame_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.app_id<>OLD.app_id OR NEW.room_id<>OLD.room_id OR NEW.stream_seq<>OLD.stream_seq
 OR NEW.stream_id IS DISTINCT FROM OLD.stream_id
 OR NEW.immutable_sha256<>OLD.immutable_sha256 OR NEW.received_at<>OLD.received_at
 OR NEW.original_generation<>OLD.original_generation OR NEW.window_epoch<>OLD.window_epoch
 OR NEW.eligible<>OLD.eligible OR (NEW.body IS NOT NULL AND NEW.body IS DISTINCT FROM OLD.body)
 THEN RAISE EXCEPTION 'IMMUTABLE_FRAME'; END IF; RETURN NEW;
END $$;
ALTER TABLE relay.relay_windows ADD COLUMN IF NOT EXISTS open_seq bigint;
ALTER TABLE relay.relay_windows ADD COLUMN IF NOT EXISTS last_control_seq bigint;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}';
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS response jsonb NOT NULL DEFAULT '{}';
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS claim_token text;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS cycle_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS retry_cycle integer NOT NULL DEFAULT 0;
ALTER TABLE relay.relay_outbox ADD COLUMN IF NOT EXISTS retry_intent_id text;
CREATE INDEX IF NOT EXISTS relay_job_scope_lookup ON relay.relay_outbox(app_id,room_id,kind,status);
CREATE TABLE IF NOT EXISTS relay.platform_task_fences(
 app_id text NOT NULL,room_id text NOT NULL,msg_type text NOT NULL,owner_lease_id text NOT NULL,
 owner_context jsonb NOT NULL,state text NOT NULL,claim_token text,claim_until timestamptz,
 query_required boolean NOT NULL DEFAULT false,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,room_id,msg_type));
CREATE TABLE IF NOT EXISTS relay.operational_events(
 event_id text PRIMARY KEY,app_id text NOT NULL,room_id text NOT NULL,kind text NOT NULL,
 details jsonb NOT NULL,recorded_at timestamptz NOT NULL DEFAULT now());
INSERT INTO relay.schema_versions VALUES(2) ON CONFLICT DO NOTHING;
