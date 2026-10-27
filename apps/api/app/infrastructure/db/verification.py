"""Connected-instance and packaged migration identity checks, before any DDL."""
import hashlib
from importlib.resources import files
import sqlalchemy as sa
from app.errors import DomainError

def verify_target(connection):
    row=connection.execute(sa.text("SELECT current_database() AS name, current_setting('server_version_num')::int AS version, shobj_description(oid,'pg_database') AS marker FROM pg_database WHERE datname=current_database()")).mappings().one()
    if row['version']//10000!=16 or not row['name'].lower().startswith('wordheat_') or row['marker'] not in {'wordheat-live:test:local','wordheat-live:development:local'}:
        raise DomainError('DATABASE_TARGET_UNVERIFIED','Dedicated PostgreSQL 16 ownership marker required',503)

def packaged_revisions():
    return {item.name:hashlib.sha256(item.read_bytes()).hexdigest() for item in files('migrations.versions').iterdir() if item.name.endswith('.sql')}

def verify_revisions(connection):
    expected=packaged_revisions()
    rows=dict(connection.execute(sa.text('SELECT revision,sha256 FROM wordheat_schema_revisions')).all())
    if not expected or any(rows.get(name)!=checksum for name,checksum in expected.items()):
        raise DomainError('DATABASE_SCHEMA_UNAVAILABLE','Packaged migrations missing or checksum mismatch',503)
