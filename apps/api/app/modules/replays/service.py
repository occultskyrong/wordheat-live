"""Replay control is isolated from browser manual/official admission."""
import hashlib
import json
import time
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid,now
from app.modules.sessions.service import RULES,ACTIVE_ROUNDS
from app.modules.wordbank import selection

class ReplayService:
    def __init__(self,core,wordbank): self.core=core; self.db=core.db; self.wordbank=wordbank

    @staticmethod
    def job(c,actor,jid,lock=False):
        query=sa.select(s.replay_jobs).where(s.replay_jobs.c.id==jid)
        row=c.execute(query.with_for_update() if lock else query).mappings().first()
        if not row: raise DomainError('RESOURCE_NOT_FOUND','Replay job not found',404)
        if row['actor_id']!=actor: raise DomainError('FORBIDDEN','Replay ownership required',403)
        owner=c.execute(sa.select(s.sessions.c.actor_id).where(s.sessions.c.id==row['session_id'])).scalar_one()
        if owner!=actor: raise DomainError('FORBIDDEN','Replay session ownership required',403)
        return dict(row)

    def mutate(self,actor,method,path,key,body,operation,jid=None):
        # Share the proven hash/cache machinery, keeping replay ownership before
        # the cache lookup and avoiding the session.control command permission.
        if jid:
            with self.db.engine.connect() as c:
                self.wordbank.authorize(c,actor,'replay.control'); self.job(c,actor,jid)
        return self.wordbank.mutate(actor,method,path,key,body,'replay.control',operation)

    def create(self,c,actor,body):
        selection.catalog_lock(c)
        if self.core.runtime_guard: self.core.runtime_guard(c,action='create')
        sid=uid(); jid=uid(); rules=dict(RULES)
        c.execute(s.sessions.insert().values(id=sid,actor_id=actor,label=body['label'],mode='replay',run_purpose='local',status='CREATED',rules=rules))
        c.execute(s.selection_state.insert().values(id=uid(),session_id=sid,revision=0)); c.execute(s.configuration.insert().values(id=uid(),session_id=sid))
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        c.execute(s.replay_jobs.insert().values(id=jid,actor_id=actor,session_id=sid,status='queued',state='CREATED',input=body,input_hash=digest,boot_id=self.core.boot_id))
        self.core.touch(c,sid)
        return self.dto(c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==jid)).mappings().one())

    def locked(self,c,actor,jid):
        selection.catalog_lock(c)
        pre=self.job(c,actor,jid)
        session=self.core.session(c,pre['session_id'],actor)
        return session,self.job(c,actor,jid,True)

    def ready(self,c,session,actor):
        op=self.wordbank.authorize(c,actor,'replay.control')
        readiness=self.core.readiness.evaluate(c,session,op['permissions'])
        # Replay has its own control permission; dependency evidence remains the
        # same actual DB/model/wordbank/current-primary observations.
        blockers=[key for key in ('db','model','wordbank','display') if readiness['checks'][key]['status']!='pass']
        if blockers: raise DomainError('READINESS_NOT_MET','Replay dependencies are not ready',409,details={'blockers':blockers})

    def checkpoint(self,c,row,roundrow=None,**changes):
        state=dict(row)|changes
        checkpoint={'input_hash':state['input_hash'],'cursor':state['cursor'],'virtual_ms':state['virtual_ms'],'round_id':roundrow['id'] if roundrow else None,'epoch':roundrow['epoch'] if roundrow else None,'remaining_ms':roundrow['remaining_ms'] if roundrow else None,'round_started_ms':state['checkpoint']['round_started_ms'],'round_ended_ms':state['checkpoint'].get('round_ended_ms'),'manifest_id':roundrow['frozen_puzzle']['manifest_id'] if roundrow else None,'calibration_id':roundrow['frozen_puzzle']['calibration_id'] if roundrow else None}
        changes['checkpoint']=checkpoint
        c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==row['id']).values(**changes))
        return checkpoint

    def open_round(self,c,session,job,virtual_ms):
        pointer=selection.pointer(c,session['id'])
        if pointer['selection_id']:
            saved=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first()
        else: saved=None
        if not saved or saved['status']!='READY':
            selection.replace(c,session,'random',reason='REPLAY_NEXT_ROUND'); pointer=selection.pointer(c,session['id'])
        current=self.core.session(c,session['id'])
        result=self.core.create_round(c,job['actor_id'],{'session_id':session['id'],'expected_session_version':current['version'],'selection_id':pointer['selection_id'],'expected_selection_version':pointer['revision']},replay_job_id=job['id'])
        row=dict(c.execute(sa.select(s.rounds).where(s.rounds.c.id==result['round_id'])).mappings().one())
        return row,{'round_started_ms':virtual_ms}

    def command(self,c,actor,jid,body):
        session,row=self.locked(c,actor,jid); self.core.check_version(row,body['expected_version'])
        if body.get('args'): raise DomainError('INPUT_INVALID','Replay command args must be empty',422)
        cmd=body['command']; version=row['version']+1
        active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==session['id']).order_by(s.rounds.c.created_at.desc()).limit(1).with_for_update()).mappings().first()
        recovering=cmd=='resume' and session['status']=='RECOVERY_REQUIRED'
        if self.core.runtime_guard and cmd in {'start','resume'}: self.core.runtime_guard(c,action='recover' if recovering else cmd,session_id=session['id'])
        if cmd in {'pause','resume'} and not self.valid_checkpoint(row,active): raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE','Complete replay checkpoint required',409)
        if cmd=='start':
            if row['status']!='queued' or row['state']!='CREATED': raise DomainError('JOB_STATE_CONFLICT','Replay already started',409)
            self.ready(c,session,actor)
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ACTIVE',version=s.sessions.c.version+1))
            session=self.core.session(c,session['id']); active,extra=self.open_round(c,session,row,0)
            row['checkpoint']=extra
            self.checkpoint(c,row,active,status='running',state='RUNNING',boot_id=self.core.boot_id,version=version)
            self.core.observe(c,'session_started',session['id'],{'started_at':now().isoformat(),'boot_id':self.core.boot_id,'mono_ns':time.monotonic_ns()})
        elif cmd=='pause':
            if row['status']!='running' or row['state']!='RUNNING': raise DomainError('JOB_STATE_CONFLICT','Running replay required',409)
            if active and active['status'] in ACTIVE_ROUNDS:
                c.execute(s.rounds.update().where(s.rounds.c.id==active['id']).values(status='PAUSED',version=s.rounds.c.version+1))
                active=dict(active)|{'status':'PAUSED'}
                self.core.event(c,session['id'],'round.paused',{'round':self.core.public_round(active)})
            self.checkpoint(c,row,active,state='PAUSED',version=version,claim_token=None,lease_until=None)
        elif cmd=='resume':
            if row['status']!='running' or row['state']!='PAUSED' or not self.valid_checkpoint(row,active): raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE','Reliable paused replay checkpoint required',409)
            self.ready(c,session,actor)
            if active and active['status']=='PAUSED':
                c.execute(s.rounds.update().where(s.rounds.c.id==active['id']).values(status='RUNNING',boot_id=self.core.boot_id,mono_started_ns=time.monotonic_ns(),version=s.rounds.c.version+1))
                self.core.event(c,session['id'],'round.resumed',{'round':self.core.public_round(dict(active)|{'status':'RUNNING','boot_id':self.core.boot_id})})
            c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==jid).values(state='RUNNING',version=version,boot_id=self.core.boot_id,claim_token=None,lease_until=None))
            if recovering:
                c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ACTIVE',version=s.sessions.c.version+1))
                if self.core.runtime_recovery_completed: self.core.runtime_recovery_completed(c,session_id=session['id'])
        elif cmd=='cancel':
            if row['status'] in {'succeeded','failed','cancelled'}: raise DomainError('JOB_STATE_CONFLICT','Replay is terminal',409)
            if active and active['status'] in ACTIVE_ROUNDS:
                from app.modules.rounds.worker import LocalRoundWorker
                LocalRoundWorker(self.core).cancel_jobs(c,active,'REPLAY_CANCELLED')
                self.core.settle(c,session,active,'INTERRUPTED',None,None)
            c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==jid).values(status='cancelled',state='CANCELLED',version=version,claim_token=None,lease_until=None))
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ENDED',version=s.sessions.c.version+1))
        else: raise DomainError('INPUT_INVALID','Unsupported replay command',422)
        self.core.touch(c,session['id'])
        return {'command_id':uid(),'status':'applied','resource_version':version,'state':cmd,'error':None}

    @staticmethod
    def valid_checkpoint(row,active):
        cp=row['checkpoint']
        required={'input_hash','cursor','virtual_ms','round_id','epoch','remaining_ms','round_started_ms','round_ended_ms','manifest_id','calibration_id'}
        if not active or not isinstance(cp,dict) or not required<=cp.keys(): return False
        if any(type(cp[key]) is not int or cp[key]<0 for key in ('cursor','virtual_ms','epoch','remaining_ms','round_started_ms')): return False
        if cp['round_started_ms']>cp['virtual_ms'] or cp['cursor']>len(row['input']['events']): return False
        ended=cp['round_ended_ms']
        if ended is not None and (type(ended) is not int or not cp['round_started_ms']<=ended<=cp['virtual_ms']): return False
        if cp['remaining_ms']>active['frozen_rules']['duration_seconds']*1000: return False
        actual=hashlib.sha256(json.dumps(row['input'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        return bool(actual==row['input_hash'] and cp['input_hash']==row['input_hash'] and cp['cursor']==row['cursor'] and cp['virtual_ms']==row['virtual_ms'] and cp['round_id']==active['id'] and cp['epoch']==active['epoch'] and cp['remaining_ms']==active['remaining_ms'] and cp['manifest_id']==active['frozen_puzzle']['manifest_id'] and cp['calibration_id']==active['frozen_puzzle']['calibration_id'])

    @staticmethod
    def dto(row):
        return {'job_id':row['id'],'status':row['status'],'version':row['version'],'progress':{'done':row['cursor'],'total':len(row['input']['events'])},'result':{'replay_session_id':row['session_id'],'events_total':len(row['input']['events']),'events_applied':row['events_applied'],'events_rejected':row['events_rejected'],'replay_cursor':str(row['cursor'])},'error':{'code':row['error_code'],'message':'Replay failed','retryable':False} if row['error_code'] else None,'state':row['state']}
