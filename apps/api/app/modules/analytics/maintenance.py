"""Bounded anonymous final recovery and reference-preserving retention.

Only the API scheduler calls these use cases. No GET seals a session, no remote
state is queried, and no ledger/dedup/receipt/ACK identity is deleted.
"""
from datetime import timedelta
import hmac
import hashlib
import json
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.analytics import models as m

# A cursor page is the only application-memory accumulation during recovery.
RECOVERY_PAGE=128

def recover_final(service,c,session,facts):
    from app.modules.analytics.service import RAW_CTE,calculate,stamp,iso,json_bytes,VERSION
    sid=session['id']; at=stamp(facts.get('materialization_basis_at') or facts.get('sealed_at'))
    if at is None: raise ValueError('Pending materialization has no immutable basis')
    frozen=dict(facts, sealed_at=iso(at),unresolved_gap=False)
    frozen.pop('materialization_pending_reason',None)
    session=dict(session,analytics_commit_counter=facts.get('materialization_watermark',session['analytics_commit_counter']))
    result=calculate(session,[],[],[],[],[],dict(frozen,started_at=None),{},at)
    result['coverage']['started_at']=frozen.get('started_at')
    params={'sid':sid,'production':session['mode']=='official' and session['run_purpose']=='production','started':result['coverage']['started_at'],'through':result['coverage']['through_at']}
    c.execute(m.final_rows.delete().where(m.final_rows.c.session_id==sid))
    fields="""count(DISTINCT participant_id) FILTER(WHERE successful) AS participants_count,
      count(*) FILTER(WHERE successful) AS valid_distinct_guesses,
      count(*) FILTER(WHERE NOT nonterminal) AS guess_attempts,
      count(*) FILTER(WHERE business_rejected) AS business_rejected,
      count(*) FILTER(WHERE system_failed) AS system_failed,
      count(*) FILTER(WHERE nonterminal) AS nonterminal"""
    query=RAW_CTE+""", grouped AS (SELECT round_id,"""+fields+""" FROM quality GROUP BY round_id),
      points AS (SELECT l.round_id,sum(l.delta)::bigint AS points FROM points_ledger l
        JOIN rounds r ON r.id=l.round_id WHERE r.session_id=:sid GROUP BY l.round_id)
      SELECT r.id,r.status,r.end_reason,r.puzzle_version_id,r.created_at,
        r.frozen_puzzle->>'category_id' AS category_id,(r.frozen_puzzle->>'difficulty')::int AS difficulty,
        f.payload AS observed,g.participants_count,g.valid_distinct_guesses,g.guess_attempts,
        g.business_rejected,g.system_failed,g.nonterminal,coalesce(p.points,0) AS points
      FROM rounds r LEFT JOIN analytics_round_facts f ON f.round_id=r.id
      LEFT JOIN grouped g ON g.round_id=r.id LEFT JOIN points p ON p.round_id=r.id
      WHERE r.session_id=:sid ORDER BY r.created_at,r.id"""
    count=0; size=0
    def save(section,key,row):
        nonlocal count,size
        serialized=json_bytes(row)
        # The entire anonymous row is kept; an invalid observation cannot consume unbounded memory.
        if len(serialized)>4*1024*1024:
            raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Anonymous row exceeds recovery bound',413,details={'reason':'FINAL_ROW_BUDGET'})
        c.execute(m.final_rows.insert().values(session_id=sid,section=section,row_key=key,payload=row,size_bytes=len(serialized)))
        count+=1; size+=len(serialized)
    with c.execute(sa.text(query).execution_options(stream_results=True,yield_per=RECOVERY_PAGE),params).mappings() as rows:
        for ordinal,r in enumerate(rows,1):
            rf=r['observed'] or {}; void=r['end_reason']=='VOID' or r['status']=='VOID'
            normal=r['end_reason'] in {'SOLVED','TIMEOUT'} or r['status'] in {'SOLVED','TIMEOUT'}
            ref=rf.get('trace_ref') or hmac.new(service.signer.key,('trace:'+sid+':'+r['id']).encode(),'sha256').hexdigest()
            codes=rf.get('anomaly_codes',[])
            row=dict(round_id=r['id'],round_ordinal=rf.get('round_ordinal',ordinal),status=r['status'],end_reason=r['end_reason'],puzzle_version_id=r['puzzle_version_id'],category_id=r['category_id'],difficulty=r['difficulty'],has_anomaly=bool(codes),anomaly_codes=codes,trace_ref=ref,started_at=rf.get('started_at'),ended_at=rf.get('ended_at'),running_duration_ms=rf.get('running_duration_ms') if normal else None,**{k:0 if void else (r[k] or 0) for k in ['participants_count','valid_distinct_guesses','guess_attempts','business_rejected','system_failed','nonterminal']},points_net=0 if void else r['points'],hints_revealed=rf.get('hints_revealed'),completed_normally=normal,included_in_aggregates=not void,exclusion_reason='VOID' if void else None,winner_display_name=rf.get('winner_display_name'))
            save('rounds',str(row['round_ordinal']).zfill(20)+':'+r['id'],row)
    if params['started']:
        query=RAW_CTE+""", grouped AS (SELECT floor(extract(epoch FROM(received_at-CAST(:started AS timestamptz)))/60)::int AS bucket,"""+fields+"""
          FROM quality WHERE received_at>=CAST(:started AS timestamptz) AND received_at<CAST(:through AS timestamptz) GROUP BY bucket)
          SELECT t.bucket,t.start_at,least(t.start_at+interval '60 seconds',CAST(:through AS timestamptz)) AS end_at,
            g.participants_count,g.valid_distinct_guesses,g.guess_attempts,g.business_rejected,g.system_failed,g.nonterminal
          FROM (SELECT row_number() OVER(ORDER BY v)-1 AS bucket,v AS start_at
            FROM generate_series(CAST(:started AS timestamptz),CAST(:through AS timestamptz),interval '60 seconds') v
            WHERE v<CAST(:through AS timestamptz)) t LEFT JOIN grouped g ON g.bucket=t.bucket ORDER BY t.bucket"""
        with c.execute(sa.text(query).execution_options(stream_results=True,yield_per=RECOVERY_PAGE),params).mappings() as rows:
            for r in rows:
                row=dict(bucket_start=iso(r['start_at']),bucket_end=iso(r['end_at']),partial=(r['end_at']-r['start_at']).total_seconds()<60,**{k:r[k] or 0 for k in ['participants_count','valid_distinct_guesses','guess_attempts','business_rejected','system_failed','nonterminal']})
                save('minutes',row['bucket_start'],row)
    service.aggregate_sql(c,session,result,details=False)
    summary=c.execute(sa.text("""SELECT count(*) AS total,
      count(*) FILTER(WHERE payload->>'status' NOT IN ('REVEALED','INTERMISSION','CLOSED','SOLVED','TIMEOUT','VOID','INTERRUPTED')) AS active,
      count(*) FILTER(WHERE (payload->>'completed_normally')::boolean) AS completed,
      count(*) FILTER(WHERE payload->>'exclusion_reason'='VOID') AS void,
      count(*) FILTER(WHERE payload->>'end_reason'='INTERRUPTED' OR payload->>'status'='INTERRUPTED') AS interrupted,
      count(*) FILTER(WHERE payload->>'exclusion_reason' IS NULL AND payload->>'status' IN ('REVEALED','INTERMISSION','CLOSED','SOLVED','TIMEOUT','INTERRUPTED')) AS terminal,
      count(*) FILTER(WHERE (payload->>'completed_normally')::boolean AND payload->>'running_duration_ms' IS NOT NULL) AS duration_samples,
      percentile_cont(0.5) WITHIN GROUP(ORDER BY (payload->>'running_duration_ms')::bigint)
        FILTER(WHERE (payload->>'completed_normally')::boolean AND payload->>'running_duration_ms' IS NOT NULL) AS median,
      count(*) FILTER(WHERE payload->>'exclusion_reason' IS NULL AND payload->>'status' IN ('REVEALED','INTERMISSION','CLOSED','SOLVED','TIMEOUT','INTERRUPTED') AND payload->>'hints_revealed' IS NULL) AS missing_hints,
      count(*) FILTER(WHERE payload->>'exclusion_reason' IS NULL AND payload->>'status' IN ('REVEALED','INTERMISSION','CLOSED','SOLVED','TIMEOUT','INTERRUPTED') AND (payload->>'hints_revealed')::int>0) AS hinted
      FROM analytics_final_rows WHERE session_id=:sid AND section='rounds'"""),{'sid':sid}).mappings().one()
    from app.modules.analytics.service import ratio
    result.update(round_counts={k:summary[k] for k in ['total','active','completed','void','interrupted']},round_completion_rate=ratio(summary['completed'],summary['terminal']),median_round_duration_ms=float(summary['median']) if summary['median'] is not None else None,hint_rate=None if summary['missing_hints'] else ratio(summary['hinted'],summary['terminal']))
    result['denominators'].update(terminal_nonvoid_rounds=summary['terminal'],normal_completed_rounds=summary['completed'],duration_samples=summary['duration_samples'],hint_eligible_rounds=summary['terminal'])
    result.update(_chunked=True,_row_count=count,_row_bytes=size)
    c.execute(insert(m.finals).values(session_id=sid,payload=result,expires_at=at+timedelta(days=90)).on_conflict_do_nothing())
    frozen['anonymous_final_persisted']=True
    frozen.pop('materialization_basis_at',None); frozen.pop('materialization_watermark',None)
    c.execute(m.session_facts.update().where(m.session_facts.c.session_id==sid).values(payload=frozen))


