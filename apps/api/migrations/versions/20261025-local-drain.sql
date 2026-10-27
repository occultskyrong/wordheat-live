ALTER TABLE rounds ADD COLUMN IF NOT EXISTS drain_until_ns bigint;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS close_seq bigint;
UPDATE service_instance SET schema_version=1;
