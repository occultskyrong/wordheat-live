ALTER TABLE puzzle_versions ADD COLUMN IF NOT EXISTS hints jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS published_hints jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS business_commands (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	actor_id VARCHAR NOT NULL, 
	response JSONB DEFAULT '{}'::jsonb NOT NULL, 
	trigger_key VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	UNIQUE (trigger_key)
)

;

CREATE TABLE IF NOT EXISTS session_configuration (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	rules_version BIGINT DEFAULT '1' NOT NULL, 
	next_rules_version BIGINT DEFAULT '1' NOT NULL, 
	next_rules JSONB, 
	PRIMARY KEY (id), 
	UNIQUE (session_id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id)
)

;

CREATE TABLE IF NOT EXISTS display_effects (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	session_id VARCHAR NOT NULL, 
	round_id VARCHAR NOT NULL, 
	feedback_id VARCHAR NOT NULL, 
	delivery_id VARCHAR NOT NULL, 
	source_seq BIGINT DEFAULT '0' NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	processed BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES live_sessions (id), 
	FOREIGN KEY(round_id) REFERENCES rounds (id), 
	UNIQUE (feedback_id), 
	UNIQUE (delivery_id)
)

;

CREATE TABLE IF NOT EXISTS effect_grants (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	display_id VARCHAR NOT NULL, 
	effect_id VARCHAR NOT NULL, 
	generation BIGINT DEFAULT '0' NOT NULL, 
	received BOOLEAN DEFAULT 'false' NOT NULL, 
	processed BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (display_id, effect_id, generation), 
	FOREIGN KEY(display_id) REFERENCES display_sessions (id), 
	FOREIGN KEY(effect_id) REFERENCES display_effects (id)
)

;

CREATE TABLE IF NOT EXISTS realtime_connections (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	display_id VARCHAR NOT NULL, 
	boot_id VARCHAR NOT NULL, 
	nonce VARCHAR NOT NULL, 
	generation BIGINT DEFAULT '0' NOT NULL, 
	ready BOOLEAN DEFAULT 'false' NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (display_id), 
	FOREIGN KEY(display_id) REFERENCES display_sessions (id)
)

;

CREATE TABLE IF NOT EXISTS render_receipts (
	id VARCHAR NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	display_id VARCHAR NOT NULL, 
	generation BIGINT DEFAULT '0' NOT NULL, 
	batch_id VARCHAR NOT NULL, 
	stage VARCHAR NOT NULL, 
	body_hash VARCHAR NOT NULL, 
	response JSONB DEFAULT '{}'::jsonb NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (display_id, generation, batch_id, stage), 
	FOREIGN KEY(display_id) REFERENCES display_sessions (id)
)

;
INSERT INTO session_configuration(id,session_id) SELECT 'config-'||id,id FROM live_sessions ON CONFLICT(session_id) DO NOTHING;
