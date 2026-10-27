-- Global runtime commands are not session business commands.
CREATE TABLE IF NOT EXISTS runtime_instances (
 installation_id text PRIMARY KEY, boot_id text NOT NULL, version bigint NOT NULL CHECK(version>0),
 state text NOT NULL, active_command_id text, last_exit jsonb, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_commands (
 id text PRIMARY KEY, installation_id text NOT NULL REFERENCES runtime_instances(installation_id),
 actor_id text NOT NULL REFERENCES operators(id), idempotency_key text NOT NULL, request_hash text NOT NULL,
 boot_id text NOT NULL, kind text NOT NULL CHECK(kind IN ('stop','request_maintenance')), operation text,
 reason text NOT NULL, status text NOT NULL CHECK(status IN ('pending','applied','failed','cancelled')), stage text NOT NULL,
 resource_version bigint NOT NULL, deadline_ns bigint, plan jsonb NOT NULL DEFAULT '{}', error jsonb, result jsonb,
 created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 UNIQUE(installation_id,actor_id,idempotency_key));
CREATE UNIQUE INDEX IF NOT EXISTS runtime_one_pending ON runtime_commands(installation_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS runtime_maintenance_steps (
 command_id text NOT NULL REFERENCES runtime_commands(id) ON DELETE CASCADE, stage text NOT NULL,
 status text NOT NULL, evidence jsonb NOT NULL, updated_at timestamptz NOT NULL, PRIMARY KEY(command_id,stage));
CREATE TABLE IF NOT EXISTS runtime_backups (
 id text PRIMARY KEY, installation_id text NOT NULL REFERENCES runtime_instances(installation_id),
 status text NOT NULL, command_id text, payload jsonb NOT NULL, updated_at timestamptz NOT NULL);

CREATE TABLE IF NOT EXISTS runtime_scope_recoveries (
 installation_id text NOT NULL REFERENCES runtime_instances(installation_id) ON DELETE CASCADE,
 boot_id text NOT NULL, session_id text NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
 recovered_at timestamptz NOT NULL, PRIMARY KEY(installation_id,boot_id,session_id));
CREATE TABLE IF NOT EXISTS runtime_native_requests (
 id text PRIMARY KEY, installation_id text NOT NULL REFERENCES runtime_instances(installation_id) ON DELETE CASCADE,
 actor_id text NOT NULL REFERENCES operators(id), boot_id text NOT NULL, status text NOT NULL,
 stage text NOT NULL, resource_version bigint NOT NULL, payload jsonb NOT NULL,error jsonb,result jsonb,updated_at timestamptz NOT NULL);
