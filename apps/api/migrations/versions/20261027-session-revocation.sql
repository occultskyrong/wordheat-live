-- Operator and display sessions are revocable inside their 8 hour ceiling.
-- A revoked row keeps its audit identity; expiry alone never re-enables it.
ALTER TABLE operator_sessions ADD COLUMN revoked_at timestamptz, ADD COLUMN revoked_by text;
ALTER TABLE operators ADD COLUMN disabled_at timestamptz;
-- A guess carries its own window epoch, so replay and audit never join rounds.
ALTER TABLE guesses ADD COLUMN epoch bigint;
UPDATE guesses g SET epoch = r.epoch FROM rounds r WHERE r.id = g.round_id AND g.epoch IS NULL;
CREATE INDEX ix_guess_epoch ON guesses(session_id, epoch);
