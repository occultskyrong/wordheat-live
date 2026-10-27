ALTER TABLE rounds ADD COLUMN next_due_ns bigint;
ALTER TABLE rounds ADD COLUMN auto_attempt_key text;
ALTER TABLE rounds ADD COLUMN auto_reason text;