def prune_session(service,c,sid,captured):
    """Scrub only final-backed comments. Keep durable key/ledger/effect/ACK references."""
    from app.modules.analytics.service import iso
    final=c.execute(sa.select(m.finals).where(m.finals.c.session_id==sid)).mappings().first()
    facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==sid)).scalar_one_or_none() or {}
    pending_effect=c.execute(sa.select(s.effects.c.id).where(s.effects.c.session_id==sid,s.effects.c.processed.is_(False)).limit(1)).first()
    pending_replay=c.execute(sa.select(s.replay_jobs.c.id).where(s.replay_jobs.c.session_id==sid,s.replay_jobs.c.status.notin_(['succeeded','failed','cancelled'])).limit(1)).first()
    pending_ack=False
    if c.execute(sa.text("SELECT to_regclass('connector_ack_outbox')")).scalar_one_or_none() is not None:
        pending_ack=bool(c.execute(sa.text("SELECT 1 FROM connector_ack_outbox WHERE session_id=:sid AND status IN ('pending','running','retry') LIMIT 1"),{'sid':sid}).first())
    protected=pending_effect or pending_replay or pending_ack
    if final and not facts.get('anonymous_final_persisted'):
        facts=dict(facts,anonymous_final_persisted=True)
        c.execute(m.session_facts.update().where(m.session_facts.c.session_id==sid).values(payload=facts))
    if (final or facts.get('anonymous_final_persisted')) and facts.get('sealed_at') and not protected:
        old=sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid,s.guesses.c.created_at<=captured-timedelta(days=7),~s.guesses.c.normalized_word.like('retained:%'))
        # A keyed digest preserves the unique unit constraint without retaining a dictionary-attackable word.
        with c.execute(sa.select(s.guesses.c.id,s.guesses.c.normalized_word).where(s.guesses.c.id.in_(old)).execution_options(stream_results=True,yield_per=RECOVERY_PAGE)).mappings() as rows:
            for r in rows:
                if not r['normalized_word'].startswith('retained:'):
                    digest=hmac.new(service.signer.key,('comment:'+sid+':'+r['normalized_word']).encode(),'sha256').hexdigest()
                    c.execute(s.guesses.update().where(s.guesses.c.id==r['id']).values(normalized_word='retained:'+digest))
        # A sealed session may span many days. Each identity's own latest raw input
        # must reach the horizon; an older guess cannot expire yesterday's nickname.
        last_guess=sa.select(sa.func.max(s.guesses.c.created_at)).where(s.guesses.c.session_id==sid,s.guesses.c.participant_id==s.participants.c.id).correlate(s.participants).scalar_subquery()
        last_observation=sa.select(sa.func.max(sa.cast(m.attempts.c.payload['received_at'].astext,sa.DateTime(timezone=True)))).where(m.attempts.c.session_id==sid,m.attempts.c.payload['participant_id'].astext==s.participants.c.id).correlate(s.participants).scalar_subquery()
        latest_input=sa.func.greatest(s.participants.c.created_at,sa.func.coalesce(last_guess,s.participants.c.created_at),sa.func.coalesce(last_observation,s.participants.c.created_at))
        eligible_people=sa.select(s.participants.c.id,s.participants.c.identity_key).where(s.participants.c.session_id==sid,latest_input<=captured-timedelta(days=7))
        with c.execute(eligible_people.execution_options(stream_results=True,yield_per=RECOVERY_PAGE)).mappings() as rows:
            for r in rows:
                if r['identity_key'].startswith('retained:'): continue
                digest=hmac.new(service.signer.key,('identity:'+sid+':'+r['identity_key']).encode(),'sha256').hexdigest()
                c.execute(s.participants.update().where(s.participants.c.id==r['id']).values(identity_key='retained:'+digest,display_name=''))
        hook=service.connector_retention
        if callable(hook):
            hook(c,sid,captured-timedelta(days=7))
        elif c.execute(sa.text("SELECT to_regclass('connector_deliveries')")).scalar_one_or_none() is not None:
            if c.execute(sa.text("SELECT 1 FROM connector_deliveries WHERE session_id=:sid LIMIT 1"),{'sid':sid}).first():
                raise DomainError('DEPENDENCY_UNAVAILABLE','Connector retention port is not bound',503,True,details={'reason':'CONNECTOR_RETENTION_UNBOUND'})
        # Immutable authorized event frames are removed only as an expired prefix.
        # Their sequence/hash boundary remains durable, so reconnect sees a real gap.
        for audience in ['DISPLAY','STUDIO']:
            blocked=c.execute(sa.select(sa.func.min(s.outbox.c.seq)).where(s.outbox.c.session_id==sid,s.outbox.c.audience==audience,s.outbox.c.created_at>captured-timedelta(days=7))).scalar_one()
            expired=s.outbox.c.session_id==sid,s.outbox.c.audience==audience,s.outbox.c.created_at<=captured-timedelta(days=7)
            query=sa.select(s.outbox.c.id,s.outbox.c.seq,s.outbox.c.payload).where(*expired)
            if blocked is not None: query=query.where(s.outbox.c.seq<blocked)
            digest=hashlib.sha256(); through=None; batch=[]
            previous=c.execute(sa.select(m.event_retention.c.payload_hash).where(m.event_retention.c.session_id==sid,m.event_retention.c.audience==audience)).scalar_one_or_none()
            if previous: digest.update(previous.encode())
            with c.execute(query.order_by(s.outbox.c.seq).execution_options(stream_results=True,yield_per=RECOVERY_PAGE)).mappings() as rows:
                for r in rows:
                    digest.update(str(r['seq']).encode()+b':'+json.dumps(r['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
                    through=r['seq']; batch.append(r['id'])
                    if len(batch)==RECOVERY_PAGE:
                        c.execute(s.outbox.delete().where(s.outbox.c.id.in_(batch))); batch=[]
                if batch: c.execute(s.outbox.delete().where(s.outbox.c.id.in_(batch)))
            if through is not None:
                c.execute(insert(m.event_retention).values(session_id=sid,audience=audience,through_seq=through,payload_hash=digest.hexdigest()).on_conflict_do_update(index_elements=[m.event_retention.c.session_id,m.event_retention.c.audience],set_={'through_seq':through,'payload_hash':digest.hexdigest()}))
        # Processed effects retain source/delivery/terminal identity for duplicate receipts.
        # No active frame or grant can consume this expired payload again.
        with c.execute(sa.select(s.effects.c.id,s.effects.c.payload).where(s.effects.c.session_id==sid,s.effects.c.processed.is_(True),s.effects.c.created_at<=captured-timedelta(days=7)).execution_options(stream_results=True,yield_per=RECOVERY_PAGE)).mappings() as rows:
            for r in rows:
                if r['payload'].get('retained'): continue
                digest=hashlib.sha256(json.dumps(r['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                c.execute(s.effects.update().where(s.effects.c.id==r['id']).values(payload={'retained':True,'payload_hash':digest}))
        # Keep replay terminal count/hash/checkpoint and event ordinals, remove comment bodies.
        c.execute(sa.text("""UPDATE replay_jobs SET input=jsonb_build_object('events',
          (SELECT coalesce(jsonb_agg('{}'::jsonb),'[]'::jsonb) FROM jsonb_array_elements(input->'events')))
          WHERE session_id=:sid AND status IN ('succeeded','failed','cancelled') AND created_at<=:cutoff"""),{'sid':sid,'cutoff':captured-timedelta(days=7)})
        # Idempotency response keeps feedback identity/status and request hash after raw fields expire.
        cutoff=captured-timedelta(days=7)
        feedbacks=sa.union(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid,s.guesses.c.created_at<=cutoff),sa.select(s.rejections.c.id).where(s.rejections.c.session_id==sid,s.rejections.c.created_at<=cutoff))
        # Only real feedback-producing command paths participate. A legacy job/cache
        # with a coincidentally named field must not lose draft/evaluation content.
        feedback_path=sa.or_(s.idempotency.c.path=='/internal/rehearsal-input',s.idempotency.c.path=='/api/v1/rehearsal/guesses',s.idempotency.c.path=='/api/v1/replays',s.idempotency.c.path.like('/api/v1/replays/%'))
        c.execute(s.idempotency.update().where(s.idempotency.c.method=='POST',feedback_path,s.idempotency.c.response['feedback_id'].astext.in_(feedbacks),s.idempotency.c.created_at<=cutoff).values(response=s.idempotency.c.response.op('||')(sa.cast(sa.literal(json.dumps({'word':None,'display_name':''})),sa.dialects.postgresql.JSONB))))
        # Only mark a whole-session sweep once its latest raw row has reached the policy horizon.
        recent=c.execute(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid,s.guesses.c.created_at>captured-timedelta(days=7)).limit(1)).first()
        unsanitized_person=c.execute(sa.select(s.participants.c.id).where(s.participants.c.session_id==sid,~s.participants.c.identity_key.like('retained:%')).limit(1)).first()
        if not recent and not unsanitized_person:
            facts=dict(facts,raw_scrubbed_at=iso(captured))
            c.execute(m.session_facts.update().where(m.session_facts.c.session_id==sid).values(payload=facts))
    service.prune_expired_traces(c,sid,captured)
    identities=sa.union(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid),sa.select(s.rejections.c.id).where(s.rejections.c.session_id==sid))
    c.execute(s.trace_steps.delete().where(s.trace_steps.c.message_id.in_(identities),s.trace_steps.c.time<=captured-timedelta(days=30)))
    if final and final['expires_at']<=captured:
        c.execute(m.final_rows.delete().where(m.final_rows.c.session_id==sid))
        c.execute(m.finals.delete().where(m.finals.c.session_id==sid))
        # Facts retain only terminal retention/opaque-reference metadata, never expired aggregates.
        c.execute(m.attempts.delete().where(m.attempts.c.session_id==sid))
        c.execute(m.latencies.delete().where(m.latencies.c.session_id==sid))
        c.execute(m.round_facts.update().where(m.round_facts.c.session_id==sid).values(payload=sa.func.jsonb_build_object('trace_ref',m.round_facts.c.payload['trace_ref'].astext,'trace_expires_at',m.round_facts.c.payload['trace_expires_at'].astext)))


class AnalyticsMaintenanceWorker:
    """Single-process lifecycle port; stopping waits for the current DB use case."""
    def __init__(self,service,interval_seconds=60):
        if not 0<interval_seconds<=60: raise ValueError('Maintenance interval must be within 60 seconds')
        self.service=service; self.interval_seconds=interval_seconds
        self.task=None; self.stopping=None; self.last_error=None

    async def run_once(self):
        import asyncio
        try:
            await asyncio.to_thread(self.service.maintenance_tick)
            self.last_error=None
        except Exception as error:
            # Only a stable reason is retained; raw DB/OS exceptions may contain private values.
            self.last_error=getattr(error,'code','ANALYTICS_MAINTENANCE_FAILED')

    async def start(self):
        import asyncio
        if self.task is not None: return
        self.stopping=asyncio.Event()
        await self.run_once()
        self.task=asyncio.create_task(self.run())

    async def run(self):
        import asyncio
        while not self.stopping.is_set():
            try: await asyncio.wait_for(self.stopping.wait(),timeout=self.interval_seconds)
            except TimeoutError: await self.run_once()

    async def stop(self):
        if self.task is None: return
        self.stopping.set()
        await self.task
        self.task=None
