"""Runtime-owned metadata; independent of core session commands."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
metadata=sa.MetaData()
instances=sa.Table('runtime_instances',metadata,
 sa.Column('installation_id',sa.String,primary_key=True),sa.Column('boot_id',sa.String,nullable=False),
 sa.Column('version',sa.BigInteger,nullable=False),sa.Column('state',sa.String,nullable=False),
 sa.Column('active_command_id',sa.String),sa.Column('last_exit',JSONB),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
commands=sa.Table('runtime_commands',metadata,
 sa.Column('id',sa.String,primary_key=True),sa.Column('installation_id',sa.String,nullable=False),
 sa.Column('actor_id',sa.String,nullable=False),sa.Column('idempotency_key',sa.String,nullable=False),sa.Column('request_hash',sa.String,nullable=False),
 sa.Column('boot_id',sa.String,nullable=False),sa.Column('kind',sa.String,nullable=False),sa.Column('operation',sa.String),
 sa.Column('reason',sa.String,nullable=False),sa.Column('status',sa.String,nullable=False),sa.Column('stage',sa.String,nullable=False),
 sa.Column('resource_version',sa.BigInteger,nullable=False),sa.Column('deadline_ns',sa.BigInteger),
 sa.Column('plan',JSONB,nullable=False),sa.Column('error',JSONB),sa.Column('result',JSONB),
 sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
steps=sa.Table('runtime_maintenance_steps',metadata,
 sa.Column('command_id',sa.String,primary_key=True),sa.Column('stage',sa.String,primary_key=True),
 sa.Column('status',sa.String,nullable=False),sa.Column('evidence',JSONB,nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
backups=sa.Table('runtime_backups',metadata,
 sa.Column('id',sa.String,primary_key=True),sa.Column('installation_id',sa.String,nullable=False),sa.Column('status',sa.String,nullable=False),sa.Column('command_id',sa.String),
 sa.Column('payload',JSONB,nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))

scopes=sa.Table('runtime_scope_recoveries',metadata,
 sa.Column('installation_id',sa.String,primary_key=True),sa.Column('boot_id',sa.String,primary_key=True),
 sa.Column('session_id',sa.String,primary_key=True),sa.Column('recovered_at',sa.DateTime(timezone=True),nullable=False))
native_requests=sa.Table('runtime_native_requests',metadata,
 sa.Column('id',sa.String,primary_key=True),sa.Column('installation_id',sa.String,nullable=False),sa.Column('actor_id',sa.String,nullable=False),
 sa.Column('boot_id',sa.String,nullable=False),sa.Column('status',sa.String,nullable=False),sa.Column('stage',sa.String,nullable=False),
 sa.Column('resource_version',sa.BigInteger,nullable=False),sa.Column('payload',JSONB,nullable=False),sa.Column('error',JSONB),
 sa.Column('result',JSONB),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
