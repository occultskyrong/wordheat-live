"""Local PostgreSQL schema. DDL is invoked only by the migration command."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

def table(name, *columns, constraints=()):
    return sa.Table(name, metadata, sa.Column('id', sa.String, primary_key=True),
                    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
                    *columns, *constraints)

def text(name, **kw): return sa.Column(name, sa.String, **kw)
def integer(name, default=0): return sa.Column(name, sa.BigInteger, nullable=False, server_default=str(default))
def json(name, default='{}'): return sa.Column(name, JSONB, nullable=False, server_default=sa.text("'"+default+"'::jsonb"))

instance = table('service_instance', text('instance_id', nullable=False, unique=True), integer('schema_version', 1))
operators = table('operators', text('username', nullable=False, unique=True), text('password_hash', nullable=False), text('display_name', nullable=False), json('permissions', '[]'), sa.Column('disabled', sa.Boolean, nullable=False, server_default='false'))
auth_sessions = table('operator_sessions', sa.Column('actor_id', sa.String, sa.ForeignKey('operators.id'), nullable=False), text('token_hash', unique=True, nullable=False), text('csrf', nullable=False), sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
sessions = table('live_sessions', sa.Column('actor_id', sa.String, sa.ForeignKey('operators.id'), nullable=False), text('label', nullable=False), text('mode', nullable=False), text('run_purpose', nullable=False), text('status', nullable=False), integer('version', 1), json('rules'), integer('admission_seq'), integer('display_seq'), integer('studio_seq'), integer('analytics_commit_counter'), constraints=(sa.CheckConstraint("mode IN ('rehearsal','official','replay')"), sa.CheckConstraint("(mode IN ('rehearsal','replay') AND run_purpose='local') OR (mode='official' AND run_purpose IN ('test_live','production'))")))
puzzles = table('puzzle_versions', text('puzzle_id', nullable=False), integer('version', 1), text('word', nullable=False), json('aliases', '[]'), text('category_id', nullable=False), integer('difficulty', 1), text('explanation', nullable=False), text('status', nullable=False), text('manifest_id'), text('calibration_id'), text('evaluation_id'), text('approved_by'), constraints=(sa.UniqueConstraint('puzzle_id','version'),))
rounds = table('rounds', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), sa.Column('puzzle_version_id', sa.String, sa.ForeignKey('puzzle_versions.id'), nullable=False), text('status', nullable=False), text('short_code', nullable=False, unique=True), integer('epoch', 1), integer('version', 1), text('boot_id', nullable=False), integer('mono_started_ns'), integer('remaining_ms'), sa.Column('deadline_at', sa.DateTime(timezone=True)), text('winner_id'), sa.Column('winner_seq', sa.BigInteger), text('end_reason'), json('frozen_puzzle'), json('frozen_rules'))
sa.Index('uq_active_round', rounds.c.session_id, unique=True, postgresql_where=rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']))
participants = table('participants', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), text('identity_key', nullable=False), text('display_name', nullable=False), constraints=(sa.UniqueConstraint('session_id','identity_key'),))
guesses = table('guesses', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), sa.Column('round_id', sa.String, sa.ForeignKey('rounds.id'), nullable=False), sa.Column('participant_id', sa.String, sa.ForeignKey('participants.id'), nullable=False), text('source_mode', nullable=False), text('source_id', nullable=False), text('normalized_word', nullable=False), integer('admission_seq'), text('status', nullable=False), sa.Column('display_score', sa.Float), sa.Column('solved', sa.Boolean, nullable=False), integer('admitted_mono_ns'), text('boot_id', nullable=False), constraints=(sa.UniqueConstraint('session_id','source_mode','source_id'), sa.UniqueConstraint('round_id','participant_id','normalized_word')))
ledger = table('points_ledger', sa.Column('round_id', sa.String, sa.ForeignKey('rounds.id'), nullable=False), sa.Column('participant_id', sa.String, sa.ForeignKey('participants.id'), nullable=False), text('reason', nullable=False), text('reference_id', nullable=False), sa.Column('delta', sa.Integer, nullable=False), sa.Column('reverses_id', sa.String, sa.ForeignKey('points_ledger.id')), constraints=(sa.UniqueConstraint('round_id','participant_id','reason','reference_id'), sa.UniqueConstraint('reverses_id')))
settlements = table('round_settlements', sa.Column('round_id', sa.String, sa.ForeignKey('rounds.id'), nullable=False, unique=True), text('reason', nullable=False), text('winner_id'), integer('version', 1))
outbox = table('outbox_events', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), text('audience', nullable=False), integer('seq'), text('type', nullable=False), json('payload'), constraints=(sa.UniqueConstraint('session_id','audience','seq'), sa.CheckConstraint("audience IN ('DISPLAY','STUDIO')")))
idempotency = table('idempotency_commands', text('actor_id', nullable=False), text('method', nullable=False), text('path', nullable=False), text('key', nullable=False), text('request_hash', nullable=False), json('response'), constraints=(sa.UniqueConstraint('actor_id','method','path','key'),))
tickets = table('display_tickets', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), text('token_hash', nullable=False, unique=True), text('purpose', nullable=False), sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('used', sa.Boolean, nullable=False, server_default='false'), sa.Column('revoked', sa.Boolean, nullable=False, server_default='false'))
displays = table('display_sessions', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), sa.Column('ticket_id', sa.String, sa.ForeignKey('display_tickets.id'), nullable=False), text('token_hash', nullable=False, unique=True), text('purpose', nullable=False), text('csrf', nullable=False), integer('render_generation', 1), sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('revoked', sa.Boolean, nullable=False, server_default='false'))
sa.Index('uq_primary_display', displays.c.session_id, unique=True, postgresql_where=sa.and_(displays.c.purpose=='primary', displays.c.revoked.is_(False)))
catalog = table('wordbank_catalog', integer('revision'), text('selected_puzzle_id'), text('selected_strategy'), text('selected_by'), integer('selected_revision'))
selection_state = table('selection_state', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False, unique=True), integer('revision'), text('selection_id'))
next_selections = table('next_selections', sa.Column('session_id', sa.String, sa.ForeignKey('live_sessions.id'), nullable=False), integer('version'), text('status', nullable=False), text('puzzle_version_id'), text('strategy', nullable=False), integer('catalog_revision'), text('reason_code'), text('consumed_round_id'))
sa.Index('uq_ready_selection', next_selections.c.session_id, unique=True, postgresql_where=next_selections.c.status=='READY')
score_jobs = table('score_jobs', sa.Column('guess_id', sa.String, sa.ForeignKey('guesses.id'), nullable=False, unique=True), text('status', nullable=False), text('error_code'), sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False), sa.Column('lease_until', sa.DateTime(timezone=True)))
# Revision 2: bounded local draining and reliable crash-visible worker state.
rounds.append_column(sa.Column('drain_until_ns', sa.BigInteger))
rounds.append_column(sa.Column('close_seq', sa.BigInteger))
effects = table('display_effects', sa.Column('session_id',sa.String,sa.ForeignKey('live_sessions.id'),nullable=False), sa.Column('round_id',sa.String,sa.ForeignKey('rounds.id'),nullable=False),text('feedback_id',nullable=False,unique=True),text('delivery_id',nullable=False,unique=True),integer('source_seq'),json('payload'),sa.Column('processed',sa.Boolean,nullable=False,server_default='false'))
grants = table('effect_grants',sa.Column('display_id',sa.String,sa.ForeignKey('display_sessions.id'),nullable=False),sa.Column('effect_id',sa.String,sa.ForeignKey('display_effects.id'),nullable=False),integer('generation'),sa.Column('received',sa.Boolean,nullable=False,server_default='false'),sa.Column('processed',sa.Boolean,nullable=False,server_default='false'),constraints=(sa.UniqueConstraint('display_id','effect_id','generation'),))
connections = table('realtime_connections',sa.Column('display_id',sa.String,sa.ForeignKey('display_sessions.id'),nullable=False,unique=True),text('boot_id',nullable=False),text('nonce',nullable=False),integer('generation'),sa.Column('ready',sa.Boolean,nullable=False,server_default='false'))
receipts = table('render_receipts',sa.Column('display_id',sa.String,sa.ForeignKey('display_sessions.id'),nullable=False),integer('generation'),text('batch_id',nullable=False),text('stage',nullable=False),text('body_hash',nullable=False),json('response'),constraints=(sa.UniqueConstraint('display_id','generation','batch_id','stage'),))
commands = table('business_commands',sa.Column('session_id',sa.String,sa.ForeignKey('live_sessions.id'),nullable=False),text('actor_id',nullable=False),json('response'),text('trigger_key',unique=True))
configuration = table('session_configuration',sa.Column('session_id',sa.String,sa.ForeignKey('live_sessions.id'),nullable=False,unique=True),integer('rules_version',1),integer('next_rules_version',1),sa.Column('next_rules',JSONB))
puzzles.append_column(sa.Column('hints',JSONB,nullable=False,server_default=sa.text("'[]'::jsonb")))
rounds.append_column(sa.Column('published_hints',JSONB,nullable=False,server_default=sa.text("'[]'::jsonb")))
rejections = table('guess_rejections',sa.Column('session_id',sa.String,sa.ForeignKey('live_sessions.id'),nullable=False),sa.Column('round_id',sa.String,sa.ForeignKey('rounds.id')),text('reason_code',nullable=False),text('source_id',nullable=False),text('source_mode',nullable=False,server_default='manual'),sa.Column('provider_test',sa.Boolean),constraints=(sa.UniqueConstraint('session_id','source_mode','source_id',name='uq_rejected_source'),))
puzzles.append_column(text('source_ref',nullable=False,server_default=''))
puzzles.append_column(json('scoring_artifact'))
wordbank_jobs = table('wordbank_jobs',text('actor_id',nullable=False),text('kind',nullable=False),text('status',nullable=False),integer('version',1),integer('progress'),json('input'),json('result'),text('error_code'),text('claim_boot'),text('claim_token'),sa.Column('lease_until',sa.DateTime(timezone=True)),sa.Column('annotations_locked_at',sa.DateTime(timezone=True)),constraints=(sa.CheckConstraint("kind IN ('import','evaluation')"),sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')")))
rounds.append_column(sa.Column('next_due_ns',sa.BigInteger))
rounds.append_column(text('auto_attempt_key'))
rounds.append_column(text('auto_reason'))
replay_jobs = table('replay_jobs',text('actor_id',nullable=False),sa.Column('session_id',sa.String,sa.ForeignKey('live_sessions.id'),nullable=False,unique=True),text('status',nullable=False),text('state',nullable=False),integer('version',1),json('input'),text('input_hash',nullable=False),json('checkpoint'),integer('cursor'),integer('virtual_ms'),integer('events_applied'),integer('events_rejected'),text('boot_id'),text('claim_token'),sa.Column('lease_until',sa.DateTime(timezone=True)),text('error_code'),constraints=(sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')"),))
replay_events = table('replay_events',sa.Column('job_id',sa.String,sa.ForeignKey('replay_jobs.id'),nullable=False),integer('ordinal'),text('source_id',nullable=False),integer('offset_ms'),text('status',nullable=False),text('feedback_id'),text('reason_code'),constraints=(sa.UniqueConstraint('job_id','ordinal'),sa.UniqueConstraint('job_id','source_id')))
trace_steps = table('message_trace_steps',text('message_id',nullable=False),text('stage',nullable=False),text('state',nullable=False),sa.Column('time',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),text('reason_code'))
guesses.append_column(sa.Column('admitted_virtual_ms',sa.BigInteger))

# Core execution: durable scoring ownership, immutable official scope and audit.
for name in ('app_id','room_id','room_binding_id','installation_id','live_profile','authorization_id','candidate_digest','grant_digest','authorization_boot_id'):
    sessions.append_column(text(name))
for name in ('session_mono_start_ns','authorization_elapsed_ms'):
    sessions.append_column(sa.Column(name,sa.BigInteger))
sa.Index('uq_active_official_session',sessions.c.app_id,sessions.c.room_id,unique=True,postgresql_where=sa.and_(sessions.c.mode=='official',sessions.c.status.in_(['ACTIVE','ENDING','RECOVERY_REQUIRED'])))
for name in ('claim_token','claim_boot'):
    score_jobs.append_column(text(name))
score_jobs.append_column(integer('attempt'))
score_jobs.append_column(json('input_versions'))
for name in ('manifest_id','calibration_id'):
    next_selections.append_column(text(name))
next_selections.append_column(integer('next_rules_version',1))
next_selections.append_column(sa.Column('invalidated_at',sa.DateTime(timezone=True)))
operation_audits=table('operation_audits',text('actor_id',nullable=False),text('action',nullable=False),text('resource_id'),integer('resource_version'),text('request_id'),text('reason'),json('details'))

guesses.append_column(sa.Column('provider_test',sa.Boolean))
guesses.append_column(sa.Column('admitted_relay_ms',sa.BigInteger))

commands.append_column(text('resource_id'))

score_jobs.append_column(sa.Column('cached_score',sa.Float))
score_jobs.append_column(sa.Column('score_completed_ns',sa.BigInteger))

sa.Index('ix_score_jobs_work',score_jobs.c.status,score_jobs.c.lease_until,score_jobs.c.created_at,postgresql_where=score_jobs.c.status.in_(['queued','running','waiting']))

catalog.append_column(text('model_manifest_id'))
catalog.append_column(text('model_manifest_sha256'))

rounds.append_column(sa.Column('next_start_at',sa.DateTime(timezone=True)))

catalog.append_column(json('joint_identity'))
catalog.append_column(text('bundle_digest'))
model_manifests=table('model_manifests',text('model_manifest_id',nullable=False),text('manifest_sha256',nullable=False,unique=True),json('manifest'),constraints=(sa.UniqueConstraint('model_manifest_id','manifest_sha256'),))
calibrations=table('calibrations',sa.Column('puzzle_version_id',sa.String,sa.ForeignKey('puzzle_versions.id'),nullable=False),text('evaluation_id',nullable=False),text('manifest_sha256',nullable=False),text('artifact_sha256',nullable=False),json('artifact'))

bundle_activations=table('bundle_activations',text('actor_id',nullable=False),text('identity_sha256',nullable=False),json('result'))


sa.Index('ix_guess_rate_mono',guesses.c.session_id,guesses.c.participant_id,guesses.c.boot_id,guesses.c.admitted_mono_ns)
sa.Index('ix_guess_rate_virtual',guesses.c.session_id,guesses.c.participant_id,guesses.c.admitted_virtual_ms)
sa.Index('ix_guess_rate_relay',guesses.c.session_id,guesses.c.participant_id,guesses.c.admitted_relay_ms)

bundle_imports=table('bundle_imports',text('actor_id',nullable=False),text('file_sha256',nullable=False),text('identity_sha256',nullable=False),json('result'))
resource_origins=table('resource_origins',text('command_id',nullable=False),text('puzzle_version_id',nullable=False),text('evaluation_id',nullable=False),text('file_sha256',nullable=False),text('imported_by',nullable=False),constraints=(sa.UniqueConstraint('command_id','puzzle_version_id'),))

# Revision 3: revocable operator sessions, operator disable time, guess epoch.
auth_sessions.append_column(sa.Column('revoked_at',sa.DateTime(timezone=True)))
auth_sessions.append_column(text('revoked_by'))
operators.append_column(sa.Column('disabled_at',sa.DateTime(timezone=True)))
guesses.append_column(sa.Column('epoch',sa.BigInteger))
sa.Index('ix_guess_epoch',guesses.c.session_id,guesses.c.epoch)
