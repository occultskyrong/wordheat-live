"""Analytics-owned tables; deliberately independent of core metadata."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
metadata=sa.MetaData()
snapshots=sa.Table('analytics_snapshots',metadata,
    sa.Column('id',sa.String,primary_key=True),sa.Column('session_id',sa.String,nullable=False),
    sa.Column('actor_id',sa.String,nullable=False),sa.Column('watermark',sa.BigInteger,nullable=False),
    sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
    sa.Column('last_access',sa.DateTime(timezone=True),nullable=False),sa.Column('payload',JSONB(none_as_null=True)),
    sa.Column('size_bytes',sa.BigInteger,nullable=False),sa.Column('reason',sa.String),
    sa.Column('pin_first',sa.DateTime(timezone=True)),sa.Column('pin_until',sa.DateTime(timezone=True)),
    sa.Column('pin_count',sa.Integer,nullable=False,server_default='0'))
finals=sa.Table('analytics_final_aggregates',metadata,sa.Column('session_id',sa.String,primary_key=True),sa.Column('payload',JSONB,nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False))
session_facts=sa.Table('analytics_session_facts',metadata,sa.Column('session_id',sa.String,primary_key=True),sa.Column('payload',JSONB,nullable=False))
round_facts=sa.Table('analytics_round_facts',metadata,sa.Column('round_id',sa.String,primary_key=True),sa.Column('session_id',sa.String,nullable=False),sa.Column('payload',JSONB,nullable=False))
attempts=sa.Table('analytics_attempt_observations',metadata,sa.Column('id',sa.String,primary_key=True),sa.Column('session_id',sa.String,nullable=False),sa.Column('payload',JSONB,nullable=False))
latencies=sa.Table('analytics_latency_observations',metadata,sa.Column('guess_id',sa.String,primary_key=True),sa.Column('session_id',sa.String,nullable=False),sa.Column('payload',JSONB,nullable=False))
exports=sa.Table('analytics_exports',metadata,sa.Column('id',sa.String,primary_key=True),sa.Column('actor_id',sa.String,nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False))

# Separately retained anonymous trace payload; opaque lookup metadata stays in round_facts.
traces=sa.Table('analytics_round_traces',metadata,
    sa.Column('round_id',sa.String,primary_key=True),sa.Column('session_id',sa.String,nullable=False),
    sa.Column('trace_ref',sa.String,nullable=False),sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
    sa.Column('payload',JSONB,nullable=False))

# Trusted local observation; remote unknown remains JSON null.
delivery=sa.Table('analytics_delivery_observations',metadata,
    sa.Column('session_id',sa.String,primary_key=True),sa.Column('observation_id',sa.String,nullable=False),
    sa.Column('revision',sa.BigInteger,nullable=False),sa.Column('payload',JSONB,nullable=False))
# Final business rows are bounded independently of the query-copy resource budget.
final_rows=sa.Table('analytics_final_rows',metadata,
    sa.Column('session_id',sa.String,primary_key=True),sa.Column('section',sa.String,primary_key=True),
    sa.Column('row_key',sa.String,primary_key=True),sa.Column('payload',JSONB,nullable=False),
    sa.Column('size_bytes',sa.BigInteger,nullable=False))
maintenance=sa.Table('analytics_maintenance_schedule',metadata,
    sa.Column('session_id',sa.String,primary_key=True),sa.Column('next_run',sa.DateTime(timezone=True),nullable=False))

event_retention=sa.Table('analytics_event_retention',metadata,
    sa.Column('session_id',sa.String,primary_key=True),sa.Column('audience',sa.String,primary_key=True),
    sa.Column('through_seq',sa.BigInteger,nullable=False),sa.Column('payload_hash',sa.String,nullable=False))
