"""Single-process local deadline/drain worker. No wall clock adjudication."""
import threading
import time
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.wordbank import selection
from app.errors import DomainError

class LocalRoundWorker:
    def __init__(self,service):
        self.service=service; self.stop_event=threading.Event(); self.thread=None
        self.last_error=None
        self.next_schedule_check=0
        self.dependency_failed=False

    def start(self):
        self.recover_boot()
        self.thread=threading.Thread(target=self.run,name='wordheat-local-rounds',daemon=True); self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=3)

    def run(self):
        while not self.stop_event.wait(.05):
            try: self.tick(); self.last_error=None
            except Exception:
                self.last_error='LOCAL_WORKER_FAILED'; self.dependency_failed=True

    def recover_boot(self):
        with self.service.db.engine.connect() as c:
            ids=list(c.execute(sa.select(s.rounds.c.id).join(s.sessions,s.sessions.c.id==s.rounds.c.session_id).where(s.sessions.c.mode=='rehearsal',s.rounds.c.status.in_(['RUNNING','PAUSED','DRAINING']),s.rounds.c.boot_id!=self.service.boot_id)).scalars())
        for rid in ids:
            with self.service.db.engine.begin() as c:
                sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==rid)).scalar_one()
                session=self.service.session(c,sid)
                row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid).with_for_update()).mappings().one()
                if row['status'] not in {'RUNNING','PAUSED','DRAINING'} or row['boot_id']==self.service.boot_id: continue
                self.cancel_jobs(c,row,'RESULT_STALE')
                if row['status']!='PAUSED' and not c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==rid)).first(): self.service.settle(c,session,row,'INTERRUPTED',None,None)
                c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1))
                self.service.touch(c,sid)
        self.schedule_next()

    def schedule_next(self):
        moment=time.monotonic_ns()
        if moment<self.next_schedule_check: return
        self.next_schedule_check=moment+1000000000
        with self.service.db.engine.connect() as c:
            ids=list(c.execute(sa.select(s.rounds.c.id).join(s.sessions,s.sessions.c.id==s.rounds.c.session_id).where(s.sessions.c.mode.in_(['rehearsal','official']),s.rounds.c.status.in_(['REVEALED','INTERMISSION']),s.rounds.c.next_due_ns.is_not(None),s.rounds.c.next_due_ns<=moment)).scalars())
        for rid in ids[:32]:
            with self.service.db.engine.begin() as c:
                selection.catalog_lock(c)
                sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==rid)).scalar_one()
                session=self.service.session(c,sid)
                row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid).with_for_update()).mappings().one()
                if row['status'] not in {'REVEALED','INTERMISSION'} or session['status']!='ACTIVE' : continue
                if c.execute(sa.select(s.rounds.c.id).join(s.sessions,s.sessions.c.id==s.rounds.c.session_id).where(s.sessions.c.mode.in_(['rehearsal','official']),s.rounds.c.session_id==sid,s.rounds.c.created_at>row['created_at'])).first(): continue
                if row['boot_id']!=self.service.boot_id:
                    c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1))
                    c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(auto_reason='LOCAL_TIME_RECOVERY_UNSAFE',next_due_ns=None)); continue
                operator=c.execute(sa.select(s.operators).where(s.operators.c.id==session['actor_id'])).mappings().one()
                ready=self.service.readiness.evaluate(c,session,operator['permissions'] if not operator['disabled'] else [])
                pointer=selection.pointer(c,sid)
                old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
                catalog=c.execute(sa.select(s.catalog.c.revision)).scalar_one()
                if old and old['status']=='EMPTY' and (old['catalog_revision']!=catalog or row['auto_attempt_key'] is None):
                    self.service.reselect(c,session,'ROUND_FINISHED',f'{rid}:catalog:{catalog}',content={'round_id':rid,'catalog_revision':catalog})
                    pointer=selection.pointer(c,sid); old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().one()
                runtime_allowed=True; runtime_reason=None
                if self.service.runtime_guard:
                    try:self.service.runtime_guard(c,action='next_round',session_id=sid)
                    except DomainError as error:runtime_allowed=False;runtime_reason=error.code
                can_next=ready['can_rehearse'] if session['mode']=='rehearsal' else ready['can_go_live'] if session['run_purpose']=='production' else ready['can_test_live']
                signature=str((pointer['revision'],can_next,runtime_allowed,operator['disabled'],'session.control' in operator['permissions']))
                if row['auto_attempt_key']==signature: continue
                reason=None if can_next and not operator['disabled'] and 'session.control' in operator['permissions'] else 'READINESS_NOT_MET'
                if not old or old['status']!='READY': reason='NO_ELIGIBLE_PUZZLE'
                if not runtime_allowed:reason=runtime_reason
                c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(status='INTERMISSION',auto_attempt_key=signature,auto_reason=reason,**({'next_start_at':None} if reason else {})))
                if not row['frozen_rules'].get('auto_next'):
                    c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(next_due_ns=None)); continue
                if reason: continue
                session=self.service.session(c,sid)
                try:
                    with c.begin_nested():
                        self.service.create_round(c,session['actor_id'],{'session_id':sid,'expected_session_version':session['version'],'selection_id':pointer['selection_id'],'expected_selection_version':pointer['revision']})
                    c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(next_due_ns=None))
                except DomainError as error:
                    c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(auto_reason=error.code))

    def cancel_jobs(self,c,row,reason):
        jobs=list(c.execute(sa.select(s.score_jobs.c.id,s.score_jobs.c.guess_id).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.round_id==row['id'],s.score_jobs.c.status.in_(['queued','running','waiting']))).mappings())
        for job in jobs:
            previous=c.execute(sa.select(s.guesses,s.participants.c.display_name).join(s.participants,s.participants.c.id==s.guesses.c.participant_id).where(s.guesses.c.id==job['guess_id'])).mappings().one()
            c.execute(s.score_jobs.update().where(s.score_jobs.c.id==job['id']).values(status='failed',error_code=reason,lease_until=None,claim_token=None))
            c.execute(s.guesses.update().where(s.guesses.c.id==job['guess_id']).values(status='system_failed',display_score=None))
            self.service.trace(c,job['guess_id'],'feedback','failed',reason)
            feedback={'feedback_id':job['guess_id'],'word':previous['normalized_word'],'display_name':previous['display_name'],'status':'system_failed','display_score':None,'solved':False,'message':reason}
            self.service.event(c,previous['session_id'],'guess.feedback',{'feedbacks':[feedback],'effects':[]})
            self.service.observe(c,'guess_terminal',previous['session_id'],{'observation_id':job['guess_id'],'guess_id':job['guess_id'],'round_id':row['id'],'participant_id':previous['participant_id'],'source_mode':previous['source_mode'],'provider_test':None,'received_at':previous['created_at'].isoformat(),'outcome':'system_failed','is_guess_attempt':True})
            if self.service.official is not None and row['frozen_puzzle'].get('time_origin')=='relay' and previous['source_mode'] in {'official','bridge'}: self.service.official.terminal_feedback(c,job['guess_id'])
            # Keep cached retries terminal after a process died during inference.
            caches=list(c.execute(sa.select(s.idempotency).where(s.idempotency.c.response['feedback_id'].astext==job['guess_id'])).mappings())
            for cached in caches:
                result=cached['response']|{'status':'system_failed','display_score':None,'message':reason}
                c.execute(s.idempotency.update().where(s.idempotency.c.id==cached['id']).values(response=result))

    def tick(self):
        self.service.synchronize_model_catalog()
        with self.service.db.engine.connect() as c:
            ids=list(c.execute(sa.select(s.rounds.c.id).join(s.sessions,s.sessions.c.id==s.rounds.c.session_id).where(sa.or_(sa.and_(s.sessions.c.mode=='rehearsal',s.rounds.c.status.in_(['RUNNING','PAUSED','DRAINING'])),sa.and_(s.sessions.c.mode=='official',s.rounds.c.status=='DRAINING',s.rounds.c.drain_until_ns.is_not(None))),s.rounds.c.boot_id==self.service.boot_id)).scalars())
        for rid in ids:
            with self.service.db.engine.begin() as c:
                sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==rid)).scalar_one()
                session=self.service.session(c,sid)
                row=dict(c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid).with_for_update()).mappings().one())
                moment=time.monotonic_ns()
                if session['mode']=='rehearsal':
                    observer=getattr(self.service.scorer,'readiness',None)
                    try: model=observer() if callable(observer) else {}
                    except Exception: model={}
                    primary=c.execute(sa.select(s.displays.c.id).where(s.displays.c.session_id==sid,s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False),s.displays.c.expires_at>__import__('app.modules.auth.service',fromlist=['now']).now())).scalar_one_or_none()
                    try: display_ok=bool(primary and self.service.readiness.probe_display(c,session_id=sid,display_instance_id=primary))
                    except Exception: display_ok=False
                    identity_mismatch=model.get('manifest_id')!=row['frozen_puzzle']['manifest_id'] or (getattr(self.service.scorer,'manifest_sha256',None) and self.service.scorer.manifest_sha256!=row['frozen_puzzle'].get('manifest_sha256'))
                    if self.dependency_failed or model.get('ready') is not True or identity_mismatch or not display_ok:
                        reason='DATABASE_DEPENDENCY_LOST' if self.dependency_failed else 'MODEL_DEPENDENCY_LOST' if model.get('ready') is not True or identity_mismatch else 'DISPLAY_DEPENDENCY_LOST'
                        self.cancel_jobs(c,row,reason)
                        if not c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==rid)).first(): self.service.settle(c,session,row,'INTERRUPTED',None,None)
                        c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1)); self.service.touch(c,sid); continue
                if session['mode']=='rehearsal' and row['status']=='RUNNING' and moment-row['mono_started_ns']>=row['remaining_ms']*1000000:
                    self.service.begin_drain(c,session,row,None,None)
                    row=dict(c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid)).mappings().one())
                if row['status']!='DRAINING': continue
                if session['mode']=='official':
                    try: self.service.official.require_current(c,session,row,result=True)
                    except DomainError as error:
                        if error.code=='CONNECTOR_RECONNECTING': continue
                        self.cancel_jobs(c,row,error.code)
                        self.service.settle(c,session,row,'INTERRUPTED',None,None)
                        c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1)); self.service.touch(c,sid); continue
                if row['drain_until_ns'] is None: continue  # Await persisted verified close barrier; connector bounds transport wait.
                pending=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.round_id==rid,s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
                if pending and moment<row['drain_until_ns']: continue
                if pending: self.cancel_jobs(c,row,'SCORER_TIMEOUT')
                self.service.settle(c,session,row,'SOLVED' if row['winner_id'] else 'TIMEOUT',row['winner_id'],row['winner_seq'])
                self.service.touch(c,sid)
        self.dependency_failed=False
        self.schedule_next()
