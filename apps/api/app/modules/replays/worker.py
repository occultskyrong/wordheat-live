"""Bounded virtual-time scheduler with durable per-input admission checkpoints."""
import threading
from datetime import timedelta
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid,now
from app.modules.replays.admission import ReplayAdmission

class ReplayWorker:
    def __init__(self,replay):
        self.replay=replay; self.core=replay.core; self.db=replay.db
        self.stop_event=threading.Event(); self.thread=None; self.last_error=None

    def start(self):
        self.recover_boot()
        self.thread=threading.Thread(target=self.run,name='wordheat-local-replay',daemon=True); self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=3)

    def run(self):
        while not self.stop_event.wait(.1):
            try: self.tick(); self.last_error=None
            except Exception: self.last_error='REPLAY_WORKER_FAILED'

    def latest(self,c,sid):
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at.desc()).limit(1).with_for_update()).mappings().first()
        return dict(row) if row else None

    def recover_boot(self):
        with self.db.engine.connect() as c:
            jobs=list(c.execute(sa.select(s.replay_jobs.c.id,s.replay_jobs.c.actor_id).where(s.replay_jobs.c.status=='running',s.replay_jobs.c.boot_id!=self.core.boot_id)).mappings())
        for item in jobs:
            with self.db.engine.begin() as c:
                session,row=self.replay.locked(c,item['actor_id'],item['id']); active=self.latest(c,session['id'])
                if not self.replay.valid_checkpoint(row,active):
                    self.fail(c,session,row,'LOCAL_TIME_RECOVERY_UNSAFE'); continue
                # Admitted work is already durable. Do not re-admit or reuse an
                # old monotonic deadline; unfinished score jobs become failed.
                if active:
                    from app.modules.rounds.worker import LocalRoundWorker
                    LocalRoundWorker(self.core).cancel_jobs(c,active,'RESULT_STALE')
                    if active['status'] in {'RUNNING','DRAINING','PAUSED'}:
                        c.execute(s.rounds.update().where(s.rounds.c.id==active['id']).values(status='PAUSED',boot_id=self.core.boot_id,mono_started_ns=0,version=s.rounds.c.version+1))
                        active=active|{'status':'PAUSED','boot_id':self.core.boot_id,'mono_started_ns':0}
                self.replay.checkpoint(c,row,active,state='PAUSED',boot_id=self.core.boot_id,version=row['version']+1,claim_token=None,lease_until=None)
                c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1))

    def fail(self,c,session,row,code):
        active=self.latest(c,session['id'])
        if active and active['status'] in {'RUNNING','PAUSED','DRAINING'}:
            from app.modules.rounds.worker import LocalRoundWorker
            LocalRoundWorker(self.core).cancel_jobs(c,active,code)
            if not c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==active['id'])).first():self.core.settle(c,session,active,'INTERRUPTED',None,None)
        c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==row['id']).values(status='failed',state='FAILED',error_code=code,claim_token=None,lease_until=None,version=row['version']+1))
        c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1))
        self.core.touch(c,session['id'])

    def fault_event(self,c,row,roundrow,event,code):
        # System failures are not consumed replay records. Preserve the committed
        # cursor/ledger prefix and retain this admitted event for diagnosis.
        code=code or 'REPLAY_SCORING_FAILED'
        c.execute(s.replay_events.update().where(s.replay_events.c.id==event['id']).values(status='failed',reason_code=code))
        if event.get('feedback_id'):self.core.trace(c,event['feedback_id'],'replay','failed',code)
        session=self.core.session(c,row['session_id'],row['actor_id'])
        self.fail(c,session,row,code)

    def finish_event(self,c,row,roundrow,event,result):
        status=result['status']
        if status=='queued':return False
        if status not in {'scored','rejected','format_invalid','duplicate','rate_limited','round_closed'}:
            self.fault_event(c,row,roundrow,event,result.get('message') or 'REPLAY_SCORING_FAILED');return False
        applied=result['status']=='scored'; rejected=not applied
        c.execute(s.replay_events.update().where(s.replay_events.c.id==event['id']).values(status='applied' if applied else 'rejected',reason_code=result.get('message') or None))
        if roundrow and roundrow['status'] in {'REVEALED','INTERMISSION'} and row['checkpoint'].get('round_ended_ms') is None:
            row['checkpoint']=row['checkpoint']|{'round_ended_ms':event['offset_ms']}
        self.replay.checkpoint(c,row,roundrow,cursor=row['cursor']+1,virtual_ms=event['offset_ms'],events_applied=row['events_applied']+int(applied),events_rejected=row['events_rejected']+int(rejected),version=row['version']+1,claim_token=None,lease_until=None)

    def reject(self,c,session,row,roundrow,source,code):
        identity=uid(); source_id=f"{row['id']}:{source['source_id']}"
        c.execute(s.rejections.insert().values(id=identity,session_id=session['id'],round_id=roundrow['id'] if roundrow else None,reason_code=code,source_id=source_id,source_mode='replay',provider_test=None))
        self.core.trace(c,identity,'admission','rejected',code)
        self.core.observe(c,'guess_rejected',session['id'],{'observation_id':identity,'round_id':roundrow['id'] if roundrow else None,'participant_id':None,'source_mode':'replay','provider_test':None,'received_at':now().isoformat(),'outcome':'business_rejected','is_guess_attempt':True})
        event={'id':uid(),'job_id':row['id'],'ordinal':row['cursor'],'source_id':source['source_id'],'offset_ms':source['offset_ms'],'status':'rejected','feedback_id':identity,'reason_code':code}
        c.execute(s.replay_events.insert().values(**event))
        self.finish_event(c,row,roundrow,event,{'status':'rejected','message':code})
        self.core.touch(c,session['id'])

    def tick(self):
        with self.db.engine.connect() as c:
            candidate=c.execute(sa.select(s.replay_jobs.c.id,s.replay_jobs.c.actor_id).where(s.replay_jobs.c.status=='running',s.replay_jobs.c.state=='RUNNING',s.replay_jobs.c.boot_id==self.core.boot_id,sa.or_(s.replay_jobs.c.lease_until.is_(None),s.replay_jobs.c.lease_until<now())).order_by(s.replay_jobs.c.created_at).limit(1)).mappings().first()
        if not candidate: return False
        body=None; result=None; token=uid(); event=None
        with self.db.engine.begin() as c:
            session,row=self.replay.locked(c,candidate['actor_id'],candidate['id'])
            if row['status']!='running' or row['state']!='RUNNING' or (row['lease_until'] and row['lease_until']>=now()): return False
            try: self.replay.wordbank.authorize(c,row['actor_id'],'replay.control')
            except DomainError:
                self.fail(c,session,row,'FORBIDDEN'); return True
            roundrow=self.latest(c,session['id'])
            try:
                if self.core.runtime_guard:self.core.runtime_guard(c,action='next_round',session_id=session['id'])
                observer=getattr(self.core.scorer,'readiness',None)
                if callable(observer) and observer().get('ready') is not True:raise DomainError('SCORER_NOT_READY','Replay model health unavailable',503)
                primary=c.execute(sa.select(s.displays.c.id).where(s.displays.c.session_id==session['id'],s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False),s.displays.c.expires_at>now())).scalar_one_or_none()
                if not primary or not self.core.readiness.display_probe or not self.core.readiness.display_probe(session_id=session['id'],display_instance_id=primary):raise DomainError('READINESS_NOT_MET','Replay primary display unavailable',409)
            except Exception as error:
                self.fail(c,session,row,error.code if isinstance(error,DomainError) else 'DEPENDENCY_UNAVAILABLE');return True
            if not self.replay.valid_checkpoint(row,roundrow): self.fail(c,session,row,'LOCAL_TIME_RECOVERY_UNSAFE'); return True
            if row['cursor']>=len(row['input']['events']):
                virtual=row['virtual_ms']
                if roundrow and roundrow['status'] in {'RUNNING','PAUSED','DRAINING'}:
                    virtual=row['checkpoint']['round_started_ms']+roundrow['frozen_rules']['duration_seconds']*1000
                    self.core.settle(c,session,roundrow,'TIMEOUT',None,None); roundrow=self.latest(c,session['id'])
                    row['checkpoint']=row['checkpoint']|{'round_ended_ms':virtual}
                self.replay.checkpoint(c,row,roundrow,status='succeeded',state='COMPLETED',virtual_ms=virtual,version=row['version']+1,claim_token=None,lease_until=None)
                c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ENDED',version=s.sessions.c.version+1))
                self.core.observe(c,'session_ended',session['id'],{'ended_at':now().isoformat(),'boot_id':self.core.boot_id,'mono_ns':__import__('time').monotonic_ns()}); self.core.touch(c,session['id']); return True
            source=row['input']['events'][row['cursor']]
            pending=c.execute(sa.select(s.replay_events).where(s.replay_events.c.job_id==row['id'],s.replay_events.c.ordinal==row['cursor'])).mappings().first()
            if pending:
                guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==pending['feedback_id'])).mappings().first()
                job=c.execute(sa.select(s.score_jobs).where(s.score_jobs.c.guess_id==guess['id'])).mappings().first() if guess else None
                if guess and guess['status']=='queued' and job:
                    waiting_valid=(job['status']=='waiting' and job['cached_score'] is not None and job['score_completed_ns'] is not None and job['claim_boot']==self.core.boot_id and job['score_completed_ns']-guess['admitted_mono_ns']<=2000000000)
                    if waiting_valid or (job['status'] in {'queued','running'} and job['deadline_at']>now()):return False
                    from app.modules.rounds.worker import LocalRoundWorker
                    LocalRoundWorker(self.core).cancel_jobs(c,roundrow,job['error_code'] or 'SCORER_TIMEOUT')
                outcome='scored' if guess and guess['status']=='scored' and (not job or job['status']=='succeeded') else 'system_failed'
                reason=(job['error_code'] if job else None) or pending.get('reason_code') or ('SCORER_TIMEOUT' if guess and guess['status']=='queued' else 'RESULT_STALE')
                self.finish_event(c,row,roundrow,pending,{'status':outcome,'message':None if outcome=='scored' else reason}); return True
            if not roundrow: self.fail(c,session,row,'LOCAL_TIME_RECOVERY_UNSAFE'); return True
            started=row['checkpoint']['round_started_ms']; deadline=started+roundrow['frozen_rules']['duration_seconds']*1000
            if roundrow['status']=='RUNNING' and source['offset_ms']>=deadline:
                self.core.settle(c,session,roundrow,'TIMEOUT',None,None); roundrow=self.latest(c,session['id'])
                row['checkpoint']=row['checkpoint']|{'round_ended_ms':deadline}
                self.replay.checkpoint(c,row,roundrow,virtual_ms=deadline,version=row['version']+1); return True
            if roundrow['status'] in {'REVEALED','INTERMISSION'}:
                ended=row['checkpoint']['round_ended_ms']
                due=(row['virtual_ms'] if ended is None else ended)+roundrow['frozen_rules']['intermission_seconds']*1000
                if not roundrow['frozen_rules']['auto_next'] or source['offset_ms']<due:
                    self.reject(c,session,row,roundrow,source,'ROUND_CLOSED'); return True
                try:
                    self.replay.ready(c,session,row['actor_id'])
                    with c.begin_nested(): opened,extra=self.replay.open_round(c,session,row,due)
                except DomainError as error:
                    self.fail(c,session,row,error.code); return True
                row['checkpoint']=extra
                self.replay.checkpoint(c,row,opened,virtual_ms=due,version=row['version']+1); return True
            if roundrow['status']!='RUNNING': self.reject(c,session,row,roundrow,source,'ROUND_CLOSED'); return True
            remaining=deadline-source['offset_ms']
            c.execute(s.rounds.update().where(s.rounds.c.id==roundrow['id']).values(remaining_ms=remaining))
            roundrow=roundrow|{'remaining_ms':remaining}
            body={'session_id':session['id'],'round_id':roundrow['id'],'short_code':roundrow['short_code'],'word':source['guess'],'display_name':source.get('display_name','Replay participant')}
            try:
                with c.begin_nested(): result=self.core.guess(c,row['actor_id'],body,f"{row['id']}:{source['source_id']}",ReplayAdmission(row['id'],row['cursor'],source['offset_ms']))
            except DomainError as error:
                if error.code in {'INPUT_INVALID','DUPLICATE_GUESS','RATE_LIMITED','ROUND_CLOSED'}:self.reject(c,session,row,roundrow,source,error.code)
                else:self.fail(c,session,row,error.code)
                return True
            event={'id':uid(),'job_id':row['id'],'ordinal':row['cursor'],'source_id':source['source_id'],'offset_ms':source['offset_ms'],'status':'processing','feedback_id':result['feedback_id']}
            c.execute(s.replay_events.insert().values(**event))
            roundrow=self.latest(c,session['id'])
            self.replay.checkpoint(c,row,roundrow,virtual_ms=source['offset_ms'],claim_token=token,lease_until=now()+timedelta(seconds=5))
        # No replay/session/round lock remains held while scoring.
        try: result=self.core.score_admitted(row['actor_id'],f"{row['id']}:{source['source_id']}",body,result)
        except Exception as error: result={'status':'system_failed','message':getattr(error,'code','REPLAY_SCORING_FAILED')}
        with self.db.engine.begin() as c:
            session,current=self.replay.locked(c,row['actor_id'],row['id'])
            if current['claim_token']!=token or current['status']!='running': return True
            roundrow=self.latest(c,session['id'])
            if result['status']=='queued':
                # Another score owner still holds the claim. Let its persisted
                # terminal result decide this record, never turn queued into rejection.
                c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==current['id']).values(claim_token=None,lease_until=None))
                return True
            self.finish_event(c,current,roundrow,event,result)
        return True
