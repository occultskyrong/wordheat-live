-- WordHeat local PostgreSQL schema revision 1. Never edit after release.

CREATE TABLE IF NOT EXISTS idempotency_commands (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	actor_id VARCHAR NOT NULL, 
	method VARCHAR NOT NULL, 
	path VARCHAR NOT NULL, 
	key VARCHAR NOT NULL, 
	request_hash VARCHAR NOT NULL, 
	response JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (actor_id, method, path, key)
)

;

CREATE TABLE IF NOT EXISTS operators (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	username VARCHAR NOT NULL, 
	password_hash VARCHAR NOT NULL, 
	display_name VARCHAR NOT NULL, 
	permissions JSONB DEFAULT '[]'::jsonb NOT NULL, 
	disabled BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (username)
)

;

CREATE TABLE IF NOT EXISTS puzzle_versions (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	puzzle_id VARCHAR NOT NULL, 
	version BIGINT DEFAULT '1' NOT NULL, 
	word VARCHAR NOT NULL, 
	aliases JSONB DEFAULT '[]'::jsonb NOT NULL, 
	category_id VARCHAR NOT NULL, 
	difficulty BIGINT DEFAULT '1' NOT NULL, 
	explanation VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	manifest_id VARCHAR, 
	calibration_id VARCHAR, 
	evaluation_id VARCHAR, 
	approved_by VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (puzzle_id, version)
)

;

CREATE TABLE IF NOT EXISTS service_instance (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	instance_id VARCHAR NOT NULL, 
	schema_version BIGINT DEFAULT '1' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (instance_id)
)

;

CREATE TABLE IF NOT EXISTS wordbank_catalog (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	selected_puzzle_id VARCHAR, 
	selected_strategy VARCHAR, 
	selected_by VARCHAR, 
	selected_revision BIGINT DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE IF NOT EXISTS live_sessions (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	actor_id VARCHAR NOT NULL, 
	label VARCHAR NOT NULL, 
	mode VARCHAR NOT NULL, 
	run_purpose VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	version BIGINT DEFAULT '1' NOT NULL, 
	rules JSONB DEFAULT '{}'::jsonb NOT NULL, 
	admission_seq BIGINT DEFAULT '0' NOT NULL, 
	display_seq BIGINT DEFAULT '0' NOT NULL, 
	studio_seq BIGINT DEFAULT '0' NOT NULL, 
	analytics_commit_counter BIGINT DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id), 
	CHECK (mode IN ('rehearsal','official','replay')), 
	CHECK ((mode IN ('rehearsal','replay') AND run_purpose='local') OR (mode='official' AND run_purpose IN ('test_live','production'))), 
	FOREIGN KEY(actor_id) REFERENCES operators (id)
)

;

CREATE TABLE IF NOT EXISTS operator_sessions (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	actor_id VARCHAR NOT NULL, 
	token_hash VARCHAR NOT NULL, 
	csrf VARCHAR NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(actor_id) REFERENCES operators (id), 
	UNIQUE (token_hash)
)

;

CREATE TABLE IF NOT EXISTS display_tickets (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	token_hash VARCHAR NOT NULL, 
	purpose VARCHAR NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	used BOOLEAN DEFAULT 'false' NOT NULL, 
	revoked BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	UNIQUE (token_hash)
)

;

CREATE TABLE IF NOT EXISTS next_selections (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	version BIGINT DEFAULT '0' NOT NULL, 
	status VARCHAR NOT NULL, 
	puzzle_version_id VARCHAR, 
	strategy VARCHAR NOT NULL, 
	catalog_revision BIGINT DEFAULT '0' NOT NULL, 
	reason_code VARCHAR, 
	consumed_round_id VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id)
)

;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ready_selection ON next_selections (session_id) WHERE status = 'READY';

CREATE TABLE IF NOT EXISTS outbox_events (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	audience VARCHAR NOT NULL, 
	seq BIGINT DEFAULT '0' NOT NULL, 
	type VARCHAR NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, audience, seq), 
	CHECK (audience IN ('DISPLAY','STUDIO')), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id)
)

