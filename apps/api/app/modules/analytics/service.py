"""Frozen local analytics: calculation, signed keyset pages, bounded query cache and CSV."""
import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import errno
import stat
import itertools
import inspect
from functools import wraps
import secrets
import statistics
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.analytics import models as m

VERSION='LW-BE-ANALYTICS/1.1.0'
EXTENSION='LW-BE/1.1.0'
CACHE_LOCK=839213560171
MAX_ROWS=20000
MAX_BYTES=4*1024*1024
MAX_TOTAL_BYTES=1024*1024*1024
MAX_EXPORT_BYTES=32*1024*1024
TERMINAL={'REVEALED','INTERMISSION','CLOSED','SOLVED','TIMEOUT','VOID','INTERRUPTED'}
CSV_FIELDS=['be_extension_version', 'context', 'expires_at', 'final', 'denominators', 'round_counts', 'participants_count', 'valid_distinct_guesses', 'guesses_per_participant', 'round_completion_rate', 'median_round_duration_ms', 'hint_rate', 'invalid_ratio', 'pipeline_latency_p50_ms', 'pipeline_latency_p95_ms', 'pipeline_latency_p99_ms', 'source_mode_counts', 'attempt_outcomes', 'latency_missing_samples', 'points_net', 'source_mode', 'provider_test', 'eligible_for_production', 'messages', 'guess_attempts', 'valid_guess_units', 'excluded_reason', 'bucket_start', 'bucket_end', 'partial', 'business_rejected', 'system_failed', 'nonterminal', 'round_id', 'round_ordinal', 'status', 'end_reason', 'puzzle_version_id', 'category_id', 'difficulty', 'has_anomaly', 'anomaly_codes', 'trace_ref', 'started_at', 'ended_at', 'running_duration_ms', 'hints_revealed', 'completed_normally', 'included_in_aggregates', 'exclusion_reason', 'winner_display_name']
SUCCESS={'scored','succeeded','exact'}
FAILED={'system_failed','failed'}
REJECTED={'business_rejected','rejected'}

