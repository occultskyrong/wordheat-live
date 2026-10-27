
CREATE TABLE connector_ack_outbox (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	stream_id TEXT NOT NULL, 
	stream_seq BIGINT DEFAULT '0' NOT NULL, 
	stage TEXT NOT NULL, 
	receipt_id TEXT NOT NULL, 
	status TEXT NOT NULL, 
	attempts BIGINT DEFAULT '0' NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE, 
	claim_token TEXT, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	response JSONB DEFAULT '{}'::jsonb NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	cycle_attempts BIGINT DEFAULT '0' NOT NULL, 
	retry_cycle BIGINT DEFAULT '0' NOT NULL, 
	retry_intent_id TEXT, 
	client_time TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, stream_id, stream_seq, stage), 
	CHECK (status IN ('pending','running','retry','succeeded','failed','cancelled'))
)

;

CREATE TABLE connector_checkpoint_history (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	stream_id TEXT NOT NULL, 
	persisted_seq BIGINT DEFAULT '0' NOT NULL, 
	admitted_seq BIGINT DEFAULT '0' NOT NULL, 
	terminal_seq BIGINT DEFAULT '0' NOT NULL, 
	reason TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, stream_id)
)

;

CREATE TABLE connector_deliveries (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	stream_id TEXT NOT NULL, 
	stream_seq BIGINT DEFAULT '0' NOT NULL, 
	immutable_sha256 TEXT NOT NULL, 
	raw_body BYTEA, 
	body JSONB DEFAULT '{}'::jsonb NOT NULL, 
	state TEXT NOT NULL, 
	reason_code TEXT, 
	feedback_id TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (stream_id, stream_seq), 
	UNIQUE (feedback_id)
)

;

CREATE TABLE connector_intents (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	round_id TEXT, 
	operation TEXT NOT NULL, 
	args JSONB DEFAULT '{}'::jsonb NOT NULL, 
	response JSONB DEFAULT '{}'::jsonb NOT NULL, 
	status TEXT NOT NULL, 
	attempts BIGINT DEFAULT '0' NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE, 
	claim_token TEXT, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	command_id TEXT NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (status IN ('pending','waiting','running','retry','succeeded','failed','cancelled')), 
	UNIQUE (command_id)
)

;

CREATE TABLE connector_room_bindings (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	app_id TEXT NOT NULL, 
	room_id TEXT NOT NULL, 
	installation_id TEXT NOT NULL, 
	connector_id TEXT NOT NULL, 
	status TEXT NOT NULL, 
	"authorization" JSONB DEFAULT '{}'::jsonb NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
)

;

CREATE TABLE connector_sessions (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	connector_id TEXT NOT NULL, 
	scope JSONB DEFAULT '{}'::jsonb NOT NULL, 
	lease JSONB DEFAULT '{}'::jsonb NOT NULL, 
	connection JSONB DEFAULT '{}'::jsonb NOT NULL, 
	state TEXT NOT NULL, 
	stream_id TEXT, 
	persisted_seq BIGINT DEFAULT '0' NOT NULL, 
	admitted_seq BIGINT DEFAULT '0' NOT NULL, 
	terminal_seq BIGINT DEFAULT '0' NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	boot_id TEXT, 
	lease_deadline_ns BIGINT DEFAULT '0' NOT NULL, 
	authorization_deadline_ns BIGINT DEFAULT '0' NOT NULL, 
	session_started_ns BIGINT DEFAULT '0' NOT NULL, 
	elapsed_ms BIGINT DEFAULT '0' NOT NULL, 
	disconnected_ns BIGINT DEFAULT '0' NOT NULL, 
	last_contact_ns BIGINT DEFAULT '0' NOT NULL, 
	last_error TEXT, 
	remote_pending BIGINT, 
	PRIMARY KEY (id), 
	UNIQUE (session_id)
)

;

CREATE TABLE connector_stream_baselines (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id TEXT NOT NULL, 
	stream_id TEXT NOT NULL, 
	start_seq BIGINT DEFAULT '0' NOT NULL, 
	intent_id TEXT NOT NULL, 
	previous_stream_id TEXT, 
	rpo JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, stream_id)
)

;

CREATE TABLE native_pairing_commands (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	actor_id TEXT NOT NULL, 
	operation TEXT NOT NULL, 
	source_id TEXT, 
	source_sha256 TEXT, 
	fingerprint TEXT NOT NULL, 
	nonce_hash TEXT NOT NULL, 
	installation_id TEXT NOT NULL, 
	boot_id TEXT NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	status TEXT NOT NULL, 
	result JSONB DEFAULT '{}'::jsonb NOT NULL, 
	last_error TEXT, 
	PRIMARY KEY (id)
)

;

CREATE TABLE readiness_issuers (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE readiness_key_bundles (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	ordinal BIGINT DEFAULT '0' NOT NULL, 
	object_id TEXT NOT NULL, 
	digest TEXT NOT NULL, 
	envelope JSONB DEFAULT '{}'::jsonb NOT NULL, 
	root_chain JSONB DEFAULT '{}'::jsonb NOT NULL, 
	native_command_id TEXT NOT NULL, 
	confirmation_id TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (ordinal), 
	UNIQUE (object_id)
)

;

CREATE TABLE readiness_objects (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	object_id TEXT NOT NULL, 
	issuer_id TEXT NOT NULL, 
	issuer_revision BIGINT DEFAULT '0' NOT NULL, 
	type TEXT NOT NULL, 
	key_id TEXT NOT NULL, 
	digest TEXT NOT NULL, 
	envelope JSONB DEFAULT '{}'::jsonb NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	revoked BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (object_id)
)

;

CREATE TABLE readiness_revocations (
	id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	object_ids JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE OR REPLACE FUNCTION wordheat_reject_room_binding_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.app_id,NEW.room_id,NEW.installation_id,NEW.connector_id) IS DISTINCT FROM (OLD.app_id,OLD.room_id,OLD.installation_id,OLD.connector_id) THEN RAISE EXCEPTION 'ROOM_BINDING_IMMUTABLE'; END IF; RETURN NEW; END $$;
CREATE TRIGGER immutable_room_binding BEFORE UPDATE ON connector_room_bindings FOR EACH ROW EXECUTE FUNCTION wordheat_reject_room_binding_mutation();
