"""Offline application use cases; session row is the first business lock."""
import hashlib
import itertools
import json
import math
import re
import secrets
import time
from threading import BoundedSemaphore
from datetime import timedelta
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid, now, token_hash
from app.modules.inbox.normalization import normalize_word
from app.modules.scoring.port import UnavailableScorer
from app.modules.wordbank import selection
from app.infrastructure.clock.session_clock import LocalRoundClock
from app.modules.operations.adapter import ReadinessAdapter

RULES = dict(duration_seconds=240, intermission_seconds=4, auto_next=True, category_id=None, difficulty_min=1, difficulty_max=5, strict_round_code=True)
ACTIVE_ROUNDS = ['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']
SHORT_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

class LocalService:
    def __init__(self, database, scorer=None,display_probe=None,official=None,admission_limiter=None):
        self.db = database
        self._admission_slots = BoundedSemaphore(4)
        self.official=official
        self.command_resolvers=[]
        self.runtime_guard=None
        self.runtime_recovery_completed=None
        self._bundle_token=object()
        self._rehearsal_token=object()
        from app.modules.inbox.limiter import GlobalAdmissionLimiter
        self.admission_limiter=admission_limiter or GlobalAdmissionLimiter()
        self.scorer = scorer or UnavailableScorer()
        self.boot_id = uid()
        self.analytics_observer=None
        self.analytics=None
        self.readiness=ReadinessAdapter(database,self.scorer,display_probe)

    def _command(self, actor, method, path, key, body, operation,request_id=None):
        if not key or len(key)>128: raise DomainError('INPUT_INVALID','Idempotency-Key required',422)
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with self.db.engine.begin() as c:
            self.authorize_command(c,actor,path,body)
            # Serializes only this actor/key before resource locks; it never acquires business locks elsewhere.
            lock=int.from_bytes(hashlib.sha256((actor+method+path+key).encode()).digest()[:8],'big',signed=True)
            c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
            old=c.execute(sa.select(s.idempotency).where(s.idempotency.c.actor_id==actor,s.idempotency.c.method==method,s.idempotency.c.path==path,s.idempotency.c.key==key)).mappings().first()
            if old:
                if path=='/api/v1/live-sessions' and old['response'].get('session_id'):
                    self.authorize_session(c,old['response']['session_id'],actor)
                if old['request_hash']!=digest: raise DomainError('IDEMPOTENCY_KEY_REUSED','Key already binds a different request',409)
                response=old['response']
                if response.get('command_id'):
                    current=c.execute(sa.select(s.commands.c.response).where(s.commands.c.id==response['command_id'])).scalar_one_or_none()
                    if current is not None: response=current
                return response
            result=operation(c)
            if result.get('command_id'):
                sid=body.get('session_id')
                match=re.fullmatch(r'/api/v1/live-sessions/([^/]+)/commands',path)
                if match: sid=match.group(1)
                match=re.fullmatch(r'/api/v1/rounds/([^/]+)/commands',path)
                if match: sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==match.group(1))).scalar_one()
                if sid:
                    c.execute(s.commands.insert().values(id=result['command_id'],session_id=sid,actor_id=actor,response=result,resource_id=body.get('round_id') or (path.split('/')[-2] if '/rounds/' in path else sid)))
                    self.event(c,sid,'command.updated',{'command':result},audiences=('STUDIO',))
            c.execute(s.idempotency.insert().values(id=uid(),actor_id=actor,method=method,path=path,key=key,request_hash=digest,response=result))
            self.audit(c,actor,body.get('command',method),body.get('session_id') or body.get('round_id') or result.get('round_id') or result.get('session_id') or (path.split('/')[-2] if path.endswith('/commands') else None),result.get('resource_version',0),body.get('args',{}).get('reason'),request_id=request_id,details={'path':path})
        # Only an admission that persisted a guess can own a commit latency sample.
        # A refused admission lives in guess_rejections and has no word/guess row,
        # so sampling it would break the guess_id foreign key on every retry.
        if path in {'/api/v1/rehearsal/guesses','/internal/rehearsal-input'} and result.get('feedback_id') and result.get('word') and result['status'] in {'queued','scored','system_failed'}:
            self.committed(body['session_id'],result['feedback_id'],'admission')
            if result['status']!='queued': self.committed(body['session_id'],result['feedback_id'],'feedback')
        return result

    def command(self,actor,method,path,key,body,operation,request_id=None):
        try:
            result=self._command(actor,method,path,key,body,operation,request_id=request_id)
        except DomainError as error:
            if error.code!='NO_ELIGIBLE_PUZZLE' or not (path=='/api/v1/rounds' or re.fullmatch(r'/api/v1/live-sessions/[^/]+/commands',path)): raise
            result=self.record_selection_failure(actor,method,path,key,body,error,request_id)
        if result.get('status')=='failed' and result.get('error',{}).get('code')=='NO_ELIGIBLE_PUZZLE':
            raise DomainError('NO_ELIGIBLE_PUZZLE','No eligible puzzle for this intent',409,details={'command_id':result['command_id'],'resource_version':result['resource_version']})
        return result

    def record_selection_failure(self,actor,method,path,key,body,error,request_id=None):
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with self.db.engine.begin() as c:
            self.authorize_command(c,actor,path,body)
            lock=int.from_bytes(hashlib.sha256((actor+method+path+key).encode()).digest()[:8],'big',signed=True)
            c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
            old=c.execute(sa.select(s.idempotency).where(s.idempotency.c.actor_id==actor,s.idempotency.c.method==method,s.idempotency.c.path==path,s.idempotency.c.key==key)).mappings().first()
            if old:
                if old['request_hash']!=digest: raise DomainError('IDEMPOTENCY_KEY_REUSED','Key binds different input',409)
                return old['response']
            selection.catalog_lock(c)
            sid=body.get('session_id') or path.split('/')[4]
            session=self.session(c,sid,actor)
            # This is a durable failed intent, not an automatic retry of start.
            if body.get('selection'):
                old=selection.pointer(c,sid); requested=body['selection']
                choice=selection.replace(c,session,requested['strategy'],requested.get('puzzle_id'),reason='NO_ELIGIBLE_PUZZLE')
                self.selection_notice(c,sid,old['selection_id'],choice,'NO_ELIGIBLE_PUZZLE',f'{method}:{path}:{key}')
            else: self.reselect(c,session,'NO_ELIGIBLE_PUZZLE',f'{method}:{path}:{key}',content={'request_hash':digest})
            session=self.session(c,sid,actor)
            result={'command_id':uid(),'status':'failed','resource_version':session['version'],'state':session['status'],'error':{'code':error.code,'message':'No eligible puzzle','retryable':False},'be_extension_version':'LW-BE/1.1.0'}
            c.execute(s.commands.insert().values(id=result['command_id'],session_id=sid,actor_id=actor,response=result,resource_id=sid))
            c.execute(s.idempotency.insert().values(id=uid(),actor_id=actor,method=method,path=path,key=key,request_hash=digest,response=result))
            self.event(c,sid,'command.updated',{'command':result},audiences=('STUDIO',))
            self.audit(c,actor,body.get('command','create_round'),sid,session['version'],error.code,request_id)
            self.touch(c,sid)
            return result

    @staticmethod
    def authorize_session(c,session_id,actor):
        # Authorization-only read: no CAS/status/readiness checks before cache.
        owner=c.execute(sa.select(s.sessions.c.actor_id).where(s.sessions.c.id==session_id)).scalar_one_or_none()
        if owner is None: raise DomainError('RESOURCE_NOT_FOUND','Session not found',404)
        if owner!=actor: raise DomainError('FORBIDDEN','Session ownership required',403)

    def authorize_command(self,c,actor,path,body):
        operator=c.execute(sa.select(s.operators.c.permissions,s.operators.c.disabled).where(s.operators.c.id==actor)).mappings().first()
        if not operator or operator['disabled']: raise DomainError('AUTH_REQUIRED','Active operator required',401)
        permission='display.manage' if path.startswith('/api/v1/display-tickets') else 'puzzle.review' if path.startswith('/api/v1/puzzles/') else 'session.control'
        if permission not in operator['permissions']: raise DomainError('FORBIDDEN','Permission required',403)
        scopes=set()
        if body.get('session_id'): scopes.add(body['session_id'])
        match=re.fullmatch(r'/api/v1/live-sessions/([^/]+)/commands',path)
        if match: scopes.add(match.group(1))
        round_match=re.fullmatch(r'/api/v1/rounds/([^/]+)/commands',path)
        round_id=round_match.group(1) if round_match else body.get('round_id')
        if round_id:
            sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==round_id)).scalar_one_or_none()
            if sid is None: raise DomainError('RESOURCE_NOT_FOUND','Round not found',404)
            scopes.add(sid)
        ticket_match=re.fullmatch(r'/api/v1/display-tickets/([^/]+)',path)
        if ticket_match:
            sid=c.execute(sa.select(s.tickets.c.session_id).where(s.tickets.c.id==ticket_match.group(1))).scalar_one_or_none()
            if sid is None: raise DomainError('RESOURCE_NOT_FOUND','Ticket not found',404)
            scopes.add(sid)
        for sid in sorted(scopes): self.authorize_session(c,sid,actor)

    def session(self,c,session_id,actor=None,*,lock=True):
        query=sa.select(s.sessions).where(s.sessions.c.id==session_id)
        if lock:query=query.with_for_update()
        row=c.execute(query).mappings().first()
        if not row: raise DomainError('RESOURCE_NOT_FOUND','Session not found',404)
        if actor is not None and row['actor_id']!=actor: raise DomainError('FORBIDDEN','Session ownership required',403)
        result=dict(row)
        try:
            observe=getattr(self.scorer,'readiness',None); model=observe() if callable(observe) else {}
            result['_manifest_id']=model.get('manifest_id') if model.get('ready') is True else None
            if getattr(self.scorer,'manifest_sha256',None): result['_manifest_sha256']=self.scorer.manifest_sha256
        except Exception: result['_manifest_id']=None
        return result

    @staticmethod
    def check_version(row,expected):
        if type(expected) is not int or expected<0: raise DomainError('INPUT_INVALID','Integer resource version required',422)
        if row['version']!=expected: raise DomainError('ROUND_VERSION_CONFLICT','Resource version changed',409)

    def touch(self,c,session_id):
        c.execute(s.sessions.update().where(s.sessions.c.id==session_id).values(analytics_commit_counter=s.sessions.c.analytics_commit_counter+1))
        observer=self.analytics or self.analytics_observer
        persist=getattr(observer,'persist_sealed',None)
        if callable(persist): persist(c,session_id)

    def event(self,c,session_id,event_type,payload,audiences=('DISPLAY','STUDIO')):
        for audience,field in [('DISPLAY','display_seq'),('STUDIO','studio_seq')]:
            if audience not in audiences: continue
            audience_payload=self.realtime.feedback_payload(c,session_id,payload,audience) if event_type=='guess.feedback' and hasattr(self,'realtime') else payload
            seq=c.execute(s.sessions.update().where(s.sessions.c.id==session_id).values({field:s.sessions.c[field]+1}).returning(s.sessions.c[field])).scalar_one()
            c.execute(s.outbox.insert().values(id=uid(),session_id=session_id,audience=audience,seq=seq,type=event_type,payload=audience_payload))

    def create_session(self,c,actor,body):
        selection.catalog_lock(c)
        if self.runtime_guard: self.runtime_guard(c,action='create')
        binding={}
        if body['mode']=='official':
            if self.official is None: raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Official coordinator unavailable',409)
            binding=self.official.authorize_create(c,actor,body)
            required={'app_id','room_id','room_binding_id','installation_id','live_profile','authorization_id','candidate_digest','grant_digest'}
            if not isinstance(binding,dict) or not required<=binding.keys() or any(not binding[k] for k in required): raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Verified official binding required',409)
        elif body['mode']!='rehearsal' or body['run_purpose']!='local' or body.get('room_binding_id') is not None or body.get('live_profile') is not None:
            raise DomainError('LIVE_PROFILE_MISMATCH','Local rehearsal purpose required',409)
        rules=self.validate_rules(RULES|body.get('rules',{}))
        if (not 30<=rules['duration_seconds']<=600 or not 3<=rules['intermission_seconds']<=30
                or not 1<=rules['difficulty_min']<=rules['difficulty_max']<=5):
            raise DomainError('INPUT_INVALID','Invalid session rules',422)
        if body['mode']=='official' and not rules['strict_round_code']: raise DomainError('INPUT_INVALID','Official strict round code is mandatory',422)
        sid=uid()
        c.execute(s.sessions.insert().values(id=sid,actor_id=actor,label=body['label'],mode=body['mode'],run_purpose=body['run_purpose'],status='CREATED',rules=rules,**binding))
        c.execute(s.selection_state.insert().values(id=uid(),session_id=sid,revision=0))
        c.execute(s.configuration.insert().values(id=uid(),session_id=sid))
        self.touch(c,sid)
        return self.session_detail(c,self.session(c,sid,actor))

    def session_command(self,c,actor,sid,body):
        selection.catalog_lock(c)
        cmd=body['command']
        if self.runtime_guard and (cmd in {'start','select_next','configure'} or (cmd=='recover' and body.get('args',{}).get('strategy')!='end_session')): self.runtime_guard(c,action='recover' if cmd=='recover' else cmd,session_id=sid)
        row=self.session(c,sid,actor)
        if cmd=='select_next':
            args=body.get('args',{}); state=selection.pointer(c,sid)
            if args.get('expected_selection_id')!=state['selection_id'] or args.get('expected_selection_version')!=state['revision']: raise DomainError('NEXT_SELECTION_VERSION_CONFLICT','Selection changed',409)
            self.check_version(row,body['expected_version'])
            if set(args)-{'strategy','puzzle_id','expected_selection_id','expected_selection_version'} or type(args.get('expected_selection_version')) is not int: raise DomainError('INPUT_INVALID','Invalid selection args',422)
            if args.get('strategy') not in {'random','specific'}: raise DomainError('INPUT_INVALID','Invalid selection strategy',422)
            if args['strategy']=='specific':
                puzzle_id=args.get('puzzle_id')
                if not isinstance(puzzle_id,str) or not puzzle_id: raise DomainError('INPUT_INVALID','Specific selection requires puzzle_id',422)
                if not c.execute(sa.select(s.puzzles.c.id).where(s.puzzles.c.puzzle_id==puzzle_id)).first():
                    raise DomainError('RESOURCE_NOT_FOUND','Puzzle not found',404)
                if selection.eligible(c,row,'specific',puzzle_id) is None:
                    raise DomainError('NO_ELIGIBLE_PUZZLE','Selected puzzle is not eligible for the next round',409)
            old=state['selection_id']; result=selection.replace(c,row,args['strategy'],args.get('puzzle_id')); self.selection_notice(c,sid,old,result,'OPERATOR_SELECTION',uid()); self.touch(c,sid)
            version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==sid)).scalar_one()
            return {'command_id':uid(),'status':'applied','resource_version':version,'state':result,'error':None}
        self.check_version(row,body['expected_version'])
        if row['mode']=='replay' and (cmd=='start' or (cmd=='recover' and body.get('args',{}).get('strategy')!='end_session')): raise DomainError('FORBIDDEN','Use replay job controls and reliable checkpoints',403)
        if cmd=='configure':
            args=body.get('args',{}); rules=args.get('next_rules')
            if set(args)!={'apply','next_rules'} or args.get('apply')!='next_round' or not isinstance(rules,dict) or set(rules)!=set(RULES): raise DomainError('INPUT_INVALID','Complete next_rules required',422)
            rules=self.validate_rules(rules)
            if row['mode']=='official' and not rules['strict_round_code']: raise DomainError('INPUT_INVALID','Official strict round code is mandatory',422)
            c.execute(s.configuration.update().where(s.configuration.c.session_id==sid).values(next_rules=rules,next_rules_version=s.configuration.c.next_rules_version+1))
            c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(version=s.sessions.c.version+1))
            rules_version=c.execute(sa.select(s.configuration.c.next_rules_version).where(s.configuration.c.session_id==sid)).scalar_one()
            result=self.reselect(c,row,'NEXT_RULES_CHANGED',f'rules:{rules_version}',content=rules)
            version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==sid)).scalar_one()
            return {'command_id':uid(),'status':'applied','resource_version':version,'state':result,'error':None}
        if cmd=='start':
            if row['status']!='CREATED': raise DomainError('ROUND_CLOSED','Session already started',409)
            if body.get('args',{}).get('run_purpose')!=row['run_purpose']: raise DomainError('LIVE_PROFILE_MISMATCH','Purpose mismatch',409)
            if row['mode']=='official': return self.official.prepare_start(c,row,fresh_start=True)
            primary=c.execute(sa.select(s.displays.c.id).where(s.displays.c.session_id==sid,s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False),s.displays.c.expires_at>now())).first()
            if not primary: raise DomainError('READINESS_NOT_MET','Primary display required',409,details={'blockers':['display']})
            permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==actor)).scalar_one()
            next_rules=selection.rules_for(c,row)
            readiness=self.readiness.evaluate(c,row|{'rules':next_rules},permissions)
            if any(readiness['checks'][name]['status']!='pass' for name in ('db','model','display')): raise DomainError('READINESS_NOT_MET','Local dependencies are not ready',409,details={'blockers':readiness['profiles']['local']['blockers']})
            candidate=selection.eligible(c,row,'random')
            if candidate is None: raise DomainError('NO_ELIGIBLE_PUZZLE','No eligible puzzle for next rules',409)
            selected=selection.pointer(c,sid)
            if selected['selection_id']:
                preview=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==selected['selection_id'])).mappings().one()
                if preview['status']=='READY':
                    saved=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==preview['puzzle_version_id'])).mappings().first()
                    if (not saved or saved['status']!='published' or not next_rules['difficulty_min']<=saved['difficulty']<=next_rules['difficulty_max'] or (next_rules.get('category_id') and saved['category_id']!=next_rules['category_id']) or not all(saved[key] for key in ('manifest_id','calibration_id','evaluation_id','approved_by'))): raise DomainError('NO_ELIGIBLE_PUZZLE','Saved next selection is no longer eligible',409)
            readiness=self.readiness.evaluate(c,row|{'rules':next_rules},permissions)
            if not readiness['can_rehearse']: raise DomainError('READINESS_NOT_MET','Local dependencies are not ready',409,details={'blockers':readiness['profiles']['local']['blockers']})
            status='ACTIVE'
        elif cmd=='recover':
            strategy=body.get('args',{}).get('strategy')
            active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid,s.rounds.c.status.in_(ACTIVE_ROUNDS)).with_for_update()).mappings().first()
            if row['mode']=='official':
                if strategy=='resume_local_round': raise DomainError('FORBIDDEN','Official rounds cannot resume local checkpoints',403)
                if strategy not in {'new_round','end_session'}: raise DomainError('INPUT_INVALID','Unknown official recovery strategy',422)
                if self.official is None: raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Official coordinator unavailable',409)
                return self.official.command(c,row,active,'recover',body.get('args',{}))
            if strategy=='resume_local_round':
                if row['mode']=='official': raise DomainError('FORBIDDEN','Official rounds cannot resume local checkpoints',403)
                if not active or active['status']!='PAUSED': raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE','Persistent PAUSED checkpoint required',409)
                permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==actor)).scalar_one()
                ready=self.readiness.evaluate(c,row,permissions)
                if not ready['can_rehearse']: raise DomainError('READINESS_NOT_MET','Recovery dependencies unavailable',409,details={'blockers':ready['profiles']['local']['blockers']})
                clock=LocalRoundClock.restore({'boot_id':active['boot_id'],'state':'PAUSED','remaining_ms':active['remaining_ms'],'mono_started_ns':None},self.boot_id,explicit_resume=True); clock.resume()
                c.execute(s.rounds.update().where(s.rounds.c.id==active['id']).values(status='RUNNING',boot_id=self.boot_id,mono_started_ns=clock.mono_started_ns,deadline_at=now()+timedelta(milliseconds=active['remaining_ms']),version=active['version']+1))
                status='ACTIVE'
            elif strategy in {'new_round','end_session'}:
                if active:
                    from app.modules.rounds.worker import LocalRoundWorker
                    LocalRoundWorker(self).cancel_jobs(c,active,'RESULT_STALE')
                    self.settle(c,row,active,'INTERRUPTED',None,None)
                status='ENDED' if strategy=='end_session' else 'ACTIVE'
            else: raise DomainError('INPUT_INVALID','Unknown recovery strategy',422)
        elif cmd=='end':
            active=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid,s.rounds.c.status.in_(ACTIVE_ROUNDS)).with_for_update()).mappings().first()
            if row['mode']=='official': return self.official.command(c,row,active,'end',body.get('args',{}))
            if active:
                from app.modules.rounds.worker import LocalRoundWorker
                LocalRoundWorker(self).cancel_jobs(c,active,'SESSION_ENDED')
                self.settle(c,row,active,'INTERRUPTED',None,None)
            status='ENDED'
        else: raise DomainError('INPUT_INVALID','Unsupported session command',422)
        if row['mode']=='replay' and status=='ENDED':
            c.execute(s.replay_jobs.update().where(s.replay_jobs.c.session_id==sid,s.replay_jobs.c.status.in_(['queued','running'])).values(status='cancelled',state='CANCELLED',version=s.replay_jobs.c.version+1,claim_token=None,lease_until=None))
        c.execute(s.sessions.update().where(s.sessions.c.id==sid).values(status=status,version=row['version']+1))
        if cmd=='start': self.observe(c,'session_started',sid,{'started_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        elif status=='ENDED' and row['status']!='ENDED': self.observe(c,'session_ended',sid,{'ended_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        if cmd=='start' or (cmd=='recover' and body.get('args',{}).get('strategy')=='new_round'):
            current=self.session(c,sid,actor); pointer=selection.pointer(c,sid)
            saved=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
            request={'session_id':sid,'expected_session_version':current['version']}
            if saved and saved['status']=='READY': request|={'selection_id':saved['id'],'expected_selection_version':pointer['revision']}
            else: request['selection']={'strategy':'random'}
            self.create_round(c,actor,request,runtime_action='recover' if cmd=='recover' else None)
        if self.runtime_recovery_completed and (cmd=='recover' or (cmd=='end' and row['status']=='RECOVERY_REQUIRED')): self.runtime_recovery_completed(c,session_id=sid)
        self.touch(c,sid)
        version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==sid)).scalar_one()
        return {'command_id':uid(),'status':'applied','resource_version':version,'state':status,'error':None}

    def ticket(self,c,actor,body):
        self.session(c,body['session_id'],actor)
        token=uid(); tid=uid(); expiry=now()+timedelta(seconds=60)
        c.execute(s.tickets.insert().values(id=tid,session_id=body['session_id'],token_hash=token_hash(token),purpose=body['purpose'],expires_at=expiry))
        return {'ticket_id':tid,'ticket':token,'expires_at':expiry.isoformat(timespec='milliseconds').replace('+00:00','Z')}

    def exchange(self,token):
        with self.db.engine.begin() as c:
            candidate=c.execute(sa.select(s.tickets.c.session_id).where(s.tickets.c.token_hash==token_hash(token))).scalar_one_or_none()
            if candidate is None: raise DomainError('DISPLAY_UNAUTHORIZED','Invalid display ticket',403)
            self.session(c,candidate)
            row=c.execute(sa.select(s.tickets).where(s.tickets.c.token_hash==token_hash(token)).with_for_update()).mappings().first()
            if row['used'] or row['revoked'] or row['expires_at']<=now(): raise DomainError('DISPLAY_UNAUTHORIZED','Ticket unavailable',403)
            if row['purpose']=='primary':
                active=c.execute(sa.select(s.displays).where(s.displays.c.session_id==candidate,s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False))).mappings().first()
                if active and active['expires_at']>now(): raise DomainError('DISPLAY_UNAUTHORIZED','Primary already bound',403)
                if active: c.execute(s.displays.update().where(s.displays.c.id==active['id']).values(revoked=True))
            cookie=uid(); values=dict(id=uid(),session_id=candidate,ticket_id=row['id'],token_hash=token_hash(cookie),purpose=row['purpose'],csrf=uid(),render_generation=1,expires_at=now()+timedelta(hours=8))
            c.execute(s.tickets.update().where(s.tickets.c.id==row['id']).values(used=True))
            c.execute(s.displays.insert().values(**values)); self.touch(c,candidate)
            return cookie,values

    def create_round(self,c,actor,body,*,runtime_action=None,replay_job_id=None):
        selection.catalog_lock(c)
        if self.runtime_guard: self.runtime_guard(c,action=runtime_action or 'next_round',session_id=body['session_id'])
        session=self.session(c,body['session_id'],actor); self.check_version(session,body['expected_session_version'])
        if session['status']!='ACTIVE': raise DomainError('ROUND_CLOSED','Session must be ACTIVE',409)
        if session['mode']=='replay':
            job=c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==replay_job_id,s.replay_jobs.c.session_id==session['id'],s.replay_jobs.c.actor_id==actor,s.replay_jobs.c.status.in_(['queued','running']))).mappings().first()
            if not job:raise DomainError('FORBIDDEN','Only the trusted replay scheduler opens replay rounds',403)
        if c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==session['id'],s.rounds.c.status.in_(ACTIVE_ROUNDS))).first(): raise DomainError('ROUND_CLOSED','An active round exists',409)
        if session['mode']=='official':
            if self.official is None: raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Official coordinator unavailable',409)
            self.official.require_current(c,session,None)
        puzzle,selection_id,selection_revision=selection.consume(c,session,body)
        if session['mode']!='official':
            permissions=c.execute(sa.select(s.operators.c.permissions).where(s.operators.c.id==actor)).scalar_one()
            ready=self.readiness.evaluate(c,session|{'rules':selection.rules_for(c,session)},permissions)
            if any(ready['checks'][k]['status']!='pass' for k in ('db','model','wordbank','display')): raise DomainError('READINESS_NOT_MET','New round dependencies unavailable',409)
        rules=selection.rules_for(c,session)
        c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(rules=rules))
        c.execute(s.configuration.update().where(s.configuration.c.session_id==session['id']).values(rules_version=s.configuration.c.next_rules_version,next_rules=None))
        code=None
        for _ in range(32):
            candidate=''.join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(4))
            if not c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.short_code==candidate)).first(): code=candidate; break
        if code is None:
            # Random probing is only the fast path. Near exhaustion it can miss every
            # free code, so one deterministic scan decides availability instead of
            # reporting a false SHORT_CODE_EXHAUSTED; a genuinely full space stays 409.
            used={value for (value,) in c.execute(sa.select(s.rounds.c.short_code))}
            for item in itertools.product(SHORT_CODE_ALPHABET,repeat=4):
                candidate=''.join(item)
                if candidate not in used: code=candidate; break
        if code is None: raise DomainError('SHORT_CODE_EXHAUSTED','No retained short code available',409)
        rid=uid(); duration=rules['duration_seconds']*1000
        frozen={key:puzzle[key] for key in ['word','aliases','category_id','difficulty','explanation','manifest_id','calibration_id','evaluation_id','hints']}
        frozen.update(puzzle.get('scoring_artifact') or {})
        frozen['puzzle_version_id']=puzzle['id']
        if session['mode']=='replay': frozen['time_origin']='local_replay'
        elif session['mode']=='official': frozen['time_origin']='relay'
        frozen['rules_version']=c.execute(sa.select(s.configuration.c.rules_version).where(s.configuration.c.session_id==session['id'])).scalar_one()
        # PREPARING intent and local opening barrier are committed atomically for local mode.
        values=dict(id=rid,session_id=session['id'],puzzle_version_id=puzzle['id'],status='PREPARING' if session['mode']=='official' else 'RUNNING',short_code=code,boot_id=self.boot_id,mono_started_ns=time.monotonic_ns(),remaining_ms=duration,deadline_at=now()+timedelta(milliseconds=duration),frozen_puzzle=frozen,frozen_rules=rules)
        c.execute(s.rounds.insert().values(**values))
        c.execute(s.next_selections.update().where(s.next_selections.c.id==selection_id).values(consumed_round_id=rid))
        if session['mode']!='official': self.reselect(c,session|{'rules':rules},'ROUND_RUNNING',rid,content={'round_id':rid})
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid)).mappings().one()
        if session['mode']=='official':
            pending=self.official.create_round(c,session,row)
            return {'round_id':rid,'short_code':code,'status':'PREPARING','version':1,'session_version':self.session(c,session['id'])['version'],'window_epoch':str(row['epoch']),'selection_id':selection_id,'selection_version':selection_revision,'command':pending}
        self.event(c,session['id'],'round.started',{'round':self.public_round(row)}); self.observe(c,'round_started',session['id'],{'round_id':rid,'started_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':row['mono_started_ns'],'category_id':frozen['category_id'],'difficulty':frozen['difficulty']}); self.touch(c,session['id'])
        return {'round_id':rid,'short_code':code,'status':'RUNNING','version':1,'session_version':c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==session['id'])).scalar_one(),'window_epoch':'1','selection_id':selection_id,'selection_version':selection_revision}

    def round(self,c,rid,actor,lock=True):
        sid=c.execute(sa.select(s.rounds.c.session_id).where(s.rounds.c.id==rid)).scalar_one_or_none()
        if sid is None: raise DomainError('RESOURCE_NOT_FOUND','Round not found',404)
        session=self.session(c,sid,actor,lock=lock)
        query=sa.select(s.rounds).where(s.rounds.c.id==rid)
        row=c.execute(query.with_for_update() if lock else query).mappings().one()
        return session,dict(row)

    def round_command(self,c,actor,rid,body):
        session,row=self.round(c,rid,actor)
        if c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==rid)).first(): raise DomainError('ROUND_CLOSED','Round already settled',409)
        self.check_version(row,body['expected_version']); cmd=body['command']
        if session['mode']=='replay' and cmd!='reveal_hint': raise DomainError('FORBIDDEN','Use replay job controls for the virtual clock',403)
        if cmd not in {'pause','resume','skip','void','reveal_hint'}: raise DomainError('INPUT_INVALID','Unsupported round command',422)
        if cmd in {'pause','resume'} and body.get('args',{}): raise DomainError('INPUT_INVALID','Pause and resume args must be empty',422)
        if cmd in {'skip','void'}:
            reason=body.get('args',{}).get('reason')
            if not isinstance(reason,str) or not reason.strip() or len(reason)>200: raise DomainError('INPUT_INVALID','Reason required',422)
        if session['mode']=='official' and cmd!='reveal_hint':
            if self.official is None: raise DomainError('LIVE_AUTHORIZATION_REQUIRED','Official coordinator unavailable',409)
            return self.official.command(c,session,row,cmd,body.get('args',{}))
        if cmd in {'pause','resume'} and body.get('args',{}): raise DomainError('INPUT_INVALID','Pause and resume args must be empty',422)
        if cmd in {'void','skip'}:
            reason=body.get('args',{}).get('reason','').strip()
            if not reason or len(reason)>200: raise DomainError('INPUT_INVALID','Reason required',422)
            from app.modules.rounds.worker import LocalRoundWorker
            LocalRoundWorker(self).cancel_jobs(c,row,'ROUND_VOID')
            for entry in c.execute(sa.select(s.ledger).where(s.ledger.c.round_id==rid,s.ledger.c.reverses_id.is_(None))).mappings():
                c.execute(s.ledger.insert().values(id=uid(),round_id=rid,participant_id=entry['participant_id'],reason='VOID',reference_id=entry['id'],delta=-entry['delta'],reverses_id=entry['id']))
            self.settle(c,session,row,'VOID',None,None)
        elif cmd=='pause' and row['status']=='RUNNING':
            self.valid_clock(row)
            clock=self.clock(row); remain=clock.pause()
            c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(status='PAUSED',remaining_ms=remain,deadline_at=None,version=row['version']+1))
        elif cmd=='resume' and row['status']=='PAUSED':
            if row['boot_id']!=self.boot_id: raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE','Explicit cross-boot recovery required',409)
            c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(status='RUNNING',boot_id=self.boot_id,mono_started_ns=time.monotonic_ns(),deadline_at=now()+timedelta(milliseconds=row['remaining_ms']),version=row['version']+1))
        elif cmd=='reveal_hint' and row['status'] in {'RUNNING','PAUSED'}:
            stage=body.get('args',{}).get('stage')
            if type(stage) is not int: raise DomainError('INPUT_INVALID','Hint stage integer required',422)
            hints=row['frozen_puzzle'].get('hints',[])
            hint=next((h for h in hints if h.get('stage')==stage),None)
            if stage not in {1,2,3} or hint is None or not isinstance(hint.get('text'),str) or len(hint['text'])>120: raise DomainError('INPUT_INVALID','No approved hint for stage',422)
            published=row['published_hints']
            if any(h['stage']==stage for h in published): raise DomainError('ROUND_VERSION_CONFLICT','Hint already revealed',409)
            published=published+[{'stage':stage,'text':hint['text'],'revealed_at':now().isoformat(timespec='milliseconds').replace('+00:00','Z')}]
            c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(published_hints=published,version=row['version']+1))
            self.event(c,session['id'],'hint.revealed',{'round_id':rid,'published_hints':published})
            self.observe(c,'hint_revealed',session['id'],{'round_id':rid,'stage':stage,'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        else: raise DomainError('ROUND_CLOSED','Command unavailable for round state',409)
        updated=c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid)).mappings().one()
        if cmd in {'pause','resume'}: self.observe(c,'round_paused' if cmd=='pause' else 'round_resumed',session['id'],{'round_id':rid,'mono_ns':time.monotonic_ns(),'boot_id':self.boot_id})
        if cmd in {'pause','resume'}: self.event(c,session['id'],'round.paused' if cmd=='pause' else 'round.resumed',{'round':self.public_round(updated)})
        self.touch(c,session['id'])
        return {'command_id':uid(),'status':'applied','resource_version':updated['version'],'state':updated['status'],'error':None}

    def observe(self,c,kind,session_id,facts):
        observer=self.analytics or self.analytics_observer
        if observer is not None: observer.observe(c,kind,session_id,facts)

    def committed(self,sid,gid,phase):
        observer=self.analytics or self.analytics_observer
        if observer is not None and hasattr(observer,'after_commit'):
            try: observer.after_commit(sid,gid,phase,self.boot_id,time.monotonic_ns())
            except Exception: self.analytics_observation_error=True  # Missing sample remains missing; never fabricates time.

    def reselect(self,c,session,reason,trigger,content=None):
        """Apply a durable trigger once, inside the caller's catalog/session locks.

        The trigger record and outbox share the mutation transaction. A restart
        or retry resolves this record before any candidate query/random draw.
        """
        sid=session['id']; key=f'{sid}:{reason}:{trigger}'
        path=f'/api/v1/live-sessions/{sid}/selection'
        digest=hashlib.sha256(json.dumps({'reason':reason,'trigger':trigger,'content':content},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        previous=c.execute(sa.select(s.idempotency).where(s.idempotency.c.actor_id=='system:selection',s.idempotency.c.method=='SYSTEM',s.idempotency.c.path==path,s.idempotency.c.key==key)).mappings().first()
        if previous:
            if previous['request_hash']!=digest: raise DomainError('IDEMPOTENCY_KEY_REUSED','Selection trigger binds different content',409)
            return previous['response']
        old=selection.pointer(c,sid)
        result=selection.replace(c,session,'random',reason=reason)
        self.selection_notice(c,sid,old['selection_id'],result,reason,trigger)
        c.execute(s.idempotency.insert().values(id=uid(),actor_id='system:selection',method='SYSTEM',path=path,key=key,request_hash=digest,response=result))
        return result

    def selection_notice(self,c,sid,old,new,reason,trigger):
        key=f'{sid}:{reason}:{trigger}'
        state={'old_selection_id':old,'selection_id':new['selection_id'],'version':new['version'],'status':new['status'],'reason':reason}
        previous=c.execute(sa.select(s.commands.c.response).where(s.commands.c.trigger_key==key)).scalar_one_or_none()
        if previous:
            digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            if digest(previous['state'])!=digest(state): raise DomainError('IDEMPOTENCY_KEY_REUSED','Selection notice binds different content',409)
            return previous
        version=c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id==sid)).scalar_one()
        result={'command_id':uid(),'status':'applied','resource_version':version,'state':state,'error':None,'be_extension_version':'LW-BE/1.1.0'}
        c.execute(s.commands.insert().values(id=result['command_id'],session_id=sid,actor_id='system:selection',response=result,trigger_key=key))
        self.event(c,sid,'command.updated',{'command':result},audiences=('STUDIO',)); self.touch(c,sid)
        return result

    @staticmethod
    def session_detail(c,row):
        active=c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==row['id'],s.rounds.c.status.in_(ACTIVE_ROUNDS))).scalar_one_or_none()
        return {'session_id':row['id'],'version':row['version'],'status':row['status'],'mode':row['mode'],'run_purpose':row['run_purpose'],'room_binding_id':row.get('room_binding_id'),'session_rules':row['rules'],'next_rules':c.execute(sa.select(s.configuration.c.next_rules).where(s.configuration.c.session_id==row['id'])).scalar_one_or_none(),'active_round_id':active}

    def private_round(self,row,c=None):
        return {'round_id':row['id'],'version':row['version'],'status':row['status'],'window_epoch':str(row['epoch']),'puzzle_version_id':row['puzzle_version_id'],'answer':row['frozen_puzzle']['word'],'aliases':row['frozen_puzzle']['aliases'],'manifest_id':row['frozen_puzzle']['manifest_id'],'calibration_id':row['frozen_puzzle']['calibration_id'],'rules_version':row['frozen_puzzle'].get('rules_version',1),'clock':self.public_round(row)['clock'],'pending_command_ids':[x['command_id'] for x in self.pending_commands(c,row['session_id'],row['id'])] if c is not None else []}

    def next_selection(self,c,sid):
        return selection.projection(c,sid)

    def clock(self,row):
        return LocalRoundClock.restore({'boot_id':row['boot_id'],'state':row['status'] if row['status'] in {'RUNNING','PAUSED'} else 'IDLE','mono_started_ns':row['mono_started_ns'],'remaining_ms':row['remaining_ms']},self.boot_id)

    def valid_clock(self,row):
        if row['boot_id']!=self.boot_id: raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE','Old boot clock cannot be resumed',409)
        if self.clock(row).expired(): raise DomainError('ROUND_CLOSED','Local window elapsed',409)

    def guess(self,c,actor,body,source_id,trusted_replay=None,trusted_official=None,trusted_rehearsal=None):
        session,row=self.round(c,body['round_id'],actor)
        source_mode='manual'; identity='manual:'+actor; virtual_ms=None
        if trusted_rehearsal is not None:
            from app.modules.inbox.admission import RehearsalAdmission
            if not isinstance(trusted_rehearsal,RehearsalAdmission) or trusted_rehearsal._token is not self._rehearsal_token or session['mode']!='rehearsal' or session['run_purpose']!='local': raise DomainError('FORBIDDEN','Boot-bound local rehearsal admission required',403)
            source_mode='bridge';identity='bridge:'+trusted_rehearsal.participant_ref
        if trusted_replay is not None:
            from app.modules.replays.admission import ReplayAdmission
            if not isinstance(trusted_replay,ReplayAdmission): raise DomainError('FORBIDDEN','Trusted replay admission required',403)
            replay=c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==trusted_replay.job_id)).mappings().one()
            if replay['actor_id']!=actor or replay['session_id']!=session['id'] or session['mode']!='replay' or replay['status']!='running' or replay['state']!='RUNNING' or replay['boot_id']!=self.boot_id or replay['cursor']!=trusted_replay.ordinal: raise DomainError('FORBIDDEN','Replay admission scope mismatch',403)
            frozen=replay['input']['events'][trusted_replay.ordinal]
            if source_id!=f"{replay['id']}:{frozen['source_id']}" or frozen['guess']!=body['word'] or frozen['offset_ms']!=trusted_replay.virtual_ms: raise DomainError('FORBIDDEN','Frozen replay input required',403)
            if trusted_replay.virtual_ms>=replay['checkpoint'].get('round_started_ms',0)+row['frozen_rules']['duration_seconds']*1000: raise DomainError('ROUND_CLOSED','Replay window elapsed',409)
            source_mode='replay'; identity='replay:'+replay['id']+':'+frozen.get('participant_key','single-participant'); virtual_ms=trusted_replay.virtual_ms
        if trusted_official is not None:
            from app.modules.inbox.admission import OfficialAdmission
            if not isinstance(trusted_official,OfficialAdmission) or session['mode']!='official' or self.official is None: raise DomainError('FORBIDDEN','Trusted relay admission required',403)
            self.official.require_current(c,session,row)
            if trusted_official.window_epoch!=row['epoch']: raise DomainError('ROUND_CLOSED','Old relay epoch',409)
            if row['deadline_at'] is None or trusted_official.received_at>=row['deadline_at']: raise DomainError('ROUND_CLOSED','Relay window elapsed',409)
            if row['close_seq'] is not None and trusted_official.stream_seq>row['close_seq']: raise DomainError('ROUND_CLOSED','Beyond relay close barrier',409)
            source_mode=trusted_official.source_mode; identity='official:'+trusted_official.participant_key
        if session['id']!=body['session_id'] or (trusted_replay is None and trusted_official is None and session['mode']!='rehearsal'): raise DomainError('FORBIDDEN','Rehearsal session required',403)
        if row['status']!='RUNNING' and not (trusted_official is not None and row['status']=='DRAINING' and row['close_seq'] is not None): raise DomainError('ROUND_CLOSED','Round not accepting input',409)
        if body['short_code']!=row['short_code']: raise DomainError('INPUT_INVALID','Round code mismatch',422)
        if trusted_replay is None and trusted_official is None: self.valid_clock(row)
        observer=getattr(self.scorer,'readiness',None)
        if callable(observer):
            try:model=observer()
            except Exception:model={}
            if model.get('ready') is not True or model.get('manifest_id')!=row['frozen_puzzle']['manifest_id']:raise DomainError('DEPENDENCY_UNAVAILABLE','Current model health is unavailable',503,True)
        display_name=(('演练 · '+body['display_name'])[:80] if trusted_replay is None and trusted_official is None and trusted_rehearsal is None else body['display_name'])
        participant=c.execute(sa.select(s.participants).where(s.participants.c.session_id==session['id'],s.participants.c.identity_key==identity)).mappings().first()
        if not participant:
            pid=uid(); c.execute(s.participants.insert().values(id=pid,session_id=session['id'],identity_key=identity,display_name=display_name))
        else: pid=participant['id']
        word=normalize_word(body['word']); mono=time.monotonic_ns()
        relay_ms=int(trusted_official.received_at.timestamp()*1000) if trusted_official is not None else None
        rate_column=s.guesses.c.admitted_relay_ms if trusted_official is not None else s.guesses.c.admitted_virtual_ms if virtual_ms is not None else s.guesses.c.admitted_mono_ns
        rate_now=relay_ms if trusted_official is not None else virtual_ms if virtual_ms is not None else mono; second=1000 if virtual_ms is not None or trusted_official is not None else 1000000000
        recent=list(c.execute(sa.select(rate_column).where(s.guesses.c.session_id==session['id'],s.guesses.c.participant_id==pid,rate_column>rate_now-10*second,sa.true() if virtual_ms is not None or trusted_official is not None else s.guesses.c.boot_id==self.boot_id)).scalars())
        if (recent and rate_now-max(recent)<second) or len(recent)>=5: raise DomainError('RATE_LIMITED','Input rate exceeded',429,True)
        if not self.admission_limiter.allow(): raise DomainError('RATE_LIMITED','Global admission budget exceeded',429,True)
        if c.execute(sa.select(s.guesses.c.id).where(s.guesses.c.round_id==row['id'],s.guesses.c.participant_id==pid,s.guesses.c.normalized_word==word)).first(): raise DomainError('DUPLICATE_GUESS','Word already entered this round',409)
        seq=c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(admission_seq=s.sessions.c.admission_seq+1).returning(s.sessions.c.admission_seq)).scalar_one()
        exact=word in [normalize_word(row['frozen_puzzle']['word']),*[normalize_word(v) for v in row['frozen_puzzle']['aliases']]]
        gid=uid()
        c.execute(s.guesses.insert().values(id=gid,session_id=session['id'],round_id=row['id'],epoch=row['epoch'],participant_id=pid,source_mode=source_mode,source_id=source_id,normalized_word=word,admission_seq=seq,status='scored' if exact else 'queued',display_score=100 if exact else None,solved=exact,admitted_mono_ns=mono,admitted_virtual_ms=virtual_ms,admitted_relay_ms=relay_ms,provider_test=trusted_official.provider_test if trusted_official is not None else True if trusted_rehearsal is not None else None,boot_id=self.boot_id))
        self.trace(c,gid,'admission','succeeded')
        result={'feedback_id':gid,'word':word,'display_name':display_name,'status':'scored' if exact else 'queued','display_score':100.0 if exact else None,'solved':exact,'message':'' if exact else 'SCORING_PENDING','admission_seq':str(seq)}
        # Unconfigured inference is an explicit persisted failure, never a zero score.
        if exact:
            if trusted_official is not None:
                c.execute(s.rounds.update().where(s.rounds.c.id==row['id']).values(winner_id=pid,winner_seq=trusted_official.stream_seq,status='DRAINING',version=s.rounds.c.version+1))
                # The relay keeps one barrier: a late winner inside the original close_seq
                # is confirmed against that barrier and never reopens the window (WT19).
                # Skipping this when close_seq was already set silently dropped the winner.
                self.official.command(c,session,row,'close',{'reason':'SOLVED','winner_seq':str(trusted_official.stream_seq)})
            else: self.begin_drain(c,session,row,pid,seq)
            pending=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).where(s.guesses.c.round_id==row['id'],s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
            if not pending and trusted_official is None: self.settle(c,session,row,'SOLVED',pid,seq)
        else:
            c.execute(sa.text('SELECT pg_advisory_xact_lock(674219386)'))
            queued=c.execute(sa.select(sa.func.count()).select_from(s.score_jobs).where(s.score_jobs.c.status.in_(['queued','running','waiting']))).scalar_one()
            if queued>=200: raise DomainError('RATE_LIMITED','Score queue capacity exceeded',429,True)
            c.execute(s.score_jobs.insert().values(id=uid(),guess_id=gid,status='queued',deadline_at=now()+timedelta(seconds=2),input_versions=self.score_input_versions(row)))
        if exact: self.trace(c,gid,'feedback','succeeded')
        else: self.trace(c,gid,'scoring','queued')
        self.observe(c,'guess_terminal',session['id'],{'observation_id':gid,'guess_id':gid,'round_id':row['id'],'participant_id':pid,'source_mode':source_mode,'provider_test':trusted_official.provider_test if trusted_official is not None else True if trusted_rehearsal is not None else None,'received_at':now().isoformat(),'outcome':'successful' if exact else 'nonterminal','is_guess_attempt':True})
        self.event(c,session['id'],'guess.feedback',{'feedbacks':[result],'effects':[]})
        self.touch(c,session['id'])
        return result

    def submit_guess(self,actor,key,body):
        return self._submit_guess(actor,key,body,'/api/v1/rehearsal/guesses')

    def _submit_guess(self,actor,key,body,path,admission=None):
        # Only volatile rehearsal ingress is shed before opening a transaction.
        # Official deliveries keep their durable connector ordering and ledger.
        # The bound is process-wide concurrent rehearsal admission work: four
        # transactions may wait on one session row at a time, and everything past
        # that is refused immediately with an explicit 429 instead of queued.
        if path not in {'/api/v1/rehearsal/guesses','/internal/rehearsal-input'}:
            return self._submit_guess_in_slot(actor,key,body,path,admission)
        if not self._admission_slots.acquire(blocking=False):
            raise DomainError('RATE_LIMITED','Admission concurrency capacity exceeded',429,True)
        try:
            return self._submit_guess_in_slot(actor,key,body,path,admission)
        finally:
            self._admission_slots.release()

    def _submit_guess_in_slot(self,actor,key,body,path,admission=None):
        source_id=('bridge:'+key) if admission is not None and isinstance(key,str) else key
        try:
            result=self.command(actor,'POST',path,key,body,lambda c:self.guess(c,actor,body,source_id,trusted_rehearsal=admission))
        except DomainError as error:
            if error.code not in {'INPUT_INVALID','DUPLICATE_GUESS','RATE_LIMITED','ROUND_CLOSED','DEPENDENCY_UNAVAILABLE'}: raise
            code=error.code
            def reject(c):
                session,row=self.round(c,body['round_id'],actor)
                rid=uid(); status={'INPUT_INVALID':'format_invalid','DUPLICATE_GUESS':'duplicate','RATE_LIMITED':'rate_limited','ROUND_CLOSED':'round_closed','DEPENDENCY_UNAVAILABLE':'system_failed'}[code]
                c.execute(s.rejections.insert().values(id=rid,session_id=session['id'],round_id=row['id'],reason_code=code,source_id=source_id,source_mode='bridge' if admission is not None else 'manual',provider_test=True if admission is not None else None))
                self.trace(c,rid,'admission','rejected',code)
                self.observe(c,'guess_rejected',session['id'],{'observation_id':rid,'round_id':row['id'],'participant_id':None,'source_mode':'bridge' if admission is not None else 'manual','provider_test':True if admission is not None else None,'received_at':now().isoformat(),'outcome':'system_failed' if code=='DEPENDENCY_UNAVAILABLE' else 'business_rejected','is_guess_attempt':True})
                result={'feedback_id':rid,'word':None,'display_name':body['display_name'],'status':status,'display_score':None,'solved':False,'message':code,'admission_seq':'0'}
                self.event(c,session['id'],'guess.feedback',{'feedbacks':[result],'effects':[]}); self.touch(c,session['id'])
                return result
            result=self.command(actor,'POST',path,key,body,reject)
        if result.get('message')=='DEPENDENCY_UNAVAILABLE':raise DomainError('DEPENDENCY_UNAVAILABLE','Model dependency unavailable',503,True,details={'feedback_id':result['feedback_id']})
        return result

    @staticmethod
    def trace(c,message_id,stage,state,reason=None):
        c.execute(s.trace_steps.insert().values(id=uid(),message_id=message_id,stage=stage,state=state,reason_code=reason,time=now()))

    def score_admitted(self,actor,key,body,result,path='/api/v1/replays'):
        if result['status']!='queued': return result
        gid=result['feedback_id']
        with self.db.engine.begin() as c:
            _,row=self.round(c,body['round_id'],actor)
            job=c.execute(sa.select(s.score_jobs).where(s.score_jobs.c.guess_id==gid).with_for_update()).mappings().one()
            if job['status'] in {'succeeded','failed','cancelled'}:
                guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==gid)).mappings().one()
                return result|{'status':guess['status'],'display_score':guess['display_score'],'message':job['error_code'] or ''}
            if job['status']=='running' and job['lease_until'] and job['lease_until']>now(): return result
            cached=job.get('cached_score'); completed_ns=job.get('score_completed_ns')
            expired=cached is None and job['deadline_at']<=now()
            invalid=row['status'] not in {'RUNNING','PAUSED','DRAINING'} or row['boot_id']!=self.boot_id or job['input_versions']!=self.score_input_versions(row)
            claim=uid()
            c.execute(s.score_jobs.update().where(s.score_jobs.c.id==job['id']).values(status='running',claim_token=claim,claim_boot=self.boot_id,attempt=s.score_jobs.c.attempt+1,lease_until=now()+timedelta(seconds=2) if cached is not None else min(job['deadline_at'],now()+timedelta(seconds=2))))
            frozen=row['frozen_puzzle']
            self.trace(c,gid,'scoring','running')
            self.touch(c,row['session_id'])
        # The scorer is an explicit process port; no SQL transaction spans inference.
        score=cached; failure=None
        try:
            if invalid:raise DomainError('RESULT_STALE','Score input no longer active',409)
            if expired: raise DomainError('SCORER_TIMEOUT','Scoring deadline elapsed',503)
            if cached is None: score=self.scorer.score(word=result['word'],frozen_puzzle=frozen)
            import inspect,asyncio
            if inspect.isawaitable(score): score=asyncio.run(score)
            if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<=score<=99.9:
                raise DomainError('SCORER_RESULT_INVALID','Scorer returned invalid non-exact score',503)
            score=round(float(score),1)
            if completed_ns is None: completed_ns=time.monotonic_ns()
        except DomainError as error: failure=error.code
        except Exception as error: failure=getattr(error,'code','DEPENDENCY_UNAVAILABLE')
        with self.db.engine.begin() as c:
            session,row=self.round(c,body['round_id'],actor)
            guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==gid).with_for_update()).mappings().one()
            job=c.execute(sa.select(s.score_jobs).where(s.score_jobs.c.guess_id==gid).with_for_update()).mappings().one()
            if job['claim_token']!=claim or job['status']!='running':
                return result|{'status':guess['status'],'display_score':guess['display_score'],'message':job['error_code'] or ''}
            if row['status'] not in {'RUNNING','PAUSED','DRAINING'} or row['boot_id']!=self.boot_id or job['lease_until'] is None or job['lease_until']<=now():
                failure='RESULT_STALE'
            if completed_ns is None or completed_ns-guess['admitted_mono_ns']>2000000000: failure=failure or 'SCORER_TIMEOUT'
            versions=self.score_input_versions(row)
            if job['input_versions']!=versions or job['claim_boot']!=self.boot_id: failure='RESULT_STALE'
            observer=getattr(self.scorer,'readiness',None)
            if callable(observer):
                try:
                    if observer().get('ready') is not True: failure='MODEL_DEPENDENCY_LOST'
                except Exception: failure='MODEL_DEPENDENCY_LOST'
            if session['mode']=='official':
                try: self.official.require_current(c,session,row,result=True)
                except DomainError as error:
                    if error.code=='CONNECTOR_RECONNECTING' and not failure and score is not None:
                        c.execute(s.score_jobs.update().where(s.score_jobs.c.id==job['id']).values(status='waiting',cached_score=score,score_completed_ns=completed_ns,lease_until=now()+timedelta(milliseconds=100),claim_token=None))
                        self.trace(c,gid,'scoring','waiting','CONNECTOR_RECONNECTING');self.touch(c,session['id'])
                        return result
                    failure='RESULT_STALE'
            updated=result|{'status':'system_failed' if failure else 'scored','display_score':None if failure else score,'message':failure or ''}
            c.execute(s.guesses.update().where(s.guesses.c.id==gid).values(status=updated['status'],display_score=updated['display_score']))
            c.execute(s.score_jobs.update().where(s.score_jobs.c.id==job['id']).values(status='failed' if failure else 'succeeded',error_code=failure,lease_until=None,score_completed_ns=completed_ns))
            if not failure:
                count=c.execute(sa.select(sa.func.count()).select_from(s.ledger).where(s.ledger.c.round_id==row['id'],s.ledger.c.participant_id==guess['participant_id'],s.ledger.c.reason=='PARTICIPATION')).scalar_one()
                if count<5: c.execute(s.ledger.insert().values(id=uid(),round_id=row['id'],participant_id=guess['participant_id'],reason='PARTICIPATION',reference_id=gid,delta=1))
                for threshold in [50,70,85]:
                    if score>=threshold and not c.execute(sa.select(s.ledger.c.id).where(s.ledger.c.round_id==row['id'],s.ledger.c.participant_id==guess['participant_id'],s.ledger.c.reason=='TIER',s.ledger.c.reference_id==str(threshold))).first():
                        c.execute(s.ledger.insert().values(id=uid(),round_id=row['id'],participant_id=guess['participant_id'],reason='TIER',reference_id=str(threshold),delta=2))
            self.trace(c,gid,'feedback','failed' if failure else 'succeeded',failure)
            self.observe(c,'guess_terminal',session['id'],{'observation_id':gid,'guess_id':gid,'round_id':row['id'],'participant_id':guess['participant_id'],'source_mode':guess['source_mode'],'provider_test':guess.get('provider_test'),'received_at':guess['created_at'].isoformat(),'outcome':'system_failed' if failure else 'successful','is_guess_attempt':True})
            self.event(c,session['id'],'guess.feedback',{'feedbacks':[updated],'effects':[]})
            self.event(c,session['id'],'ranking.updated',{'ranking':self.ranking(c,session['id'])})
            c.execute(s.idempotency.update().where(s.idempotency.c.actor_id==actor,s.idempotency.c.method=='POST',s.idempotency.c.response['feedback_id'].astext==gid).values(response=updated))
            if session['mode']=='official' and self.official is not None:
                self.official.terminal_feedback(c,gid)
            self.touch(c,session['id'])
        self.committed(session['id'],gid,'feedback')
        return updated

    def begin_drain(self,c,session,row,winner,seq):
        close=session['admission_seq'] if seq is None else seq
        c.execute(s.rounds.update().where(s.rounds.c.id==row['id']).values(status='DRAINING',winner_id=winner,winner_seq=seq,close_seq=close,drain_until_ns=time.monotonic_ns()+2000000000,version=row['version']+1))
        updated=c.execute(sa.select(s.rounds).where(s.rounds.c.id==row['id'])).mappings().one()
        self.observe(c,'round_paused',session['id'],{'round_id':row['id'],'boot_id':self.boot_id,'mono_ns':min(time.monotonic_ns(),row['mono_started_ns']+row['remaining_ms']*1000000)})
        self.event(c,session['id'],'round.draining',{'round':self.public_round(updated)})
        self.touch(c,session['id'])

    def settle(self,c,session,row,reason,winner,seq):
        rid=row['id']
        c.execute(s.settlements.insert().values(id=uid(),round_id=rid,reason=reason,winner_id=winner))
        if winner:
            c.execute(s.ledger.insert().values(id=uid(),round_id=rid,participant_id=winner,reason='WINNER',reference_id=rid,delta=20))
        c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(status='REVEALED',end_reason=reason,winner_id=winner,winner_seq=seq,version=s.rounds.c.version+1,remaining_ms=0,next_start_at=now()+timedelta(seconds=int(row['frozen_rules']['intermission_seconds'])) if row['frozen_rules'].get('auto_next') else None,next_due_ns=time.monotonic_ns()+int(row['frozen_rules']['intermission_seconds'])*1000000000))
        updated=c.execute(sa.select(s.rounds).where(s.rounds.c.id==rid)).mappings().one()
        self.observe(c,'round_settled',session['id'],{'round_id':rid,'reason':reason,'winner_id':winner,'ended_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        self.event(c,session['id'],'round.revealed',{'round':self.public_round(updated),'reveal':self.reveal(c,updated),'ranking':self.ranking(c,session['id'])})
        c.execute(s.rounds.update().where(s.rounds.c.id==rid).values(status='INTERMISSION'))
        updated=dict(updated)|{'status':'INTERMISSION'}
        self.event(c,session['id'],'intermission.started',{'round':self.public_round(updated),'reveal':self.reveal(c,updated)})

    def public_round(self,row):
        remaining=row['remaining_ms']
        replay=row['frozen_puzzle'].get('time_origin')=='local_replay'
        if not replay and row['frozen_puzzle'].get('time_origin')!='relay' and row['status']=='RUNNING' and row['boot_id']==self.boot_id: remaining=self.clock(row).remaining_ms()
        return {'round_id':row['id'],'status':row['status'],'version':row['version'],'window_epoch':str(row['epoch']),'short_code':row['short_code'],'category':row['frozen_puzzle']['category_id'],'answer_length':len(row['frozen_puzzle']['word']),'published_hints':row.get('published_hints',[]),'clock':{'time_origin':'relay' if row['frozen_puzzle'].get('time_origin')=='relay' else 'local_replay' if replay else 'local_rehearsal','deadline_at':None if replay else row['deadline_at'].isoformat(timespec='milliseconds').replace('+00:00','Z') if row['deadline_at'] else None,'remaining_ms':remaining}}

    @staticmethod
    def ranking(c,sid):
        players=c.execute(sa.select(s.participants.c.display_name,sa.func.sum(s.ledger.c.delta).label('points')).join(s.ledger,s.ledger.c.participant_id==s.participants.c.id).where(s.participants.c.session_id==sid).group_by(s.participants.c.id,s.participants.c.display_name).order_by(sa.desc('points'),s.participants.c.id).limit(3)).mappings()
        current=c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at.desc()).limit(1)).scalar_one_or_none()
        guesses=c.execute(sa.select(s.guesses,s.participants.c.display_name).join(s.participants,s.participants.c.id==s.guesses.c.participant_id).where(s.guesses.c.round_id==current,s.guesses.c.status=='scored',s.guesses.c.display_score.is_not(None),~s.guesses.c.normalized_word.like('retained:%')).order_by(s.guesses.c.admission_seq,s.guesses.c.id)).mappings()
        words={}
        for row in guesses:
            word=row['normalized_word']
            if word not in words: words[word]={'word':word,'first_display_name':row['display_name'],'participant_count':0,'display_score':float(row['display_score']),'first_seq':row['admission_seq'],'participants':set()}
            item=words[word]; item['participants'].add(row['participant_id']); item['participant_count']=len(item['participants']); item['display_score']=max(item['display_score'],float(row['display_score']))
        ranked=sorted(words.values(),key=lambda r:(-r['display_score'],r['first_seq'],r['word']))[:8]
        return {'words':[{k:v for k,v in r.items() if k not in {'first_seq','participants'}} for r in ranked],'players':[dict(display_name=r['display_name'],points=r['points'],rank=i+1) for i,r in enumerate(players)]}

    @staticmethod
    def reveal(c,row):
        if row['status'] not in {'REVEALED','INTERMISSION'}: return None
        winner=c.execute(sa.select(s.participants.c.display_name).where(s.participants.c.id==row['winner_id'])).scalar_one_or_none() if row['winner_id'] else None
        return {'round_id':row['id'],'answer':row['frozen_puzzle']['word'],'explanation':row['frozen_puzzle']['explanation'],'winner_display_name':winner,'reason':row['end_reason'],'next_start_at':row['next_start_at'].isoformat(timespec='milliseconds').replace('+00:00','Z') if row.get('next_start_at') else None}

    def transport(self,c,session):
        """Public transport reads reuse the caller's consistent transaction."""
        if session['mode']!='official':
            return {'state':'DISCONNECTED','reason_code':'LOCAL_REHEARSAL','recoverable':False,'last_confirmed_at':None}
        if self.official is None:
            return {'state':'DISCONNECTED','reason_code':'RELAY_NOT_CONNECTED','recoverable':False,'last_confirmed_at':None}
        return self.official.public_transport(c,session)

    def snapshot(self,c,sid,actor=None,lock=True):
        session=self.session(c,sid,actor,lock=lock)
        row=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid).order_by(s.rounds.c.created_at.desc()).limit(1)).mappings().first()
        feedbacks=c.execute(sa.select(s.guesses,s.participants.c.display_name).join(s.participants,s.participants.c.id==s.guesses.c.participant_id).where(s.guesses.c.session_id==sid).order_by(s.guesses.c.created_at.desc()).limit(20)).mappings()
        terminal=[]; seen=set()
        events=c.execute(sa.select(s.outbox.c.payload).where(s.outbox.c.session_id==sid,s.outbox.c.audience=='DISPLAY',s.outbox.c.type=='guess.feedback',sa.func.jsonb_array_length(s.outbox.c.payload['feedbacks'])>0).order_by(s.outbox.c.seq.desc()).limit(40)).scalars()
        for event in events:
            for feedback in event['feedbacks']:
                if feedback['feedback_id'] not in seen and feedback['status']!='queued' and len(terminal)<20 and not (feedback.get('word') or '').startswith('retained:'):
                    belongs=c.execute(sa.select(s.guesses.c.round_id).where(s.guesses.c.id==feedback['feedback_id'])).scalar_one_or_none()
                    if belongs is None: belongs=c.execute(sa.select(s.rejections.c.round_id).where(s.rejections.c.id==feedback['feedback_id'])).scalar_one_or_none()
                    if not row or belongs!=row['id']: continue
                    seen.add(feedback['feedback_id']); terminal.append({key:feedback[key] for key in ('feedback_id','word','display_name','status','display_score','solved','message')})
        result={'schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'mode':session['mode'],'run_purpose':session['run_purpose'],'as_of_seq':str(session['display_seq']),'server_time':now().isoformat(timespec='milliseconds').replace('+00:00','Z'),'round':self.public_round(row) if row else None,'transport':self.transport(c,session),'ranking':self.ranking(c,sid),'latest_feedbacks':terminal,'reveal':self.reveal(c,row) if row else None,'pending_effects':self.realtime.pending(c,sid) if hasattr(self,'realtime') else []}
        return result

    @staticmethod
    def validate_rules(rules):
        from pydantic import BaseModel,ConfigDict,Field,ValidationError
        class Rules(BaseModel):
            model_config=ConfigDict(extra='forbid',strict=True)
            duration_seconds:int=Field(ge=30,le=600)
            intermission_seconds:int=Field(ge=3,le=30)
            auto_next:bool
            category_id:str|None
            difficulty_min:int=Field(ge=1,le=5)
            difficulty_max:int=Field(ge=1,le=5)
            strict_round_code:bool
        try: result=Rules.model_validate(rules).model_dump()
        except ValidationError: raise DomainError('INPUT_INVALID','Invalid session rules',422) from None
        if result['difficulty_min']>result['difficulty_max']: raise DomainError('INPUT_INVALID','Invalid difficulty range',422)
        return result

    @staticmethod
    def audit(c,actor,action,resource_id=None,version=0,reason=None,request_id=None,details=None):
        c.execute(s.operation_audits.insert().values(id=uid(),actor_id=actor,action=action,resource_id=resource_id,resource_version=version,reason=reason,request_id=request_id,details=details or {}))

    def apply_window_result(self,c,session,roundrow,result):
        """Called only by the verified connector worker, under catalog/session locks."""
        selection.catalog_lock(c)
        session=self.session(c,session['id'])
        if session['mode']!='official' or self.official is None: raise DomainError('FORBIDDEN','Official result port unavailable',403)
        op=result.get('operation')
        if result.get('status')!='succeeded':
            if roundrow:
                from app.modules.rounds.worker import LocalRoundWorker
                LocalRoundWorker(self).cancel_jobs(c,roundrow,result.get('error_code') or 'OFFICIAL_OPERATION_FAILED')
                if not c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==roundrow['id'])).first(): self.settle(c,session,roundrow,'INTERRUPTED',None,None)
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='RECOVERY_REQUIRED',version=s.sessions.c.version+1))
            return
        if op=='recover' and result.get('strategy')=='end_session':
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ENDED',version=s.sessions.c.version+1))
            self.observe(c,'session_ended',session['id'],{'ended_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
            if self.runtime_recovery_completed: self.runtime_recovery_completed(c,session_id=session['id'])
            self.touch(c,session['id']); return
        if op=='prepare' or (op=='recover' and result.get('strategy')=='new_round'):
            if op=='prepare' and session['status']!='CREATED': return
            if op=='recover' and c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id==session['id'],s.rounds.c.status.in_(ACTIVE_ROUNDS))).first(): raise DomainError('ROUND_CLOSED','Recovery still has an active old round',409)
            self.official.require_current(c,session,None)
            if op=='prepare': self.official.mark_started(c,session)
            c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ACTIVE',version=s.sessions.c.version+1))
            session=self.session(c,session['id']); pointer=selection.pointer(c,session['id'])
            saved=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
            body={'session_id':session['id'],'expected_session_version':session['version']}
            if saved and saved['status']=='READY': body|={'selection_id':saved['id'],'expected_selection_version':pointer['revision']}
            else: body['selection']={'strategy':'random'}
            self.create_round(c,session['actor_id'],body,runtime_action='recover' if op=='recover' else None)
            if op=='recover' and self.runtime_recovery_completed: self.runtime_recovery_completed(c,session_id=session['id'])
            if op=='prepare': self.observe(c,'session_started',session['id'],{'started_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
            return
        if roundrow:
            _,roundrow=self.round(c,roundrow['id'],session['actor_id'])
        if op in {'open','resume'}:
            if not roundrow or roundrow['status'] not in {'PREPARING','PAUSED'}: return
            from datetime import datetime
            deadline=datetime.fromisoformat(result['deadline_at'].replace('Z','+00:00'))
            if deadline.tzinfo is None or type(result.get('remaining_ms')) is not int or result['remaining_ms']<0: raise DomainError('INPUT_INVALID','Verified relay clock required',422)
            c.execute(s.rounds.update().where(s.rounds.c.id==roundrow['id']).values(status='RUNNING',epoch=int(result['window_epoch']),deadline_at=deadline,remaining_ms=result['remaining_ms'],boot_id=self.boot_id,close_seq=None,drain_until_ns=None,version=s.rounds.c.version+1))
            updated=c.execute(sa.select(s.rounds).where(s.rounds.c.id==roundrow['id'])).mappings().one()
            self.event(c,session['id'],'round.started' if op=='open' else 'round.resumed',{'round':self.public_round(updated)})
            if op=='resume': self.observe(c,'round_resumed',session['id'],{'round_id':roundrow['id'],'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
            if op=='open':
                self.reselect(c,session,'ROUND_RUNNING',roundrow['id'],content={'round_id':roundrow['id']})
                self.observe(c,'round_started',session['id'],{'round_id':roundrow['id'],'started_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns(),'category_id':updated['frozen_puzzle']['category_id'],'difficulty':updated['frozen_puzzle']['difficulty']})
        elif op=='pause':
            if not roundrow: raise DomainError('INPUT_INVALID','Round required',422)
            c.execute(s.rounds.update().where(s.rounds.c.id==roundrow['id']).values(status='PAUSED',close_seq=int(result['close_seq']),remaining_ms=result['remaining_ms'],deadline_at=None,version=s.rounds.c.version+1))
            updated=c.execute(sa.select(s.rounds).where(s.rounds.c.id==roundrow['id'])).mappings().one()
            self.event(c,session['id'],'round.paused',{'round':self.public_round(updated)})
            self.observe(c,'round_paused',session['id'],{'round_id':roundrow['id'],'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        elif op in {'close','skip','void','end'}:
            if roundrow and not c.execute(sa.select(s.settlements.c.id).where(s.settlements.c.round_id==roundrow['id'])).first():
                if op in {'skip','void'}:
                    from app.modules.rounds.worker import LocalRoundWorker
                    LocalRoundWorker(self).cancel_jobs(c,roundrow,'ROUND_VOID')
                    for entry in c.execute(sa.select(s.ledger).where(s.ledger.c.round_id==roundrow['id'],s.ledger.c.reverses_id.is_(None))).mappings():
                        c.execute(s.ledger.insert().values(id=uid(),round_id=roundrow['id'],participant_id=entry['participant_id'],reason='VOID',reference_id=entry['id'],delta=-entry['delta'],reverses_id=entry['id']))
                    self.settle(c,session,roundrow,'VOID',None,None)
                else:
                    c.execute(s.rounds.update().where(s.rounds.c.id==roundrow['id']).values(status='DRAINING',close_seq=int(result['close_seq']),drain_until_ns=time.monotonic_ns()+2000000000,version=s.rounds.c.version+1))
                    self.observe(c,'round_paused',session['id'],{'round_id':roundrow['id'],'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
                    if op=='end': c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ENDING',version=s.sessions.c.version+1))
            elif op=='end':
                c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(status='ENDED',version=s.sessions.c.version+1))
                if session['status']!='ENDED': self.observe(c,'session_ended',session['id'],{'ended_at':now().isoformat(),'boot_id':self.boot_id,'mono_ns':time.monotonic_ns()})
        self.touch(c,session['id'])

    def admit_delivery(self,c,session,body):
        """Verified ordered relay input, isolated from every browser request model."""
        from app.modules.inbox.admission import OfficialAdmission
        from app.modules.inbox.normalization import parse_official_guess
        from datetime import datetime
        session=self.session(c,session['id'])
        if session['mode']!='official' or body.get('run_purpose')!=session['run_purpose'] or self.official is None: raise DomainError('FORBIDDEN','Official input scope mismatch',403)
        if body.get('source_mode') not in {'official','synthetic'} or type(body.get('provider_test')) is not bool or (body['source_mode']=='synthetic' and (not body['provider_test'] or session['run_purpose']!='test_live')): raise DomainError('FORBIDDEN','Official input purpose mismatch',403)
        if session['run_purpose']=='production' and (body['source_mode']!='official' or body['provider_test'] is not False): raise DomainError('FORBIDDEN','Production requires verified non-test official input',403)
        if body.get('type')!='comment': return {'status':'barrier','reason':None}
        payload=body['payload']; source_id=body['stream_id']+':'+payload['source_msg_id']
        old=c.execute(sa.select(s.guesses).where(s.guesses.c.session_id==session['id'],s.guesses.c.source_id==source_id)).mappings().first()
        if old: return {'status':old['status'],'feedback_id':old['id']}
        rejected=c.execute(sa.select(s.rejections).where(s.rejections.c.session_id==session['id'],s.rejections.c.source_id==source_id,s.rejections.c.source_mode==('official' if body['source_mode']=='official' else 'bridge'))).mappings().first()
        if rejected: return {'status':{'INPUT_INVALID':'format_invalid','RATE_LIMITED':'rate_limited','DUPLICATE_GUESS':'duplicate','ROUND_CLOSED':'round_closed'}.get(rejected['reason_code'],'system_failed'),'feedback_id':rejected['id'],'message':rejected['reason_code']}
        row=None
        try:
            with c.begin_nested():
                parsed=parse_official_guess(payload['content']['text'])
                if parsed is None:
                    self.observe(c,'source_message',session['id'],{'observation_id':'connector-source:'+body['stream_id']+':'+body['stream_seq'],'round_id':body.get('round_id'),'source_mode':'official' if body['source_mode']=='official' else 'bridge','provider_test':body['provider_test'],'received_at':body['relay_received_at'],'is_guess_attempt':False})
                    self.touch(c,session['id'])
                    return {'status':'ignored','reason':'NOT_GUESS'}
                code,word=parsed
                if not body.get('eligible') or not payload.get('participant_key'): raise DomainError('ROUND_CLOSED','Input was not admitted by relay',409)
                received=datetime.fromisoformat(body['relay_received_at'].replace('Z','+00:00'))
                if received.tzinfo is None: raise DomainError('INPUT_INVALID','Relay timestamp must have timezone',422)
                _,row=self.round(c,body['round_id'],session['actor_id'])
                admission=OfficialAdmission(payload['participant_key'],str(payload.get('display_name') or 'Viewer')[:80],int(body['stream_seq']),int(body['window_epoch']),received,'official' if body['source_mode']=='official' else 'bridge',body['provider_test'])
                request={'session_id':session['id'],'round_id':row['id'],'short_code':code,'word':word,'display_name':admission.display_name}
                return self.guess(c,session['actor_id'],request,source_id,trusted_official=admission)
        except DomainError as error:
            status={'INPUT_INVALID':'format_invalid','RATE_LIMITED':'rate_limited','DUPLICATE_GUESS':'duplicate','ROUND_CLOSED':'round_closed'}.get(error.code,'system_failed')
            previous=c.execute(sa.select(s.rejections.c.id).where(s.rejections.c.session_id==session['id'],s.rejections.c.source_id==source_id,s.rejections.c.source_mode==('official' if body['source_mode']=='official' else 'bridge'))).scalar_one_or_none()
            if previous: return {'status':status,'feedback_id':previous}
            rid=uid(); round_id=body.get('round_id')
            if not c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.id==round_id,s.rounds.c.session_id==session['id'])).first(): round_id=None
            c.execute(s.rejections.insert().values(id=rid,session_id=session['id'],round_id=round_id,source_id=source_id,reason_code=error.code,source_mode='official' if body['source_mode']=='official' else 'bridge',provider_test=body['provider_test']))
            self.trace(c,rid,'admission','rejected',error.code)
            feedback={'feedback_id':rid,'word':None,'display_name':str(payload.get('display_name') or 'Viewer')[:80],'status':status,'display_score':None,'solved':False,'message':error.code}
            self.event(c,session['id'],'guess.feedback',{'feedbacks':[feedback],'effects':[]})
            self.observe(c,'guess_rejected',session['id'],{'observation_id':rid,'round_id':round_id,'participant_id':None,'source_mode':'official' if body['source_mode']=='official' else 'bridge','provider_test':body['provider_test'],'received_at':body['relay_received_at'],'outcome':'system_failed' if status=='system_failed' else 'business_rejected','is_guess_attempt':True})
            self.touch(c,session['id']); return feedback

    def pending_commands(self,c,session_id,round_id=None):
        query=sa.select(s.commands.c.response).where(s.commands.c.session_id==session_id,s.commands.c.response['status'].astext.in_(['pending','failed']))
        if round_id: query=query.where(s.commands.c.resource_id==round_id)
        values=list(c.execute(query).scalars())
        if self.official is not None and callable(getattr(self.official,'pending_commands',None)):
            session=self.session(c,session_id,lock=False)
            values+=self.official.pending_commands(c,session,round_id=round_id)
        return list({v['command_id']:v for v in values}.values())

    def synchronize_model_catalog(self):
        """Persist model identity changes and selection invalidation before any consume."""
        observer=getattr(self.scorer,'readiness',None)
        try: model=observer() if callable(observer) else {}
        except Exception: return
        manifest=model.get('manifest_id') if model.get('ready') is True else None
        if not manifest: return  # Temporary health loss is handled as a fault, not a new manifest.
        sha=getattr(self.scorer,'manifest_sha256',None)
        with self.db.engine.begin() as c:
            catalog=selection.catalog_lock(c)
            changed=catalog['model_manifest_id'] not in {None,manifest} or (catalog['model_manifest_sha256'] is not None and catalog['model_manifest_sha256']!=sha)
            if catalog['model_manifest_id']==manifest and catalog['model_manifest_sha256']==sha: return
            revision=catalog['revision']+int(changed)
            c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(model_manifest_id=manifest,model_manifest_sha256=sha,revision=revision))
            ids=list(c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status!='ENDED').order_by(s.sessions.c.id).with_for_update()).scalars())
            for sid in ids:
                session=self.session(c,sid); pointer=selection.pointer(c,sid)
                old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
                mismatch=bool(old and old['status']=='READY' and old['manifest_id']!=manifest)
                if old and old['status']=='READY' and sha:
                    puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==old['puzzle_version_id'])).mappings().first()
                    mismatch=mismatch or not puzzle or puzzle['scoring_artifact'].get('manifest_sha256')!=sha
                if mismatch or (changed and old and old['status']=='EMPTY'):
                    self.reselect(c,session,'MANIFEST_CHANGED',f'{revision}:{manifest}:{sha}',content={'manifest_id':manifest,'manifest_sha256':sha,'catalog_revision':revision})

    def bundle_permit_issuer(self):
        from app.modules.wordbank.bundle import BundlePermit,digest
        from copy import deepcopy
        def issue(identity,actor_id,command_id):
            return BundlePermit(self._bundle_token,deepcopy(identity),digest(identity),actor_id,command_id)
        return issue

    def export_verified_bundle(self,c,model_identity=None):
        from app.modules.wordbank.bundle import export
        return export(self,c,model_identity)

    def validate_verified_bundle(self,c,identity):
        from app.modules.wordbank.bundle import validate
        return validate(self,c,identity)

    def activate_verified_bundle(self,c,permit):
        from app.modules.wordbank.bundle import activate
        return activate(self,c,permit)

    @staticmethod
    def score_input_versions(row):
        from app.modules.wordbank.bundle import digest
        frozen=row['frozen_puzzle']
        return {'round_id':row['id'],'epoch':row['epoch'],'manifest_id':frozen['manifest_id'],'manifest_sha256':frozen.get('manifest_sha256'),'normalization':frozen.get('normalization'),'calibration_id':frozen['calibration_id'],'puzzle_version_id':row['puzzle_version_id'],'target_vector_sha256':digest(frozen.get('target_vector',[])),'calibration_sha256':digest(frozen.get('calibration',{}))}

    def rehearsal_input_issuer(self):
        from app.modules.inbox.admission import RehearsalAdmission
        def issue(participant_ref,display_name):
            if not isinstance(participant_ref,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',participant_ref) or not isinstance(display_name,str) or not display_name.strip() or len(display_name)>80: raise DomainError('INPUT_INVALID','Opaque local participant reference required',422)
            return RehearsalAdmission(self._rehearsal_token,participant_ref,('合成演练 · '+display_name)[:80])
        return issue

    def admit_rehearsal_input(self,actor,key,body,permit):
        from app.modules.inbox.admission import RehearsalAdmission
        if not isinstance(permit,RehearsalAdmission) or permit._token is not self._rehearsal_token: raise DomainError('FORBIDDEN','Trusted local rehearsal capability required',403)
        if not isinstance(body,dict) or set(body)!={'session_id','round_id','short_code','word'}: raise DomainError('INPUT_INVALID','Fixed rehearsal body required',422)
        data=body|{'display_name':permit.display_name,'participant_ref':permit.participant_ref}
        return self._submit_guess(actor,key,data,'/internal/rehearsal-input',admission=permit)

    def bundle_resource_permit_issuer(self):
        from app.modules.wordbank.resources import BundleResourcePermit,parse
        from app.modules.wordbank.bundle import digest
        from copy import deepcopy
        def issue(identity,resource_bytes,expected_file_sha256,actor_id,command_id):
            content=parse(resource_bytes,expected_file_sha256)
            return BundleResourcePermit(self._bundle_token,deepcopy(identity),digest(identity),content,digest(content),expected_file_sha256,actor_id,command_id)
        return issue

    def export_verified_bundle_resources(self,c,identity):
        from app.modules.wordbank.resources import export
        return export(self,c,identity)

    def describe_bundle_resources(self,content):
        from app.modules.wordbank.resources import describe
        return describe(content)

    def import_verified_bundle_resources(self,c,permit):
        from app.modules.wordbank.resources import import_resources
        return import_resources(self,c,permit)