def moment(): return datetime.now(timezone.utc)
def iso(value): return value.isoformat(timespec='milliseconds').replace('+00:00','Z') if value else None
def stamp(value): return datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
def json_bytes(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def ratio(n,d): return n/d if d else None

def outcome(row):
    status=row.get('outcome',row.get('status','unknown'))
    if status in SUCCESS or status=='successful': return 'successful'
    if status in FAILED: return 'system_failed'
    if status in REJECTED: return 'business_rejected'
    return 'nonterminal'

def counts(rows):
    values={x:sum(outcome(g)==x for g in rows) for x in ['successful','business_rejected','system_failed','nonterminal']}
    valid=[g for g in rows if outcome(g)=='successful']
    return dict(participants_count=len({g['participant_id'] for g in valid}),valid_distinct_guesses=len(valid),
                guess_attempts=len(rows)-values['nonterminal'],**{x:values[x] for x in ['business_rejected','system_failed','nonterminal']})

def storage_guard(function):
    """Translate PostgreSQL capacity failures without leaking driver details."""
    def translate(error):
        code=getattr(error.orig,'sqlstate',None) or getattr(error.orig,'pgcode',None)
        if code in {'53100','53200','53300'}:
            reason='STORAGE_QUOTA' if code=='53100' else 'DATABASE_CAPACITY'
            raise DomainError('ANALYTICS_CAPACITY_BUSY','Analytics storage unavailable',503,True,details={'reason':reason}) from None
        raise error
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def generator(*args,**kwargs):
            try: yield from function(*args,**kwargs)
            except sa.exc.DBAPIError as error: translate(error)
        return generator
    @wraps(function)
    def normal(*args,**kwargs):
        try: return function(*args,**kwargs)
        except sa.exc.DBAPIError as error: translate(error)
    return normal

class CursorSigner:
    def __init__(self,key):
        if not isinstance(key,bytes) or len(key)<32: raise DomainError('ANALYTICS_CONFIG_INVALID','A dedicated cursor key of at least 32 bytes is required',503,details={'reason':'CURSOR_KEY_INVALID'})
        self.key=key
    def encode(self,snapshot,session,section,limit,last):
        body=json_bytes([snapshot,session,section,hashlib.sha256(b'{}').hexdigest(),limit,last])
        return base64.urlsafe_b64encode(body+hmac.digest(self.key,body,'sha256')).decode().rstrip('=')
    def decode(self,token,snapshot,session,section,limit):
        try:
            if len(token)>4096: raise ValueError()
            raw=base64.b64decode(token+'='*(-len(token)%4),altchars=b'-_',validate=True)
            body,sig=raw[:-32],raw[-32:]
            if not hmac.compare_digest(hmac.digest(self.key,body,'sha256'),sig): raise ValueError()
            value=json.loads(body)
            if value[:5]!=[snapshot,session,section,hashlib.sha256(b'{}').hexdigest(),limit]: raise ValueError()
            return value[5]
        except (ValueError,TypeError,IndexError,json.JSONDecodeError):
            raise DomainError('ANALYTICS_CURSOR_INVALID','Cursor does not match this snapshot query',422) from None

def calculate(session,rounds,guesses,ledger,attempts,latencies,facts,round_facts,captured):
    """Pure calculation. Unobserved time/relay facts remain unknown, never inferred from row creation."""
    sid=session['id']; by_round={r['id']:r for r in rounds}
    void={r['id'] for r in rounds if r.get('end_reason')=='VOID' or r['status']=='VOID'}
    # An observation replacing an admitted guess is keyed by guess_id; rejection/chat rows have independent ids.
    overrides={a.get('guess_id'):a for a in attempts if a.get('guess_id')}
    all_attempts=[dict(g,**overrides.get(g['id'],{})) for g in guesses]
    all_attempts += [a for a in attempts if not a.get('guess_id') and a.get('is_guess_attempt',True)]
    production=session['mode']=='official' and session['run_purpose']=='production'
    def eligible(g): return not production or (g.get('source_mode')=='official' and g.get('provider_test') is False)
    assigned=[g for g in all_attempts if g.get('round_id') in by_round and g['round_id'] not in void and eligible(g)]
    valid=[g for g in assigned if outcome(g)=='successful']
    totals=counts(assigned)
    normal=[r for r in rounds if r.get('end_reason') in {'SOLVED','TIMEOUT'} or r['status'] in {'SOLVED','TIMEOUT'}]
    terminal=[r for r in rounds if r['id'] not in void and (r['status'] in TERMINAL or r.get('end_reason') in {'SOLVED','TIMEOUT','INTERRUPTED'})]
    durations=[round_facts[r['id']]['running_duration_ms'] for r in normal if round_facts.get(r['id'],{}).get('running_duration_ms') is not None]
    hinted=[r for r in terminal if round_facts.get(r['id'],{}).get('hints_revealed',0)>0]
    hint_known=all('hints_revealed' in round_facts.get(r['id'],{}) for r in terminal)
    latmap={x['guess_id']:x for x in latencies}
    samples=[]
    for g in valid:
        obs=latmap.get(g['id'],{})
        if obs.get('admission_boot_id') and obs.get('admission_boot_id')==obs.get('feedback_boot_id') and obs.get('admission_commit_mono_ns') is not None and obs.get('feedback_commit_mono_ns') is not None:
            delta=obs['feedback_commit_mono_ns']-obs['admission_commit_mono_ns']
            if delta>=0: samples.append(delta/1000000)
    samples.sort()
    started=stamp(facts.get('started_at')); ended=stamp(facts.get('ended_at')); through=min(ended,captured) if ended else captured
    minutes=[]
    if started:
        count=max(0,math.ceil((through-started).total_seconds()/60))
        if count+len(rounds)>MAX_ROWS: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Snapshot row budget exceeded',413,details={'reason':'ROW_LIMIT'})
        for i in range(count):
            start=started+timedelta(seconds=i*60); end=min(start+timedelta(seconds=60),through)
            rows=[g for g in assigned if g.get('received_at',g.get('created_at')) and start<=stamp(g.get('received_at',g.get('created_at')))<end]
            minutes.append(dict(bucket_start=iso(start),bucket_end=iso(end),partial=(end-start).total_seconds()<60,**counts(rows)))
    details=[]
    for ordinal,r in enumerate(rounds,1):
        rf=round_facts.get(r['id'],{}); rows=[g for g in all_attempts if g.get('round_id')==r['id'] and eligible(g)]
        codes=list(rf.get('anomaly_codes',[])); is_void=r['id'] in void
        points=0 if is_void else sum(x['delta'] for x in ledger if x['round_id']==r['id'])
        details.append(dict(round_id=r['id'],round_ordinal=rf.get('round_ordinal',ordinal),status=r['status'],end_reason=r.get('end_reason'),puzzle_version_id=r['puzzle_version_id'],
            category_id=r.get('frozen_puzzle',{}).get('category_id'),difficulty=r.get('frozen_puzzle',{}).get('difficulty'),
            has_anomaly=bool(codes),anomaly_codes=codes,trace_ref=rf.get('trace_ref'),started_at=rf.get('started_at'),ended_at=rf.get('ended_at'),
            running_duration_ms=rf.get('running_duration_ms') if r in normal else None,**counts([] if is_void else rows),points_net=points,
            hints_revealed=rf.get('hints_revealed'),completed_normally=r in normal,included_in_aggregates=not is_void,exclusion_reason='VOID' if is_void else None,winner_display_name=rf.get('winner_display_name')))
    details.sort(key=lambda x:(x['round_ordinal'],x['round_id']))
    partitions={}
    messages=[g for g in guesses if g['id'] not in overrides]+attempts
    for a in messages:
        source=a.get('source_mode','unknown'); test=a.get('provider_test'); key=(source,test)
        part=partitions.setdefault(key,dict(source_mode=source,provider_test=test,eligible_for_production=session['mode']=='official' and session['run_purpose']=='production' and source=='official' and test is False,messages=0,guess_attempts=0,valid_guess_units=0,excluded_reason=None))
        part['messages']+=1
        if not part['eligible_for_production']: part['excluded_reason']='excluded_unknown' if source=='unknown' or (source=='official' and test is None) else ('test_live' if session['run_purpose']=='test_live' else source)
    for a in all_attempts:
        key=(a.get('source_mode','unknown'),a.get('provider_test'))
        if key not in partitions:
            source,test=key; eligible=session['mode']=='official' and session['run_purpose']=='production' and source=='official' and test is False
            partitions[key]=dict(source_mode=source,provider_test=test,eligible_for_production=eligible,messages=0,guess_attempts=0,valid_guess_units=0,excluded_reason=None if eligible else 'excluded_unknown')
        partitions[key]['guess_attempts']+=outcome(a)!='nonterminal'
        partitions[key]['valid_guess_units']+=a in valid
    nonterminal=dict(admission=facts.get('admission_nonterminal',0),score_jobs=facts.get('score_jobs_nonterminal',sum(outcome(g)=='nonterminal' for g in guesses)),commands=facts.get('commands_nonterminal',0))
    sealed=bool(facts.get('sealed_at'))
    state='sealed' if sealed else ('open' if session['status'] in {'ACTIVE','CREATED'} else 'draining')
    return dict(be_extension_version=EXTENSION,metric_definition_version=VERSION,
        context=dict(session_id=sid,mode=session['mode'],run_purpose=session['run_purpose'],live_profile=facts.get('live_profile'),candidate_digest=facts.get('candidate_digest')),
        as_of=dict(captured_at=iso(captured),local_commit_watermark=str(session['analytics_commit_counter']),studio_as_of_seq=str(session['studio_seq'])),
        coverage=dict(started_at=iso(started),ended_at=iso(ended),through_at=iso(through),relay_coverage=facts.get('relay_coverage','unknown'),relay_confirmed_stream_seq=facts.get('relay_confirmed_stream_seq')),
        closure=dict(state=state,sealed_at=facts.get('sealed_at'),local_nonterminal=nonterminal,local_delivery_pending=facts.get('local_delivery_pending',dict(outbox=0,display_effects=0,ack_outbox=0)),remote_fulfillment_pending=facts.get('remote_fulfillment_pending'),unresolved_gap=facts.get('unresolved_gap',False)),
        denominators=dict(valid_guess_units=len(valid),participants=totals['participants_count'],terminal_nonvoid_rounds=len(terminal),normal_completed_rounds=len(normal),guess_attempts=totals['guess_attempts'],invalid_attempts=totals['business_rejected'],duration_samples=len(durations),latency_samples=len(samples),hint_eligible_rounds=len(terminal)),
        round_counts=dict(total=len(rounds),active=sum(r['status'] not in TERMINAL for r in rounds),completed=len(normal),void=len(void),interrupted=sum(r.get('end_reason')=='INTERRUPTED' or r['status']=='INTERRUPTED' for r in rounds)),
        participants_count=totals['participants_count'],valid_distinct_guesses=len(valid),guesses_per_participant=ratio(len(valid),totals['participants_count']),round_completion_rate=ratio(len(normal),len(terminal)),
        median_round_duration_ms=statistics.median(durations) if durations else None,hint_rate=ratio(len(hinted),len(terminal)) if hint_known else None,
        invalid_ratio=ratio(totals['business_rejected'],totals['guess_attempts']),
        **{'pipeline_latency_p'+str(p)+'_ms':samples[math.ceil(p/100*len(samples))-1] if samples else None for p in [50,95,99]},
        latency_missing_samples=len(valid)-len(samples),source_mode_counts={source:sum(p['messages'] for p in partitions.values() if p['source_mode']==source) for source,_ in partitions},
        source_partitions=list(partitions.values()),attempt_outcomes=dict(successful=len(valid),business_rejected=totals['business_rejected'],system_failed=totals['system_failed'],nonterminal=totals['nonterminal'],void_excluded=sum(g.get('round_id') in void for g in all_attempts),unassigned_round=sum(g.get('round_id') not in by_round for g in all_attempts)),
        points_net=sum(x['delta'] for x in ledger if x['round_id'] not in void),final=sealed,_minutes=minutes,_rounds=details)

RAW_CTE="""
        WITH raw AS (
          SELECT g.id, g.round_id, g.participant_id,
            coalesce(o.payload->>'outcome',g.status) AS state,
            coalesce(o.payload->>'source_mode',g.source_mode,'unknown') AS source_mode,
            (o.payload->>'provider_test')::boolean AS provider_test,
            coalesce((o.payload->>'received_at')::timestamptz,g.created_at) AS received_at,
            true AS is_guess
          FROM guesses g LEFT JOIN analytics_attempt_observations o
            ON o.session_id=g.session_id AND o.payload->>'guess_id'=g.id
          WHERE g.session_id=:sid
          UNION ALL
          SELECT o.id,o.payload->>'round_id',o.payload->>'participant_id',
            coalesce(o.payload->>'outcome','unknown'),coalesce(o.payload->>'source_mode','unknown'),
            (o.payload->>'provider_test')::boolean,(o.payload->>'received_at')::timestamptz,
            coalesce((o.payload->>'is_guess_attempt')::boolean,true)
          FROM analytics_attempt_observations o
          WHERE o.session_id=:sid AND o.payload->>'guess_id' IS NULL
        ), facts AS (
          SELECT raw.*,r.id IS NOT NULL AS assigned,
            coalesce(r.end_reason='VOID' OR r.status='VOID',false) AS is_void,
            state IN ('scored','succeeded','exact','successful') AS successful,
            state IN ('system_failed','failed') AS system_failed,
            state IN ('business_rejected','rejected') AS business_rejected,
            state NOT IN ('scored','succeeded','exact','successful','system_failed','failed','business_rejected','rejected') AS nonterminal,
            (NOT :production OR (source_mode='official' AND provider_test IS FALSE)) AS eligible
          FROM raw LEFT JOIN rounds r ON r.id=raw.round_id AND r.session_id=:sid
        ), quality AS (
          SELECT * FROM facts WHERE assigned AND NOT is_void AND eligible AND is_guess
        )
        """


class AnalyticsService:
    def __init__(self,database,cursor_key):
        self.db=database; self.signer=CursorSigner(cursor_key)
        self.connector_retention=None

    def observe(self,c,kind,session_id,facts):
        """Called inside the domain transaction BEFORE its final touch(); never takes business locks.

        UTC timestamps must come from the transition; mono_ns/boot_id from the real clock.
        Only approved anonymous facts enter these tables. The domain owns sealing decisions.
        """
        existing=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==session_id)).scalar_one_or_none() or {}
        if existing.get('sealed_at') or existing.get('materialization_pending_reason'): return
        def merge(table,key,identity,payload):
            old=c.execute(sa.select(table.c.payload).where(key==identity)).scalar_one_or_none() or {}
            values={key.name:identity,'payload':old|payload}
            if 'session_id' in table.c and key.name!='session_id': values['session_id']=session_id
            c.execute(insert(table).values(**values).on_conflict_do_update(index_elements=[key],set_={'payload':old|payload}))
        if kind.startswith('session_'):
            allowed={'started_at','ended_at','sealed_at','live_profile','candidate_digest','relay_coverage','relay_confirmed_stream_seq','admission_nonterminal','commands_nonterminal','local_delivery_pending','remote_fulfillment_pending','unresolved_gap'}
            merge(m.session_facts,m.session_facts.c.session_id,session_id,{k:v for k,v in facts.items() if k in allowed})
            if kind=='session_ended': self.try_seal_session(c,session_id)
        elif kind.startswith('round_') or kind=='hint_revealed':
            rid=facts['round_id']; old=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==rid)).scalar_one_or_none() or {}
            allowed={'round_ordinal','trace_ref','started_at','ended_at','hints_revealed','anomaly_codes','winner_display_name','running_duration_ms'}
            patch={k:v for k,v in facts.items() if k in allowed}
            if facts.get('winner_id'):
                patch['winner_display_name']=c.execute(sa.select(s.participants.c.display_name).where(s.participants.c.id==facts['winner_id'],s.participants.c.session_id==session_id)).scalar_one_or_none()
            if facts.get('reason')=='INTERRUPTED': patch['anomaly_codes']=list(dict.fromkeys(old.get('anomaly_codes',[])+['INTERRUPTED']))
            if kind=='round_started':
                if 'round_ordinal' not in old:
                    patch['round_ordinal']=c.execute(sa.select(sa.func.count()).select_from(m.round_facts).where(m.round_facts.c.session_id==session_id)).scalar_one()+1
                patch['trace_ref']=old.get('trace_ref',secrets.token_urlsafe(24))
                patch['hints_revealed']=old.get('hints_revealed',0)
            if kind in {'round_started','round_resumed'} and facts.get('mono_ns') is not None:
                patch.update(run_started_ns=facts['mono_ns'],run_boot_id=facts.get('boot_id'))
            if kind in {'round_paused','round_settled'} and old.get('run_started_ns') is not None:
                delta=facts.get('mono_ns',-1)-old['run_started_ns']
                if old.get('run_boot_id')==facts.get('boot_id') and delta>=0:
                    patch['running_duration_ms']=old.get('running_duration_ms',0)+delta//1000000
                else: patch['running_duration_ms']=None
                patch['run_started_ns']=None
            if kind=='hint_revealed': patch['hints_revealed']=old.get('hints_revealed',0)+1
            merge(m.round_facts,m.round_facts.c.round_id,rid,patch)
        elif kind in {'guess_terminal','guess_rejected','source_message'}:
            allowed={'guess_id','round_id','participant_id','source_mode','provider_test','received_at','outcome','is_guess_attempt'}
            identity=facts.get('observation_id') or facts.get('guess_id')
            if not identity: raise ValueError('A stable deduplication observation_id is required')
            merge(m.attempts,m.attempts.c.id,identity,{k:v for k,v in facts.items() if k in allowed})
        else: raise ValueError('Unsupported analytics observation kind')

    def after_commit(self,session_id,guess_id,phase,boot_id,mono_ns):
        """Independent short transaction; call only AFTER the corresponding commit returned successfully."""
        if phase not in {'admission','feedback'} or not isinstance(mono_ns,int) or mono_ns<0: raise ValueError('Invalid commit observation')
        with self.db.engine.begin() as c:
            # Domain lock first, then observations, then the counter as the final business write.
            row=c.execute(sa.select(s.sessions).where(s.sessions.c.id==session_id).with_for_update()).mappings().one()
            facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==session_id)).scalar_one_or_none() or {}
            if facts.get('sealed_at') or facts.get('materialization_pending_reason'): return  # Late samples cannot rewrite the sealed aggregate.
            old=c.execute(sa.select(m.latencies.c.payload).where(m.latencies.c.guess_id==guess_id)).scalar_one_or_none() or {}
            if phase+'_commit_mono_ns' in old: return
            payload=old|{'guess_id':guess_id,phase+'_boot_id':boot_id,phase+'_commit_mono_ns':mono_ns}
            c.execute(insert(m.latencies).values(guess_id=guess_id,session_id=session_id,payload=payload).on_conflict_do_update(index_elements=[m.latencies.c.guess_id],set_={'payload':payload}))
            self.try_seal_session(c,session_id)
            c.execute(s.sessions.update().where(s.sessions.c.id==session_id).values(analytics_commit_counter=row['analytics_commit_counter']+1))
            self.persist_sealed(c,session_id)

    def try_seal_session(self,c,session_id):
        """Termination/recovery hook only; caller already holds the session business lock.

        No GET invokes this function. The local component has synchronous commands and
        no admission queue beyond persisted guesses/jobs. Official gap status must be explicit.
        """
        session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==session_id)).mappings().one()
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==session_id)).scalar_one_or_none() or {}
        if facts.get('sealed_at'): return True
        if facts.get('materialization_pending_reason'): return False
        if session['status']!='ENDED' or not facts.get('ended_at') or facts.get('unresolved_gap'): return False
        if session['mode']=='official':
            # Official sealing needs positive evidence that the relay stream closed
            # without a gap. The connector publishes that as relay_coverage; no code
            # writes a bare unresolved_gap key before sealing, so requiring the key
            # alone would leave every official session permanently unsealed.
            delivery=c.execute(sa.select(m.delivery.c.payload).where(m.delivery.c.session_id==session_id)).scalar_one_or_none() or {}
            if delivery.get('relay_coverage')!='complete': return False
        if facts.get('admission_nonterminal',0) or facts.get('commands_nonterminal',0): return False
        active=c.execute(sa.select(sa.func.count()).select_from(s.rounds).where(s.rounds.c.session_id==session_id,s.rounds.c.status.notin_(list(TERMINAL)))).scalar_one()
        jobs=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==session_id,s.score_jobs.c.status.notin_(['succeeded','failed','cancelled']))).scalar_one()
        pending=c.execute(sa.select(sa.func.count()).select_from(s.guesses).where(s.guesses.c.session_id==session_id,s.guesses.c.status.notin_(list(SUCCESS|FAILED|REJECTED)))).scalar_one()
        if active or jobs or pending: return False
        # Upgrade observer-free historical rounds only in this business transaction.
        for row in c.execute(sa.select(s.rounds.c.id,s.rounds.c.created_at).where(s.rounds.c.session_id==session_id)).mappings():
            old=c.execute(sa.select(m.round_facts.c.payload).where(m.round_facts.c.round_id==row['id'])).scalar_one_or_none() or {}
            payload=old|{'trace_ref':old.get('trace_ref') or hmac.new(self.signer.key,('trace:'+session_id+':'+row['id']).encode(),'sha256').hexdigest()}
            c.execute(insert(m.round_facts).values(round_id=row['id'],session_id=session_id,payload=payload).on_conflict_do_update(index_elements=[m.round_facts.c.round_id],set_={'payload':payload}))
            AnalyticsService.persist_round_trace(c,session_id,row['id'],payload['trace_ref'],stamp(payload.get('started_at')) or row['created_at'])
        payload={key:value for key,value in facts.items() if key!='materialization_pending_reason'}|{'sealed_at':iso(c.execute(sa.text('SELECT transaction_timestamp()')).scalar_one()),'unresolved_gap':False}
        c.execute(m.session_facts.update().where(m.session_facts.c.session_id==session_id).values(payload=payload))
        return True

    @contextmanager
    def locked(self):
        # Session advisory lock is acquired BEFORE the MVCC transaction starts. A waiting
        # creator therefore cannot read stale cache quotas from a pre-lock RR snapshot.
        with self.db.engine.connect() as c:
            c.execute(sa.text('SELECT pg_advisory_lock(:key)'),{'key':CACHE_LOCK}); c.commit()
            try: yield c
            finally:
                c.rollback()
                c.execution_options(isolation_level='READ COMMITTED')
                c.execute(sa.text('SELECT pg_advisory_unlock(:key)'),{'key':CACHE_LOCK}); c.commit()

    @staticmethod
    def authorize(c,sid,actor):
        row=c.execute(sa.select(s.sessions).where(s.sessions.c.id==sid)).mappings().first()
        if not row or row['actor_id']!=actor: raise DomainError('FORBIDDEN','Session ownership required',403)
        return dict(row)

    @staticmethod
    def unavailable(reason='NOT_AVAILABLE'):
        return DomainError('ANALYTICS_SNAPSHOT_UNAVAILABLE','Request a new first page',410,details={'reason':reason})

    @staticmethod
    def refresh_final(baseline,session,captured,delivery):
        """A new query snapshot observes current diagnostics over the frozen sealed business basis."""
        return baseline|{
            'as_of':{'captured_at':iso(captured),'local_commit_watermark':str(session['analytics_commit_counter']),'studio_as_of_seq':str(session['studio_seq'])},
            'closure':baseline['closure']|{'local_delivery_pending':dict(delivery)},
        }

    @staticmethod
    def delivery_projection(c,session):
        if not hasattr(s,'effects') or c.execute(sa.text("SELECT to_regclass('display_effects')")).scalar_one_or_none() is None:
            raise DomainError('DEPENDENCY_UNAVAILABLE','Realtime effect projection is not migrated',503,True,details={'reason':'EFFECT_SCHEMA_MISSING'})
        effects=c.execute(sa.select(sa.func.count()).select_from(s.effects).where(s.effects.c.session_id==session['id'],s.effects.c.processed.is_(False))).scalar_one()
        # This architecture reads the immutable replay log directly, with no dispatcher job queue.
        # Nonofficial modes have no official ACK tasks. Neither fact describes remote delivery.
        observed=c.execute(sa.select(m.delivery.c.payload).where(m.delivery.c.session_id==session['id'])).scalar_one_or_none() or {}
        ack=observed.get('ack_outbox',0)
        if c.execute(sa.text("SELECT to_regclass('connector_ack_outbox')")).scalar_one_or_none() is not None:
            ack=c.execute(sa.text("SELECT count(*) FROM connector_ack_outbox WHERE session_id=:sid AND status IN ('pending','running','retry')"),{'sid':session['id']}).scalar_one()
        elif session['mode']=='official' and not observed:
            raise DomainError('DEPENDENCY_UNAVAILABLE','Local ACK schema or trusted observation is required',503,True,details={'reason':'ACK_SCHEMA_MISSING'})
        return {'outbox':0,'display_effects':effects,'ack_outbox':ack}

    def persist_sealed(self,c,session_id):
        """Do not promote a query resource limit into a failed business termination.

        Oversized final materialization is explicitly pending. Only this exact budget
        failure is recoverable here; database, authorization and dependency errors
        propagate and preserve normal transaction rollback semantics.
        """
        try:
            with c.begin_nested(): self._persist_sealed(c,session_id)
        except DomainError as error:
            if error.code!='ANALYTICS_SNAPSHOT_TOO_LARGE': raise
            facts=dict(c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==session_id)).scalar_one())
            facts['materialization_basis_at']=facts.pop('sealed_at',None)
            facts['materialization_watermark']=c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id==session_id)).scalar_one()
            facts['materialization_pending_reason']='FINAL_AGGREGATE_TOO_LARGE'
            c.execute(m.session_facts.update().where(m.session_facts.c.session_id==session_id).values(payload=facts))

    def _persist_sealed(self,c,session_id):
        """Business transaction tail, AFTER its final counter/studio writes.

        Caller owns the session lock. This port does not acquire business locks or
        invoke remote projections. A failed aggregate write rolls back the seal,
        counter, settlement and this durable basis together.
        """
        session=dict(c.execute(sa.select(s.sessions).where(s.sessions.c.id==session_id)).mappings().one())
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==session_id)).scalar_one_or_none() or {}
        if not facts.get('sealed_at'): return
        if c.execute(sa.select(m.finals.c.session_id).where(m.finals.c.session_id==session_id)).first(): return
        if facts.get('anonymous_final_persisted'): return  # Expiry/missing basis is never repaired from retained raw rows.
        captured=stamp(facts['sealed_at'])
        result=self.business_payload(c,session,captured)
        if len(result['_minutes'])+len(result['_rounds'])>MAX_ROWS or len(json_bytes(result))>MAX_BYTES:
            raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Sealed aggregate budget exceeded',413,details={'reason':'FINAL_AGGREGATE_BUDGET'})
        c.execute(insert(m.finals).values(session_id=session_id,payload=result,expires_at=captured+timedelta(days=90)).on_conflict_do_nothing())
        c.execute(m.session_facts.update().where(m.session_facts.c.session_id==session_id).values(payload=facts|{'anonymous_final_persisted':True}))

    @staticmethod
    def persist_round_trace(c,sid,rid,trace_ref,started):
        identities=sa.union(sa.select(s.guesses.c.id).where(s.guesses.c.session_id==sid,s.guesses.c.round_id==rid),sa.select(s.rejections.c.id).where(s.rejections.c.session_id==sid,s.rejections.c.round_id==rid))
        rows=c.execute(sa.select(s.trace_steps.c.stage,s.trace_steps.c.state,s.trace_steps.c.time,s.trace_steps.c.reason_code).where(s.trace_steps.c.message_id.in_(identities)).order_by(s.trace_steps.c.time,s.trace_steps.c.id)).mappings()
        steps=[{'stage':r['stage'],'state':r['state'],'time':iso(r['time']),'reason_code':r['reason_code']} for r in rows]
        # This separate 30-day store contains no guess IDs, comments, identities or answers.
        c.execute(insert(m.traces).values(round_id=rid,session_id=sid,trace_ref=trace_ref,expires_at=started+timedelta(days=30),payload={'steps':steps}).on_conflict_do_nothing())

    @staticmethod
    def prune_expired_traces(c,session_id,captured):
        """Explicit session-scoped retention hook; never deletes aggregate/tombstone metadata."""
        c.execute(sa.text("""UPDATE analytics_round_facts f SET payload=f.payload||jsonb_build_object('trace_expires_at',t.expires_at)
          FROM analytics_round_traces t WHERE t.round_id=f.round_id AND t.session_id=:sid AND t.expires_at<=:at"""),{'sid':session_id,'at':captured})
        return c.execute(m.traces.delete().where(m.traces.c.session_id==session_id,m.traces.c.expires_at<=captured)).rowcount

    def facts_payload(self,c,session,captured):
        sid=session['id']
        final=c.execute(sa.select(m.finals).where(m.finals.c.session_id==sid)).mappings().first()
        if final:
            if final['expires_at']<=captured: raise self.unavailable('EXPIRED')
            payload=self.load_final(c,final)
            result=self.refresh_final(payload,session,captured,self.delivery_projection(c,session))
            observed=c.execute(sa.select(m.delivery.c.payload).where(m.delivery.c.session_id==sid)).scalar_one_or_none() or {}
            result['closure']['remote_fulfillment_pending']=observed.get('remote_fulfillment_pending',payload['closure'].get('remote_fulfillment_pending'))
            return result
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==sid)).scalar_one_or_none() or {}
        if facts.get('materialization_pending_reason'):
            raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Final materialization requires recovery',413,details={'reason':facts['materialization_pending_reason']})
        if facts.get('sealed_at'):
            # GET must never silently reaggregate after retention or repair a missing final.
            reason='EXPIRED' if stamp(facts['sealed_at'])+timedelta(days=90)<=captured else 'FINAL_AGGREGATE_MISSING'
            raise self.unavailable(reason)
        result=self.business_payload(c,session,captured)
        result['closure']['local_delivery_pending']=self.delivery_projection(c,session)
        return result

    def business_payload(self,c,session,captured):
        sid=session['id']
        rounds=[dict(x) for x in c.execute(sa.select(s.rounds.c.id,s.rounds.c.status,s.rounds.c.end_reason,s.rounds.c.puzzle_version_id,s.rounds.c.frozen_puzzle['category_id'].astext.label('category'),s.rounds.c.frozen_puzzle['difficulty'].as_integer().label('difficulty')).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at,s.rounds.c.id).limit(MAX_ROWS+1)).mappings()]
        for r in rounds: r['frozen_puzzle']={'category_id':r.pop('category'),'difficulty':r.pop('difficulty')}
        if len(rounds)>MAX_ROWS: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Snapshot row budget exceeded',413,details={'reason':'ROW_LIMIT'})
        facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==sid)).scalar_one_or_none() or {}
        facts=dict(facts)
        observed=c.execute(sa.select(m.delivery.c.payload).where(m.delivery.c.session_id==sid)).scalar_one_or_none() or {}
        facts.update({k:v for k,v in observed.items() if k in {'relay_coverage','relay_confirmed_stream_seq','remote_fulfillment_pending'}})
        facts['score_jobs_nonterminal']=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.session_id==sid,s.score_jobs.c.status.notin_(['succeeded','failed','cancelled']))).scalar_one()
        # Delivery/ACK diagnostics are queried separately; freezing the local business
        # basis must not depend on an unavailable remote/official projection.
        facts['local_delivery_pending']=None
        fact_size=c.execute(sa.text('SELECT coalesce(sum(octet_length(payload::text)),0) FROM analytics_round_facts WHERE session_id=:sid'),{'sid':sid}).scalar_one()
        if fact_size>MAX_BYTES: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Round fact byte budget exceeded',413,details={'reason':'FACT_BYTE_LIMIT'})
        rf={x['round_id']:x['payload'] for x in c.execute(sa.select(m.round_facts).where(m.round_facts.c.session_id==sid)).mappings()}
        # Stable keyed, opaque trace refs for pre-observer history; no UID/answer is exposed.
        for r in rounds:
            rf.setdefault(r['id'],{})
            rf[r['id']].setdefault('trace_ref',hmac.new(self.signer.key,('trace:'+sid+':'+r['id']).encode(),'sha256').hexdigest())
        result=calculate(session,rounds,[],[],[],[],facts,rf,captured)
        self.aggregate_sql(c,session,result)
        return result

    @storage_guard
    def snapshot(self,sid,actor,*,fresh=False):
        with self.locked() as c:
            c.execution_options(isolation_level='REPEATABLE READ')
            with c.begin():
                captured=c.execute(sa.text('SELECT transaction_timestamp()')).scalar_one()
                session=self.authorize(c,sid,actor)
                self.delivery_projection(c,session)
                self.expire(c,captured)
                basis=c.execute(sa.select(m.finals.c.expires_at,m.finals.c.payload['metric_definition_version'].astext.label('metric')).where(m.finals.c.session_id==sid)).mappings().first()
                if basis and basis['expires_at']<=captured: raise self.unavailable('EXPIRED')
                metric=basis['metric'] if basis else VERSION
                old=c.execute(sa.select(m.snapshots).where(m.snapshots.c.session_id==sid,m.snapshots.c.actor_id==actor,m.snapshots.c.payload.is_not(None),m.snapshots.c.payload['metric_definition_version'].astext==metric).order_by(m.snapshots.c.captured_at.desc()).limit(1)).mappings().first()
                if old and old['watermark']==session['analytics_commit_counter']:
                    return self.access(c,old,captured)
                if old and (captured-old['captured_at']).total_seconds()<5:
                    if fresh: raise DomainError('ANALYTICS_RATE_LIMITED','New snapshots require a 5 second interval',429,True,details={'reason':'FRESH_INTERVAL'})
                    return self.access(c,old,captured)
                # Tombstones still impose the creation interval if capacity evicted the latest copy.
                latest=c.execute(sa.select(sa.func.max(m.snapshots.c.captured_at)).where(m.snapshots.c.session_id==sid,m.snapshots.c.actor_id==actor)).scalar_one()
                if latest and (captured-latest).total_seconds()<5: raise DomainError('ANALYTICS_RATE_LIMITED','Snapshot creation interval exceeded',429,True,details={'reason':'FRESH_INTERVAL'})
                payload=self.facts_payload(c,session,captured)
                if len(payload['_minutes'])+len(payload['_rounds'])>MAX_ROWS: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Snapshot row budget exceeded',413,details={'reason':'ROW_LIMIT'})
                identity=secrets.token_urlsafe(24); expires=min(captured+timedelta(hours=24),basis['expires_at']) if basis else captured+timedelta(hours=24)
                payload=payload|dict(snapshot_id=identity,expires_at=iso(expires))
                size=len(json_bytes(payload))
                if size>MAX_BYTES: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Snapshot byte budget exceeded',413,details={'reason':'BYTE_LIMIT'})
                self.reserve(c,actor,sid,size,captured)
                c.execute(m.snapshots.insert().values(id=identity,session_id=sid,actor_id=actor,watermark=session['analytics_commit_counter'],captured_at=captured,expires_at=expires,last_access=captured,payload=payload,size_bytes=size))
                return payload

    @staticmethod
    def access(c,row,now):
        c.execute(m.snapshots.update().where(m.snapshots.c.id==row['id']).values(last_access=now))
        return row['payload']

    @staticmethod
    def expire(c,now):
        c.execute(m.snapshots.update().where(m.snapshots.c.expires_at<=now,m.snapshots.c.payload.is_not(None),sa.or_(m.snapshots.c.pin_until.is_(None),m.snapshots.c.pin_until<=now)).values(payload=None,size_bytes=0,reason='EXPIRED',pin_count=0))


    @staticmethod
    def reserve(c,actor,sid,size,now):
        while True:
            rows=list(c.execute(sa.select(m.snapshots.c.id,m.snapshots.c.actor_id,m.snapshots.c.session_id,m.snapshots.c.last_access,m.snapshots.c.size_bytes,m.snapshots.c.pin_until,m.snapshots.c.pin_count).where(m.snapshots.c.payload.is_not(None)).order_by(m.snapshots.c.last_access)).mappings())
            scoped=[r for r in rows if r['actor_id']==actor and r['session_id']==sid]
            scope_full=len(scoped)>=16
            if not scope_full and len(rows)<256 and sum(r['size_bytes'] for r in rows)+size<=MAX_TOTAL_BYTES: return
            candidates=[r for r in (scoped if scope_full else rows) if not r['pin_until'] or r['pin_until']<=now or not r['pin_count']]
            if not candidates: raise DomainError('ANALYTICS_CAPACITY_BUSY','Snapshot capacity is pinned',503,True,details={'reason':'PINNED_CAPACITY'})
            c.execute(m.snapshots.update().where(m.snapshots.c.id==candidates[0]['id']).values(payload=None,size_bytes=0,reason='EVICTED',pin_count=0))

    @contextmanager
    @storage_guard
    def pinned(self,sid,actor,identity):
        with self.db.engine.connect() as authorization:
            session=self.authorize(authorization,sid,actor)
            self.delivery_projection(authorization,session)
        if not isinstance(identity,str) or len(identity)>128 or not identity or any(x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for x in identity):
            raise DomainError('INPUT_INVALID','Invalid snapshot id',422)
        with self.locked() as c:
            with c.begin():
                self.authorize(c,sid,actor); now=moment()
                row=c.execute(sa.select(m.snapshots).where(m.snapshots.c.id==identity,m.snapshots.c.session_id==sid,m.snapshots.c.actor_id==actor)).mappings().first()
                if not row: raise self.unavailable()
                if row['expires_at']<=now: raise self.unavailable('EXPIRED')
                if row['payload'] is None: raise self.unavailable(row['reason'] or 'NOT_AVAILABLE')
                first=row['pin_first'] if row['pin_until'] and row['pin_until']>now else now
                until=min(first+timedelta(minutes=5),row['expires_at'])
                count=row['pin_count'] if row['pin_until'] and row['pin_until']>now else 0
                c.execute(m.snapshots.update().where(m.snapshots.c.id==identity).values(pin_first=first,pin_until=until,pin_count=count+1,last_access=now))
                payload=row['payload']
        try: yield payload
        finally:
            with self.locked() as c:
                with c.begin(): c.execute(m.snapshots.update().where(m.snapshots.c.id==identity,m.snapshots.c.pin_first==first).values(pin_count=sa.func.greatest(m.snapshots.c.pin_count-1,0)))

    def page(self,payload,sid,section,limit,cursor=None):
        if section not in {'rounds','minutes'} or isinstance(limit,bool) or not 1<=limit<=100: raise DomainError('INPUT_INVALID','Invalid analytics page',422)
        rows=payload['_'+section]; key=(lambda x:[x['round_ordinal'],x['round_id']]) if section=='rounds' else (lambda x:[x['bucket_start']])
        last=self.signer.decode(cursor,payload['snapshot_id'],sid,section,limit) if cursor else None
        if last is not None:
            if not isinstance(last,list) or not any(key(r)==last for r in rows): raise DomainError('ANALYTICS_CURSOR_INVALID','Invalid last key',422)
            remaining=[r for r in rows if key(r)>last]
        else: remaining=rows
        items=remaining[:limit]; next_cursor=self.signer.encode(payload['snapshot_id'],sid,section,limit,key(items[-1])) if len(remaining)>limit else None
        return dict(be_extension_version=EXTENSION,snapshot_id=payload['snapshot_id'],items=items,next_cursor=next_cursor,total=len(rows))

    def aggregate(self,payload,sid,limit=50):
        result={k:v for k,v in payload.items() if not k.startswith('_')}
        minute=self.page(payload,sid,'minutes',limit); round_page=self.page(payload,sid,'rounds',limit)
        result['minute_participation']=dict(bucket_seconds=60,timezone='UTC',items=minute['items'],next_cursor=minute['next_cursor'])
        result['round_details']={k:round_page[k] for k in ['items','next_cursor','total']}
        return result

    @contextmanager
    @storage_guard
    def export_slot(self,actor):
        identity=secrets.token_urlsafe(24)
        with self.locked() as c:
            with c.begin():
                now=moment(); c.execute(m.exports.delete().where(m.exports.c.expires_at<=now))
                actors=list(c.execute(sa.select(m.exports.c.actor_id)).scalars())
                if actor in actors or len(actors)>=2: raise DomainError('ANALYTICS_EXPORT_LIMITED','Export concurrency limit reached',429,True,details={'reason':'CONCURRENCY_LIMIT'})
                c.execute(m.exports.insert().values(id=identity,actor_id=actor,expires_at=now+timedelta(seconds=30)))
        try: yield
        finally:
            with self.db.engine.begin() as c: c.execute(m.exports.delete().where(m.exports.c.id==identity))

    @staticmethod
    def export_directory():
        # A single API owns this per-user, private product scratch area.
        root=Path(tempfile.gettempdir())/('wordheat-analytics-'+str(os.getuid() if hasattr(os,'getuid') else 'local'))
        try: root.mkdir(mode=0o700,exist_ok=True)
        except FileExistsError:
            # exist_ok must tolerate an existing directory; a brokered or racing
            # create can still surface EEXIST. Ownership/mode are verified below.
            if not root.is_dir(): raise DomainError('ANALYTICS_CAPACITY_BUSY','Export storage unavailable',503,True,details={'reason':'STORAGE_IO'}) from None
        mode=root.lstat()
        if not stat.S_ISDIR(mode.st_mode) or mode.st_mode&0o077 or hasattr(os,'getuid') and mode.st_uid!=os.getuid():
            raise DomainError('ANALYTICS_CAPACITY_BUSY','Export storage unavailable',503,True,details={'reason':'STORAGE_PERMISSIONS'})
        return root

    @staticmethod
    def cleanup_export_files():
        root=AnalyticsService.export_directory(); cutoff=time.time()-30; removed=0
        # No recursive traversal, symlink following or removal outside this owned directory.
        for path in root.glob('wordheat-analytics-*.csv'):
            info=path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime<=cutoff and (not hasattr(os,'getuid') or info.st_uid==os.getuid()):
                path.unlink(missing_ok=True); removed+=1
        return removed

    @staticmethod
    def csv_file(payload):
        """Complete bounded preflight; errors happen before HTTP attachment headers."""
        started=time.monotonic(); fd=None; path=None
        try:
            fd,raw=tempfile.mkstemp(prefix='wordheat-analytics-',suffix='.csv',dir=AnalyticsService.export_directory())
            path=Path(raw); os.chmod(path,0o600)
            base=['record_type','metric_definition_version','snapshot_id','as_of','closure','coverage']
            rows=itertools.chain([('aggregate',{k:v for k,v in payload.items() if not k.startswith('_') and k!='source_partitions'})],(('source_partition',x) for x in payload['source_partitions']),(('minute',x) for x in payload['_minutes']),(('round',x) for x in payload['_rounds']))
            def cell(value):
                if value is None: return ''
                if isinstance(value,(dict,list)): value=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
                if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@','\t','\r')): return "'"+value
                return value
            with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
                fd=None
                writer=csv.DictWriter(stream,fieldnames=base+CSV_FIELDS,quoting=csv.QUOTE_ALL); writer.writeheader()
                for kind,row in rows:
                    if time.monotonic()-started>30: raise DomainError('ANALYTICS_EXPORT_TIMEOUT','Export exceeded 30 seconds',503,True,details={'reason':'TIME_LIMIT'})
                    value=dict(row,record_type=kind,metric_definition_version=payload['metric_definition_version'],snapshot_id=payload['snapshot_id'],as_of=payload['as_of'],closure=payload['closure'],coverage=payload['coverage'])
                    writer.writerow({k:cell(v) for k,v in value.items()}); stream.flush()
                    if stream.tell()>MAX_EXPORT_BYTES: raise DomainError('ANALYTICS_EXPORT_TOO_LARGE','Export exceeded 32 MiB',413,details={'reason':'BYTE_LIMIT'})
                if time.monotonic()-started>30: raise DomainError('ANALYTICS_EXPORT_TIMEOUT','Export exceeded 30 seconds',503,True,details={'reason':'TIME_LIMIT'})
            return path
        except BaseException as error:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
            if path: path.unlink(missing_ok=True)
            if isinstance(error,OSError):
                reason='STORAGE_QUOTA' if error.errno in {errno.ENOSPC,getattr(errno,'EDQUOT',-1)} else 'STORAGE_IO'
                raise DomainError('ANALYTICS_CAPACITY_BUSY','Export storage unavailable',503,True,details={'reason':reason}) from None
            raise

    @staticmethod
    def aggregate_sql(c,session,result,*,details=True):
        """All raw-input aggregation stays in PG. Python only receives bounded grouped rows."""
        sid=session['id']; production=session['mode']=='official' and session['run_purpose']=='production'
        params={'sid':sid,'production':production,'started':result['coverage']['started_at'],'through':result['coverage']['through_at']}
        cte=RAW_CTE
        fields="""count(DISTINCT participant_id) FILTER(WHERE successful) AS participants_count,
          count(*) FILTER(WHERE successful) AS valid_distinct_guesses,
          count(*) FILTER(WHERE NOT nonterminal) AS guess_attempts,
          count(*) FILTER(WHERE business_rejected) AS business_rejected,
          count(*) FILTER(WHERE system_failed) AS system_failed,
          count(*) FILTER(WHERE nonterminal) AS nonterminal"""
        total=dict(c.execute(sa.text(cte+'SELECT '+fields+' FROM quality'),params).mappings().one())
        result.update(participants_count=total['participants_count'],valid_distinct_guesses=total['valid_distinct_guesses'],guesses_per_participant=ratio(total['valid_distinct_guesses'],total['participants_count']),invalid_ratio=ratio(total['business_rejected'],total['guess_attempts']))
        result['denominators'].update(valid_guess_units=total['valid_distinct_guesses'],participants=total['participants_count'],guess_attempts=total['guess_attempts'],invalid_attempts=total['business_rejected'])
        exclusions=c.execute(sa.text(cte+"SELECT count(*) FILTER(WHERE is_void AND is_guess) AS void_excluded,count(*) FILTER(WHERE NOT assigned AND is_guess) AS unassigned_round FROM facts"),params).mappings().one()
        result['attempt_outcomes']=dict(successful=total['valid_distinct_guesses'],**{x:total[x] for x in ['business_rejected','system_failed','nonterminal']},**dict(exclusions))
        if details:
            # There can be no more groups than the preflighted round-detail count.
            byround={x['round_id']:dict(x) for x in c.execute(sa.text(cte+'SELECT round_id,'+fields+' FROM quality GROUP BY round_id'),params).mappings()}
            points={x['round_id']:x['points'] for x in c.execute(sa.text('SELECT l.round_id,sum(l.delta)::bigint AS points FROM points_ledger l JOIN rounds r ON r.id=l.round_id WHERE r.session_id=:sid GROUP BY l.round_id'),{'sid':sid}).mappings()}
            for row in result['_rounds']:
                row.update({k:v for k,v in byround.get(row['round_id'],{}).items() if k!='round_id'})
                row['points_net']=0 if row['exclusion_reason']=='VOID' else points.get(row['round_id'],0)
            result['points_net']=sum(r['points_net'] for r in result['_rounds'])
            if params['started']:
                query=cte+"SELECT floor(extract(epoch FROM (received_at-CAST(:started AS timestamptz)))/60)::int AS bucket,"+fields+" FROM quality WHERE received_at>=CAST(:started AS timestamptz) AND received_at<CAST(:through AS timestamptz) GROUP BY bucket ORDER BY bucket LIMIT 20001"
                buckets=list(c.execute(sa.text(query),params).mappings())
                if len(buckets)>MAX_ROWS: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Minute budget exceeded',413,details={'reason':'ROW_LIMIT'})
                for row in buckets:
                    if 0<=row['bucket']<len(result['_minutes']): result['_minutes'][row['bucket']].update({k:v for k,v in row.items() if k!='bucket'})
        else:
            result['points_net']=c.execute(sa.text("SELECT coalesce(sum(l.delta),0) FROM points_ledger l JOIN rounds r ON r.id=l.round_id WHERE r.session_id=:sid AND r.status!='VOID' AND coalesce(r.end_reason,'')!='VOID'"),{'sid':sid}).scalar_one()
        partitions=[]
        # Trusted source vocabulary has finite cardinality; reject unknown arbitrary labels.
        query=cte+"""SELECT source_mode,provider_test,count(*) AS messages,
          count(*) FILTER(WHERE is_guess AND NOT nonterminal) AS guess_attempts,
          count(*) FILTER(WHERE is_guess AND successful AND assigned AND NOT is_void) AS valid_guess_units
          FROM facts GROUP BY source_mode,provider_test ORDER BY source_mode,provider_test LIMIT 100"""
        for value in c.execute(sa.text(query),params).mappings():
            part=dict(value); source=part['source_mode']; test=part['provider_test']
            part['eligible_for_production']=production and source=='official' and test is False
            part['excluded_reason']=None if part['eligible_for_production'] else ('excluded_unknown' if source=='unknown' or (source=='official' and test is None) else ('test_live' if session['run_purpose']=='test_live' else source))
            partitions.append(part)
        if len(partitions)>=100: raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Source partition budget exceeded',413,details={'reason':'SOURCE_PARTITION_LIMIT'})
        result['source_partitions']=partitions
        result['source_mode_counts']={source:sum(p['messages'] for p in partitions if p['source_mode']==source) for source in {p['source_mode'] for p in partitions}}
        latency=cte+""", samples AS (
          SELECT ((l.payload->>'feedback_commit_mono_ns')::numeric-(l.payload->>'admission_commit_mono_ns')::numeric)/1000000 AS ms
          FROM quality q JOIN analytics_latency_observations l ON l.guess_id=q.id AND l.session_id=:sid
          WHERE q.successful AND l.payload->>'admission_boot_id'=l.payload->>'feedback_boot_id'
          AND l.payload->>'admission_boot_id' IS NOT NULL
          AND l.payload->>'admission_commit_mono_ns' IS NOT NULL AND l.payload->>'feedback_commit_mono_ns' IS NOT NULL
        ) SELECT count(*) AS samples,
          percentile_disc(0.50) WITHIN GROUP(ORDER BY ms) AS p50,
          percentile_disc(0.95) WITHIN GROUP(ORDER BY ms) AS p95,
          percentile_disc(0.99) WITHIN GROUP(ORDER BY ms) AS p99 FROM samples WHERE ms>=0"""
        observation=c.execute(sa.text(latency),params).mappings().one()
        result['denominators']['latency_samples']=observation['samples']
        result['latency_missing_samples']=total['valid_distinct_guesses']-observation['samples']
        for p in [50,95,99]: result['pipeline_latency_p'+str(p)+'_ms']=float(observation['p'+str(p)]) if observation['p'+str(p)] is not None else None

    @staticmethod
    def observe_delivery(c,session_id,observation_id,revision,*,ack_outbox,remote_fulfillment_pending=None,relay_coverage='unknown',relay_confirmed_stream_seq=None):
        """Trusted connector port; caller owns session lock and the final counter touch.

        This API is never exposed to browser input. Local task counts are exact;
        absent remote evidence is null. Revision reuse with different facts fails.
        """
        integers=[ack_outbox]+([] if remote_fulfillment_pending is None else [remote_fulfillment_pending])
        if not observation_id or isinstance(revision,bool) or not isinstance(revision,int) or revision<0 or any(isinstance(x,bool) or not isinstance(x,int) or x<0 for x in integers):
            raise ValueError('Invalid trusted delivery observation')
        if relay_coverage not in {'complete','partial','unknown'} or relay_confirmed_stream_seq is not None and (not isinstance(relay_confirmed_stream_seq,str) or not relay_confirmed_stream_seq.isdecimal()):
            raise ValueError('Invalid trusted relay coverage')
        payload=dict(ack_outbox=ack_outbox,remote_fulfillment_pending=remote_fulfillment_pending,relay_coverage=relay_coverage,relay_confirmed_stream_seq=relay_confirmed_stream_seq)
        old=c.execute(sa.select(m.delivery).where(m.delivery.c.session_id==session_id)).mappings().first()
        if old:
            if old['observation_id']!=observation_id or revision<old['revision'] or revision==old['revision'] and payload!=old['payload']:
                raise DomainError('VERSION_CONFLICT','Trusted observation revision conflict',409)
            if revision==old['revision']: return False
        c.execute(insert(m.delivery).values(session_id=session_id,observation_id=observation_id,revision=revision,payload=payload).on_conflict_do_update(index_elements=[m.delivery.c.session_id],set_={'observation_id':observation_id,'revision':revision,'payload':payload}))
        return True

    def load_final(self,c,final):
        payload=dict(final['payload'])
        if not payload.get('_chunked'): return payload
        if payload['_row_count']>MAX_ROWS or payload['_row_bytes']+len(json_bytes(payload))>MAX_BYTES:
            raise DomainError('ANALYTICS_SNAPSHOT_TOO_LARGE','Snapshot budget exceeded',413,details={'reason':'FINAL_QUERY_BUDGET'})
        payload['_minutes']=[]; payload['_rounds']=[]
        for row in c.execute(sa.select(m.final_rows.c.section,m.final_rows.c.payload).where(m.final_rows.c.session_id==final['session_id']).order_by(m.final_rows.c.section,m.final_rows.c.row_key)).mappings():
            payload['_'+row['section']].append(row['payload'])
        return {k:v for k,v in payload.items() if k not in {'_chunked','_row_count','_row_bytes'}}

    def maintenance_tick(self,now=None,batch_size=100):
        """Scheduler entry. Each session commits independently; crash retry is idempotent."""
        from app.modules.analytics.maintenance import recover_final,prune_session
        if isinstance(batch_size,bool) or not 1<=batch_size<=100: raise ValueError('Maintenance batch must be 1..100')
        captured=now or moment(); processed=0
        self.cleanup_export_files()
        with self.db.engine.connect() as c:
            candidates=list(c.execute(sa.select(s.sessions.c.id).outerjoin(m.maintenance,m.maintenance.c.session_id==s.sessions.c.id).where(sa.or_(m.maintenance.c.next_run.is_(None),m.maintenance.c.next_run<=captured)).order_by(m.maintenance.c.next_run.asc().nullsfirst(),s.sessions.c.id).limit(batch_size)).scalars())
        for sid in candidates:
            with self.db.engine.begin() as c:
                session=c.execute(sa.select(s.sessions).where(s.sessions.c.id==sid).with_for_update(skip_locked=True)).mappings().first()
                if not session: continue
                facts=c.execute(sa.select(m.session_facts.c.payload).where(m.session_facts.c.session_id==sid)).scalar_one_or_none() or {}
                if facts.get('materialization_pending_reason'):
                    recover_final(self,c,dict(session),facts)
                prune_session(self,c,sid,captured)
                c.execute(insert(m.maintenance).values(session_id=sid,next_run=captured+timedelta(seconds=60)).on_conflict_do_update(index_elements=[m.maintenance.c.session_id],set_={'next_run':captured+timedelta(seconds=60)}))
                processed+=1
        with self.locked() as c:
            with c.begin():
                # Query-copy tombstones remain to return stable 410 reasons.
                c.execute(m.snapshots.update().where(m.snapshots.c.expires_at<=captured,m.snapshots.c.payload.is_not(None),sa.or_(m.snapshots.c.pin_until.is_(None),m.snapshots.c.pin_until<=captured)).values(payload=None,size_bytes=0,reason='EXPIRED',pin_count=0))
                c.execute(m.exports.delete().where(m.exports.c.expires_at<=captured))
        return processed
