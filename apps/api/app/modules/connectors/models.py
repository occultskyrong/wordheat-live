"""Connector-owned metadata; no imports of another application's implementation."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata=sa.MetaData()


def table(name,*columns,constraints=()):
    return sa.Table(name,metadata,sa.Column('id',sa.Text,primary_key=True),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),*columns,*constraints)


def text(name,**kw):return sa.Column(name,sa.Text,**kw)
def integer(name,default='0'):return sa.Column(name,sa.BigInteger,nullable=False,server_default=default)
def timestamp(name):return sa.Column(name,sa.DateTime(timezone=True))
def data(name):return sa.Column(name,JSONB,nullable=False,server_default=sa.text("'{}'::jsonb"))

trust_objects=table('readiness_objects',text('object_id',nullable=False,unique=True),text('issuer_id',nullable=False),integer('issuer_revision'),text('type',nullable=False),text('key_id',nullable=False),text('digest',nullable=False),data('envelope'),data('payload'),sa.Column('revoked',sa.Boolean,nullable=False,server_default='false'))
trust_issuers=table('readiness_issuers',integer('revision'))
trust_state=table('readiness_revocations',integer('revision'),data('object_ids'))
key_bundles=table('readiness_key_bundles',integer('ordinal'),text('object_id',nullable=False,unique=True),text('digest',nullable=False),data('envelope'),data('root_chain'),text('native_command_id',nullable=False),text('confirmation_id',nullable=False),constraints=(sa.UniqueConstraint('ordinal'),))
room_bindings=table('connector_room_bindings',text('app_id',nullable=False),text('room_id',nullable=False),text('installation_id',nullable=False),text('connector_id',nullable=False),text('status',nullable=False),data('authorization'),timestamp('expires_at'))
bindings=table('connector_sessions',text('session_id',nullable=False,unique=True),text('connector_id',nullable=False),data('scope'),data('lease'),data('connection'),text('state',nullable=False),text('stream_id'),integer('persisted_seq'),integer('admitted_seq'),integer('terminal_seq'),integer('revision'),text('boot_id'),integer('lease_deadline_ns'),integer('authorization_deadline_ns'),integer('session_started_ns'),integer('elapsed_ms'),integer('disconnected_ns'),integer('last_contact_ns'),text('last_error'))
bindings.append_column(sa.Column('remote_pending',sa.BigInteger))
deliveries=table('connector_deliveries',text('session_id',nullable=False),text('stream_id',nullable=False),integer('stream_seq'),text('immutable_sha256',nullable=False),sa.Column('raw_body',sa.LargeBinary),data('body'),text('state',nullable=False),text('reason_code'),text('feedback_id',unique=True),constraints=(sa.UniqueConstraint('stream_id','stream_seq'),))
intents=table('connector_intents',text('session_id',nullable=False),text('round_id'),text('operation',nullable=False),data('args'),data('response'),text('status',nullable=False),integer('attempts'),timestamp('next_attempt_at'),text('claim_token'),timestamp('lease_until'),text('last_error'),text('command_id',nullable=False,unique=True),constraints=(sa.CheckConstraint("status IN ('pending','waiting','running','retry','succeeded','failed','cancelled')"),))
acks=table('connector_ack_outbox',text('session_id',nullable=False),text('stream_id',nullable=False),integer('stream_seq'),text('stage',nullable=False),text('receipt_id',nullable=False),text('status',nullable=False),integer('attempts'),timestamp('next_attempt_at'),text('claim_token'),timestamp('lease_until'),text('last_error'),data('response'),integer('revision'),constraints=(sa.UniqueConstraint('session_id','stream_id','stream_seq','stage'),sa.CheckConstraint("status IN ('pending','running','retry','succeeded','failed','cancelled')"),))
acks.append_column(integer('cycle_attempts'));acks.append_column(integer('retry_cycle'));acks.append_column(text('retry_intent_id'))
acks.append_column(sa.Column('client_time',sa.DateTime(timezone=True)))
baselines=table('connector_stream_baselines',text('session_id',nullable=False),text('stream_id',nullable=False),integer('start_seq'),text('intent_id',nullable=False),text('previous_stream_id'),data('rpo'),constraints=(sa.UniqueConstraint('session_id','stream_id'),))
checkpoint_history=table('connector_checkpoint_history',text('session_id',nullable=False),text('stream_id',nullable=False),integer('persisted_seq'),integer('admitted_seq'),integer('terminal_seq'),text('reason',nullable=False),constraints=(sa.UniqueConstraint('session_id','stream_id'),))
native_commands=table('native_pairing_commands',text('actor_id',nullable=False),text('operation',nullable=False),text('source_id'),text('source_sha256'),text('fingerprint',nullable=False),text('nonce_hash',nullable=False),text('installation_id',nullable=False),text('boot_id',nullable=False),timestamp('expires_at'),text('status',nullable=False),data('result'),text('last_error'))