;

CREATE TABLE IF NOT EXISTS participants (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	identity_key VARCHAR NOT NULL, 
	display_name VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, identity_key), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id)
)

;

CREATE TABLE IF NOT EXISTS rounds (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	puzzle_version_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	short_code VARCHAR NOT NULL, 
	epoch BIGINT DEFAULT '1' NOT NULL, 
	version BIGINT DEFAULT '1' NOT NULL, 
	boot_id VARCHAR NOT NULL, 
	mono_started_ns BIGINT DEFAULT '0' NOT NULL, 
	remaining_ms BIGINT DEFAULT '0' NOT NULL, 
	deadline_at TIMESTAMP WITH TIME ZONE, 
	winner_id VARCHAR, 
	winner_seq BIGINT, 
	end_reason VARCHAR, 
	frozen_puzzle JSONB DEFAULT '{}'::jsonb NOT NULL, 
	frozen_rules JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	FOREIGN KEY(puzzle_version_id) REFERENCES puzzle_versions (id), 
	UNIQUE (short_code)
)

;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_round ON rounds (session_id) WHERE status IN ('PREPARING', 'RUNNING', 'PAUSED', 'DRAINING', 'FAULTED');

CREATE TABLE IF NOT EXISTS selection_state (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	revision BIGINT DEFAULT '0' NOT NULL, 
	selection_id VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (session_id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id)
)

;

CREATE TABLE IF NOT EXISTS display_sessions (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	ticket_id VARCHAR NOT NULL, 
	token_hash VARCHAR NOT NULL, 
	purpose VARCHAR NOT NULL, 
	csrf VARCHAR NOT NULL, 
	render_generation BIGINT DEFAULT '1' NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	FOREIGN KEY(ticket_id) REFERENCES display_tickets (id), 
	UNIQUE (token_hash)
)

;
CREATE UNIQUE INDEX IF NOT EXISTS uq_primary_display ON display_sessions (session_id) WHERE purpose = 'primary' AND revoked IS false;

CREATE TABLE IF NOT EXISTS guesses (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	round_id VARCHAR NOT NULL, 
	participant_id VARCHAR NOT NULL, 
	source_mode VARCHAR NOT NULL, 
	source_id VARCHAR NOT NULL, 
	normalized_word VARCHAR NOT NULL, 
	admission_seq BIGINT DEFAULT '0' NOT NULL, 
	status VARCHAR NOT NULL, 
	display_score FLOAT, 
	solved BOOLEAN NOT NULL, 
	admitted_mono_ns BIGINT DEFAULT '0' NOT NULL, 
	boot_id VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (session_id, source_mode, source_id), 
	UNIQUE (round_id, participant_id, normalized_word), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	FOREIGN KEY(round_id) REFERENCES rounds (id), 
	FOREIGN KEY(participant_id) REFERENCES participants (id)
)

;

CREATE TABLE IF NOT EXISTS points_ledger (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	round_id VARCHAR NOT NULL, 
	participant_id VARCHAR NOT NULL, 
	reason VARCHAR NOT NULL, 
	reference_id VARCHAR NOT NULL, 
	delta INTEGER NOT NULL, 
	reverses_id VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (round_id, participant_id, reason, reference_id), 
	UNIQUE (reverses_id), 
	FOREIGN KEY(round_id) REFERENCES rounds (id), 
	FOREIGN KEY(participant_id) REFERENCES participants (id), 
	FOREIGN KEY(reverses_id) REFERENCES points_ledger (id)
)

;

CREATE TABLE IF NOT EXISTS round_settlements (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	round_id VARCHAR NOT NULL, 
	reason VARCHAR NOT NULL, 
	winner_id VARCHAR, 
	version BIGINT DEFAULT '1' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (round_id), 
	FOREIGN KEY(round_id) REFERENCES rounds (id)
)

;

CREATE TABLE IF NOT EXISTS score_jobs (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	guess_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	error_code VARCHAR, 
	deadline_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (guess_id), 
	FOREIGN KEY(guess_id) REFERENCES guesses (id)
)

;
