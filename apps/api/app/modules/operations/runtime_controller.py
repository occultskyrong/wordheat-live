"""Persistent portable runtime intents. Native authority is never a Web field."""
import hashlib
import json
import secrets
import time
import os
from dataclasses import dataclass
from datetime import datetime,timezone
from sqlalchemy.dialects.postgresql import insert
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.operations import runtime_models as m

OPERATIONS=frozenset({'backup_local','verify_local_backup','restore_new_instance','upgrade_app','switch_model'})
ACTIVE_SESSIONS=('ACTIVE','ENDING','RECOVERY_REQUIRED')
ACTIVE_ROUNDS=('PREPARING','RUNNING','PAUSED','DRAINING','FAULTED')
LOCK=835297811

def now(): return datetime.now(timezone.utc)
def brief(code,message='Runtime operation requires attention',retryable=False):return dict(code=code,message=message,retryable=retryable)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

@dataclass(frozen=True)
class PrivateRuntimeBoot:
    boot_id:str
    installation_id:str
    model_manifest_id:str
    model_manifest_sha256:str
    api_pid:int
    parent_pid:int
    restart:dict|None

    @classmethod
    def from_port(cls,port):
        provider=getattr(port,'verified_boot_context',None)
        if not callable(provider):return None
        value=provider()
        if not isinstance(value,dict) or value.get('api_pid')!=os.getpid() or value.get('parent_pid')!=os.getppid() or not isinstance(value.get('boot_id'),str):raise DomainError('RUNTIME_BOOT_IDENTITY_INVALID','Private parent bootstrap identity required',403)
        return cls(**{k:value.get(k) for k in cls.__dataclass_fields__})

class RuntimeController:
    def __init__(self,core,*,boot_id=None,stop_port=None,native_port=None,platform_projection=None,clock=time.monotonic_ns,bundle_issuer=None,resource_issuer=None,control_importer=None,boot_context=None,pairing_bridge=None):
        self.core=core;self.db=core.db;self.boot_id=boot_id or core.boot_id
        self.installation_id=self.db.settings.instance_id;self.stop_port=stop_port;self.native_port=native_port
        self.platform_projection=platform_projection;self.clock=clock;self.bundle_issuer=bundle_issuer;self.resource_issuer=resource_issuer;self.control_importer=control_importer
        self._confirmation_key=object();self.last_error=None
        self.boot_context=boot_context;self.pairing_bridge=pairing_bridge

    @staticmethod
    def authorize(c,actor_id):
        actor=c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id)).mappings().first()
        if not actor or actor['disabled'] or 'maintenance.request' not in actor['permissions']:
            raise DomainError('FORBIDDEN','Current maintenance permission required',403)
        return actor

    def initialize(self):
        with self.db.engine.begin() as c:
            c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':LOCK})
            row=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().first()
            if row and row['boot_id']==self.boot_id:return
            unfinished=c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status.in_(ACTIVE_SESSIONS)).limit(1)).first()
            pending=c.execute(sa.select(m.commands).where(m.commands.c.installation_id==self.installation_id,m.commands.c.status=='pending').limit(1)).mappings().first()
            context=self.boot_context
            restart=(context.restart if isinstance(context,PrivateRuntimeBoot) else None) or {}
            planned=(pending['plan'].get('restart_expected') if pending else None) or {}
            allowed_restart=bool(pending and pending['kind']=='request_maintenance' and pending['operation'] in {'switch_model','upgrade_app'} and pending['plan'].get('confirmed') is True and not unfinished and isinstance(context,PrivateRuntimeBoot) and context.boot_id==self.boot_id and context.installation_id==self.installation_id and planned and restart==planned and planned.get('command_id')==pending['id'] and planned.get('model_manifest_id')==context.model_manifest_id and planned.get('model_manifest_sha256')==context.model_manifest_sha256)
            c.execute(m.native_requests.update().where(m.native_requests.c.installation_id==self.installation_id,m.native_requests.c.status=='pending',m.native_requests.c.boot_id!=self.boot_id).values(status='failed',stage='RECOVERY_REQUIRED',error=brief('RUNTIME_RECOVERY_REQUIRED','Native confirmation belongs to a previous boot'),updated_at=now()))
            if allowed_restart:
                resumed=pending['plan']|{'recovering':True,'restart_resumed':True}
                c.execute(m.commands.update().where(m.commands.c.id==pending['id']).values(boot_id=self.boot_id,stage='WARMING_NEW_BUNDLE',deadline_ns=self.clock()+120_000_000_000,plan=resumed,error=None,updated_at=now()))
                c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(boot_id=self.boot_id,state='MAINTENANCE',version=row['version']+1,active_command_id=pending['id'],updated_at=now()))
                return
            state='RECOVERY_REQUIRED' if unfinished or pending or row and row['state'] in {'MAINTENANCE','STOPPING','RECOVERY_REQUIRED'} else 'CHECKING'
            if row:
                c.execute(m.commands.update().where(m.commands.c.installation_id==self.installation_id,m.commands.c.status=='pending').values(status='failed',stage='RECOVERY_REQUIRED',error=brief('RUNTIME_RECOVERY_REQUIRED','Boot changed; explicit native recovery required'),updated_at=now()))
                c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(boot_id=self.boot_id,state=state,version=row['version']+1,active_command_id=None,last_exit=brief('BOOT_CHANGED','Previous process generation ended'),updated_at=now()))
            else:c.execute(m.instances.insert().values(installation_id=self.installation_id,boot_id=self.boot_id,state=state,version=1,updated_at=now()))

    @staticmethod
    def validate(body):
        if not isinstance(body,dict) or set(body)!={'command','expected_version','args'} or body['command'] not in {'stop','request_maintenance'}:
            raise DomainError('INPUT_INVALID','Fixed runtime command required',422)
        version=body['expected_version'];args=body['args']
        if isinstance(version,bool) or not isinstance(version,int) or version<0 or not isinstance(args,dict) or set(args)-{'reason','operation'}:
            raise DomainError('INPUT_INVALID','Invalid runtime command fields',422)
        if not isinstance(args.get('reason'),str) or not 1<=len(args['reason'].strip())<=200:
            raise DomainError('INPUT_INVALID','A reason of 1..200 characters is required',422)
        if body['command']=='stop' and 'operation' in args or body['command']=='request_maintenance' and args.get('operation') not in OPERATIONS:
            raise DomainError('INPUT_INVALID','Fixed maintenance operation required',422)
        return body

    def command(self,actor_id,key,body):
        self.validate(body)
        if not isinstance(key,str) or not 1<=len(key)<=128:raise DomainError('INPUT_INVALID','Idempotency-Key required',422)
        hashed=digest(body)
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c);self.authorize(c,actor_id)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            old=c.execute(sa.select(m.commands).where(m.commands.c.installation_id==self.installation_id,m.commands.c.actor_id==actor_id,m.commands.c.idempotency_key==key)).mappings().first()
            if old:
                if old['request_hash']!=hashed:raise DomainError('IDEMPOTENCY_KEY_REUSED','Key binds a different runtime intent',409)
                record=dict(old)
            else:
                if instance['version']!=body['expected_version']:raise DomainError('RUNTIME_VERSION_CONFLICT','Runtime state changed',409,details={'version':instance['version']})
                pending=c.execute(sa.select(m.commands).where(m.commands.c.id==instance['active_command_id'],m.commands.c.status=='pending')).mappings().first() if instance['active_command_id'] else None
                if pending:
                    if body['command']=='stop' and pending['kind']=='request_maintenance' and pending['stage']=='AWAITING_NATIVE_CONFIRMATION':
                        c.execute(m.commands.update().where(m.commands.c.id==pending['id']).values(status='cancelled',stage='SUPERSEDED_BY_STOP',resource_version=instance['version']+1,updated_at=now()))
                        instance=dict(instance,version=instance['version']+1)
                    else:raise DomainError('RUNTIME_COMMAND_PENDING','Another runtime intent is pending',409)
                cid=secrets.token_urlsafe(24);version=instance['version']+1;stamp=now()
                needs_arm=body['command']=='stop' and callable(getattr(self.stop_port,'arm_stop',None))
                record=dict(id=cid,installation_id=self.installation_id,actor_id=actor_id,idempotency_key=key,request_hash=hashed,boot_id=self.boot_id,kind=body['command'],operation=body['args'].get('operation'),reason=body['args']['reason'],status='pending',stage='DRAINING' if body['command']=='stop' else 'AWAITING_NATIVE_CONFIRMATION',resource_version=version,deadline_ns=self.clock()+15_000_000_000,plan={'watchdog_armed':not needs_arm,'request_state':instance['state']},created_at=stamp,updated_at=stamp)
                c.execute(m.commands.insert().values(**record));c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(version=version,state='STOPPING' if body['command']=='stop' else instance['state'],active_command_id=cid,updated_at=stamp))
        # The intent is durable BEFORE the independent process owner arms its monotonic deadline.
        if record['kind']=='stop' and record['status']=='pending' and not record['plan'].get('watchdog_armed'):
            try:
                evidence=self.stop_port.arm_stop(record['id'],self.boot_id,record['deadline_ns'])
                if not isinstance(evidence,dict) or evidence.get('authorized') is not True:raise DomainError('RUNTIME_STOP_UNAUTHORIZED','Watchdog arm authority not proven',403)
                with self.db.engine.begin() as c:
                    c.execute(sa.text("SET LOCAL lock_timeout='1000ms'"));c.execute(sa.text("SET LOCAL statement_timeout='1000ms'"))
                    c.execute(m.commands.update().where(m.commands.c.id==record['id'],m.commands.c.status=='pending').values(plan=record['plan']|{'watchdog_armed':True},stage='DRAINING',updated_at=now()))
            except Exception as error:
                self.last_error='RUNTIME_WATCHDOG_ARM_UNCONFIRMED'
                if isinstance(error,DomainError) and error.code in {'RUNTIME_STOP_UNAVAILABLE','WINDOWS_NATIVE_REQUIRED'}:
                    self.update(record['id'],status='failed',stage='FAILED',error=brief(error.code,error.message),state='FAULTED')
                    with self.db.engine.connect() as c:record=dict(c.execute(sa.select(m.commands).where(m.commands.c.id==record['id'])).mappings().one())
                else:
                    # An ACK may have been lost after the parent armed. Preserve the intent;
                    # its non-extendable parent deadline remains authoritative.
                    with self.db.engine.begin() as c:c.execute(m.commands.update().where(m.commands.c.id==record['id'],m.commands.c.status=='pending').values(stage='WATCHDOG_ARM_UNCONFIRMED',updated_at=now()))
                    record['stage']='WATCHDOG_ARM_UNCONFIRMED'
        return self.dto(record)

    @staticmethod
    def dto(row):
        return dict(command_id=row['id'],status=row['status'],resource_version=row['resource_version'],state=row['stage'],error=row.get('error'))

    def lookup_command(self,c,command_id,actor_id,permissions):
        row=c.execute(sa.select(m.commands).where(m.commands.c.id==command_id)).mappings().first()
        if not row:
            row=c.execute(sa.select(m.native_requests).where(m.native_requests.c.id==command_id)).mappings().first()
            if not row:return None
        self.authorize(c,actor_id)
        if row['actor_id']!=actor_id or row['installation_id']!=self.installation_id:raise DomainError('FORBIDDEN','Runtime command ownership required',403)
        return self.dto(row)

    def update(self,cid,*,status=None,stage=None,error=None,result=None,state=None):
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            command=c.execute(sa.select(m.commands).where(m.commands.c.id==cid).with_for_update()).mappings().one()
            if command['status']!='pending':return
            values=dict(updated_at=now(),resource_version=instance['version']+1)
            if status:values['status']=status
            if stage:values['stage']=stage
            if error is not None:values['error']=error
            if result is not None:values['result']=result
            c.execute(m.commands.update().where(m.commands.c.id==cid).values(**values))
            patch=dict(version=instance['version']+1,updated_at=now())
            if state:patch['state']=state
            if status and status!='pending':patch['active_command_id']=None
            c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(**patch))

    def drain_sessions(self,command):
        with self.db.engine.connect() as c:
            sessions=list(c.execute(sa.select(s.sessions.c.id,s.sessions.c.status).where(s.sessions.c.status.in_(ACTIVE_SESSIONS)).order_by(s.sessions.c.id)).mappings())
        for record in sessions:
            if record['status']=='ENDING':continue
            with self.db.engine.begin() as c:
                # The same global order as ordinary core commands; no runtime row lock is held here.
                from app.modules.wordbank.selection import catalog_lock
                catalog_lock(c)
                session=self.core.session(c,record['id'])
                if session['status'] not in ACTIVE_SESSIONS:continue
                self.core.session_command(c,session['actor_id'],session['id'],{'command':'end','expected_version':session['version'],'args':{'reason':command['reason']}})
        with self.db.engine.connect() as c:
            remaining=bool(c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status.in_(ACTIVE_SESSIONS)).limit(1)).first())
            platform=self.platform_projection(c) if callable(self.platform_projection) else None
            official=bool(c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.mode=='official').limit(1)).first())
            if official and platform is None:raise DomainError('DEPENDENCY_UNAVAILABLE','Official stop observation is not bound',503,details={'reason':'PLATFORM_STOP_UNKNOWN'})
            return not remaining and not (platform or {}).get('platform_stop_pending',False)

    def tick(self):
        with self.db.engine.connect() as c:
            row=c.execute(sa.select(m.commands).where(m.commands.c.installation_id==self.installation_id,m.commands.c.status=='pending').order_by(m.commands.c.created_at).limit(1)).mappings().first()
        if not row:return
        row=dict(row)
        try:
            with self.db.engine.connect() as c:self.authorize(c,row['actor_id'])
            if row['boot_id']!=self.boot_id:raise DomainError('RUNTIME_RECOVERY_REQUIRED','Boot changed; explicit recovery required',409)
            if row['kind']=='request_maintenance' and row['stage']=='RESTART_REQUIRED':
                # The old API is retiring. Only a new private bootstrap matching
                # this exact confirmed plan may resume it; never retry on the old proxy.
                return
            if row['kind']=='request_maintenance' and row['stage']=='AWAITING_NATIVE_CONFIRMATION':
                if self.native_port is None:raise DomainError('NATIVE_CONTROL_UNAVAILABLE','A private native confirmation port is not configured',503)
                return
            if row['stage']=='WARMING_NEW_BUNDLE':
                expected=row['plan']['restart_expected']
                try:ready=self.core.scorer.readiness()
                except Exception:ready={}
                if ready.get('ready') is not True:
                    if self.clock()>=row['deadline_ns']:raise DomainError('MODEL_WARMUP_TIMEOUT','New scorer did not produce verified readiness within its startup budget',503)
                    return
                if ready.get('manifest_id')!=expected['model_manifest_id'] or getattr(self.core.scorer,'manifest_sha256',None)!=expected['model_manifest_sha256']:raise DomainError('CANDIDATE_MISMATCH','Actual new scorer identity differs from confirmed bundle',409)
                self.update(row['id'],stage='CONFIRMED',state='MAINTENANCE');return
            if row['stage']=='DRAINING':
                if row['kind']=='stop' and not row['plan'].get('watchdog_armed',True):return
                if not self.drain_sessions(row):
                    if self.clock()>=row['deadline_ns']:raise DomainError('SHUTDOWN_DRAIN_TIMEOUT','Business or platform stop remains unconfirmed',503,details={'reason':'STOP_PENDING'})
                    return
                if row['kind']=='stop':
                    if self.stop_port is None:raise DomainError('RUNTIME_STOP_UNAVAILABLE','No owned process stop port is connected',503)
                    remaining=max(0,(row['deadline_ns']-self.clock())/1e9)
                    if remaining<=0:raise DomainError('SHUTDOWN_DRAIN_TIMEOUT','Runtime drain budget expired',503)
                    evidence=self.stop_port.prepare_stop(row['id'],self.boot_id,remaining)
                    if not isinstance(evidence,dict) or evidence.get('authorized') is not True:raise DomainError('RUNTIME_STOP_UNAUTHORIZED','Stop authority was not proven',403)
                    self.update(row['id'],status='applied',stage='STOPPED',result={'owned_processes_stopped':True},state='STOPPED')
                    self.stop_port.finish_exit();return
                if self.native_port is None:raise DomainError('NATIVE_CONTROL_UNAVAILABLE','A private native confirmation port is not configured',503)
                self.update(row['id'],stage='CONFIRMED',state='MAINTENANCE')
            elif row['kind']=='request_maintenance' and row['plan'].get('confirmed') is True:self.execute_maintenance(row)
        except DomainError as error:
            self.fail_backup(row['id'],brief(error.code,error.message,error.retryable))
            force=row['kind']=='stop' and error.code=='SHUTDOWN_DRAIN_TIMEOUT' and callable(getattr(self.stop_port,'force_stop',None))
            self.update(row['id'],status='failed',stage='STOPPED' if force else 'FAILED',error=brief(error.code,error.message,error.retryable),result={'model_observation':error.details} if error.code=='MODEL_RESTART_REQUIRED' else None,state='STOPPED' if force else 'FAULTED' if row['kind']=='stop' else 'MAINTENANCE')
            if force:self.stop_port.force_stop(row['id'],self.boot_id,error.code)
        except Exception:
            self.fail_backup(row['id'],brief('RUNTIME_OPERATION_FAILED'))
            self.update(row['id'],status='failed',stage='FAILED',error=brief('RUNTIME_OPERATION_FAILED'),state='FAULTED' if row['kind']=='stop' else 'MAINTENANCE')

    def status(self,c,readiness,extended=False):
        instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id)).mappings().one()
        active=c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status.in_(ACTIVE_SESSIONS)).order_by(s.sessions.c.created_at.desc()).limit(1)).scalar_one_or_none()
        state=instance['state']
        if state not in {'STOPPED','STOPPING','MAINTENANCE','FAULTED','RECOVERY_REQUIRED'}:
            checks=readiness.get('checks',{})
            basic=all(checks.get(k,{}).get('status')=='pass' for k in ('db','model','wordbank'))
            state='READY_OFFICIAL' if basic and (readiness.get('can_test_live') or readiness.get('can_go_live')) else 'READY_REHEARSAL' if basic else 'WARMING' if checks.get('db',{}).get('status')=='pass' else 'CHECKING'
        readiness=dict(readiness)
        if state in {'STOPPED','STOPPING','MAINTENANCE','FAULTED','RECOVERY_REQUIRED'}:
            readiness.update(can_rehearse=False,can_test_live=False,can_go_live=False)
        latest=c.execute(sa.select(m.commands).where(m.commands.c.installation_id==self.installation_id,m.commands.c.kind=='request_maintenance').order_by(m.commands.c.created_at.desc()).limit(1)).mappings().first()
        blank=dict(backup_id=None,status='none',last_completed_at=None,last_verified_at=None,app_version=None,schema_version=None,model_manifest_id=None,location_hint=None,last_error=None)
        backup=c.execute(sa.select(m.backups.c.payload).where(m.backups.c.installation_id==self.installation_id).order_by(m.backups.c.updated_at.desc()).limit(1)).scalar_one_or_none()
        maintenance=dict(command_id=latest['id'] if latest else None,operation=latest['operation'] if latest else None,stage=latest['stage'] if latest else 'idle',requires_native_confirmation=not latest or latest['stage'] in {'DRAINING','AWAITING_NATIVE_CONFIRMATION','FAILED'},last_error=latest['error'] if latest else None)
        return dict(version=instance['version'],launcher_state=state,active_session_id=active,installation_id=self.installation_id,app_version='0.1.0',model_manifest_id=readiness.get('context',{}).get('model_manifest_id'),schema_version=1,readiness=readiness if extended else self.core.readiness.c1(readiness),backup_status={'local':backup or dict(blank),'relay':dict(blank)},maintenance_status=maintenance)

    def require_running(self,c,action='start',session_id=None):
        state=c.execute(sa.select(m.instances.c.state).where(m.instances.c.installation_id==self.installation_id).with_for_update(read=True)).scalar_one()
        if state=='RECOVERY_REQUIRED' and session_id:
            status=c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==session_id)).scalar_one_or_none()
            if action=='recover' and status=='RECOVERY_REQUIRED':return
            permit=c.execute(sa.select(m.scopes.c.session_id).where(m.scopes.c.installation_id==self.installation_id,m.scopes.c.boot_id==self.boot_id,m.scopes.c.session_id==session_id)).first()
            if action=='next_round' and permit and status=='ACTIVE':return
        if state in {'STOPPING','STOPPED','MAINTENANCE','FAULTED','RECOVERY_REQUIRED'}:
            raise DomainError('RUNTIME_NOT_READY','Runtime is stopping, under maintenance, or requires recovery',409,details={'state':state})

    def recovery_completed(self,c,*,session_id):
        # Caller owns catalog -> runtime shared -> session locks in this transaction.
        row=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
        if row['state']!='RECOVERY_REQUIRED':return
        status=c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==session_id)).scalar_one()
        if status=='RECOVERY_REQUIRED':return
        c.execute(insert(m.scopes).values(installation_id=self.installation_id,boot_id=self.boot_id,session_id=session_id,recovered_at=now()).on_conflict_do_nothing())
        other=c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status=='RECOVERY_REQUIRED',s.sessions.c.id!=session_id).limit(1)).first()
        pending=c.execute(sa.select(m.commands.c.id).where(m.commands.c.installation_id==self.installation_id,m.commands.c.status=='pending').limit(1)).first()
        state='RECOVERY_REQUIRED' if other or pending else 'CHECKING'
        c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(state=state,version=row['version']+1,updated_at=now()))

    def native_control_port(self):
        """Give only to the restricted native bridge. This object is never an HTTP DTO."""
        controller=self;capability=self._confirmation_key
        class Port:
            def prepare(self,command_id,artifact_id=None,**options):return controller._prepare_native(capability,command_id,artifact_id,**options)
            def confirm(self,command_id,fingerprint,nonce,installation_id):return controller._confirm_native(capability,command_id,fingerprint,nonce,installation_id)
            def cancel(self,command_id):return controller._cancel_native(capability,command_id)
            def recover(self,command_id):return controller._recover_native(capability,command_id)
            def prepare_key_import(self,actor_id,source_id):return controller._prepare_key_import(capability,actor_id,source_id)
            def confirm_key_import(self,command_id,fingerprint,nonce,installation_id):return controller._confirm_key_import(capability,command_id,fingerprint,nonce,installation_id)
        return Port()

    def current(self):
        with self.db.engine.connect() as c:
            schema=c.execute(sa.select(s.instance.c.schema_version).where(s.instance.c.instance_id==self.installation_id)).scalar_one()
            bytes_required=c.execute(sa.text("SELECT coalesce(sum(pg_total_relation_size(quote_ident(schemaname)||'.'||quote_ident(tablename))),0) FROM pg_tables WHERE schemaname=current_schema()")).scalar_one()
        scorer=getattr(self.core,'scorer',None)
        try:model=scorer.readiness()
        except Exception:model={}
        return dict(installation_id=self.installation_id,app_version=getattr(self.core,'app_version','0.1.0'),schema_version=schema,pg_major=16,model_manifest_id=model.get('manifest_id'),model_manifest_sha256=getattr(scorer,'manifest_sha256',None),estimated_backup_bytes=int(bytes_required))

    def private_command(self,c,capability,cid):
        if capability is not self._confirmation_key:raise DomainError('FORBIDDEN','Native confirmation capability required',403)
        row=c.execute(sa.select(m.commands).where(m.commands.c.id==cid,m.commands.c.installation_id==self.installation_id)).mappings().first()
        if not row or row['kind']!='request_maintenance':raise DomainError('RESOURCE_NOT_FOUND','Maintenance command not found',404)
        self.authorize(c,row['actor_id']);return dict(row)

    def _prepare_native(self,capability,cid,artifact_id=None,*,target_id=None,allow_data_loss=False):
        if self.native_port is None:raise DomainError('NATIVE_CONTROL_UNAVAILABLE','Private native port is not configured',503)
        with self.db.engine.connect() as c:
            row=self.private_command(c,capability,cid)
            if row['status']!='pending' or row['stage']!='AWAITING_NATIVE_CONFIRMATION':raise DomainError('MAINTENANCE_STAGE_CONFLICT','Command is not awaiting local confirmation',409)
        if row['plan'].get('payload') and row['plan'].get('recovering'):
            if artifact_id is not None and artifact_id!=row['plan']['payload'].get('artifact_id'):raise DomainError('MAINTENANCE_RECOVERY_REQUIRED','Recovery requires the original artifact',409)
            plan=self.native_port.restore_plan(row['plan']['payload'],row['plan']['fingerprint'])
        else:plan=self.native_port.prepare(row['operation'],artifact_id,cid,self.current(),target_id=target_id,allow_data_loss=allow_data_loss)
        nonce=secrets.token_urlsafe(32)
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            c.execute(sa.select(m.instances.c.version).where(m.instances.c.installation_id==self.installation_id).with_for_update()).scalar_one()
            current=self.private_command(c,capability,cid)
            if current['status']!='pending' or current['stage']!='AWAITING_NATIVE_CONFIRMATION' or current['boot_id']!=self.boot_id:raise DomainError('MAINTENANCE_STAGE_CONFLICT','Maintenance state changed during preparation',409)
            c.execute(m.commands.update().where(m.commands.c.id==cid).values(plan={'payload':plan.payload,'fingerprint':plan.fingerprint,'nonce_hash':hashlib.sha256(nonce.encode()).hexdigest(),'confirmed':False,'recovering':bool(row['plan'].get('recovering')),'request_state':row['plan'].get('request_state','CHECKING')},updated_at=now()))
        artifact=plan.payload.get('artifact') or {};bundle=artifact.get('bundle') or {};backup=artifact.get('database_backup') or {}
        return {'command_id':cid,'fingerprint':plan.fingerprint,'confirmation_nonce':nonce,'installation_id':self.installation_id,'operation':row['operation'],'artifact_sha256':plan.payload['manifest_sha256'],'target':plan.payload['target'],'canonical_paths':plan.payload['artifact_paths'],'allow_data_loss':plan.payload['allow_data_loss'],'model_manifest_id':bundle.get('model_manifest_id'),'model_manifest_sha256':bundle.get('model_manifest_sha256'),'app_version':artifact.get('app_version'),'schema_compatibility':artifact.get('compatibility'),'backup_created_at':backup.get('created_at'),'rpo_boundary':backup.get('snapshot_captured_at',backup.get('created_at')),'artifact_trust':'untrusted_backup_data' if artifact.get('format')=='wordheat-local-backup/1' else 'signed_release'}

    def _confirm_native(self,capability,cid,fingerprint,nonce,installation_id):
        import hmac
        with self.db.engine.connect() as c:row=self.private_command(c,capability,cid)
        stored=row['plan']
        if row['status']!='pending' or row['stage']!='AWAITING_NATIVE_CONFIRMATION' or row['boot_id']!=self.boot_id or installation_id!=self.installation_id or stored.get('fingerprint')!=fingerprint or not isinstance(nonce,str) or not hmac.compare_digest(stored.get('nonce_hash',''),hashlib.sha256(nonce.encode()).hexdigest()):
            raise DomainError('NATIVE_CONFIRMATION_INVALID','Confirmation does not match this command, boot, instance and artifact',403)
        self.native_port.restore_plan(stored['payload'],fingerprint)
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            fresh=self.private_command(c,capability,cid)
            if fresh['status']!='pending' or fresh['stage']!='AWAITING_NATIVE_CONFIRMATION' or fresh['plan']!=stored:raise DomainError('MAINTENANCE_STAGE_CONFLICT','Confirmation already used or replaced',409)
            c.execute(m.commands.update().where(m.commands.c.id==cid).values(stage='DRAINING',deadline_ns=self.clock()+15_000_000_000,plan=stored|{'confirmed':True,'nonce_hash':None,'confirmation_id':secrets.token_urlsafe(24)},resource_version=instance['version']+1,updated_at=now()))
            c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(version=instance['version']+1,state='STOPPING',updated_at=now()))
        return {'command_id':cid,'confirmed':True}

    def _cancel_native(self,capability,cid):
        with self.db.engine.connect() as c:
            row=self.private_command(c,capability,cid)
            if row['status']!='pending':raise DomainError('MAINTENANCE_STAGE_CONFLICT','Only a pending maintenance command may be cancelled',409)
            progressed=c.execute(sa.select(m.steps.c.stage).where(m.steps.c.command_id==cid).limit(1)).first()
        self.update(cid,status='cancelled',stage='CANCELLED',error=brief('MAINTENANCE_PARTIAL_ACTION','Reconcile completed native actions before restarting') if progressed else None,state='MAINTENANCE' if progressed else row['plan'].get('request_state','CHECKING'))
        return {'command_id':cid,'status':'cancelled'}

    def _recover_native(self,capability,cid):
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            row=self.private_command(c,capability,cid)
            if row['status']!='failed':raise DomainError('MAINTENANCE_STAGE_CONFLICT','Failed maintenance required for explicit recovery',409)
            if c.execute(sa.select(m.commands.c.id).where(m.commands.c.installation_id==self.installation_id,m.commands.c.status=='pending')).first():raise DomainError('RUNTIME_COMMAND_PENDING','Another command is pending',409)
            c.execute(m.commands.update().where(m.commands.c.id==cid).values(status='pending',stage='AWAITING_NATIVE_CONFIRMATION',boot_id=self.boot_id,error=None,plan=(row['plan']|{'confirmed':False,'nonce_hash':None,'recovering':True}) if row['plan'].get('payload') else {},resource_version=instance['version']+1,updated_at=now()))
            c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(active_command_id=cid,state='MAINTENANCE',version=instance['version']+1,updated_at=now()))
        return {'command_id':cid,'status':'pending','stage':'AWAITING_NATIVE_CONFIRMATION'}

    def execute_maintenance(self,row):
        from app.modules.operations.runtime_artifacts import fingerprint
        plan=self.native_port.restore_plan(row['plan']['payload'],row['plan']['fingerprint'])
        if row['plan'].get('confirmed') is not True:raise DomainError('NATIVE_CONFIRMATION_INVALID','Explicit native confirmation required',403)
        sequences={
            'backup_local':['CREATE_BACKUP','VALIDATE_BACKUP'],
            'verify_local_backup':['VALIDATE_BACKUP','VERIFY_RESTORE'],
            'restore_new_instance':['VALIDATE_BACKUP','CREATE_RESTORE_TARGET','VERIFY_RESTORE','REGISTER_RESTORED_INSTANCE'],
            'upgrade_app':['VERIFY_RELEASE','CREATE_BACKUP','VALIDATE_BACKUP','VERIFY_RESTORE','INSTALL_VERSION','MIGRATE_SCHEMA','WARM_MODEL','IMPORT_BUNDLE_RESOURCES','SELF_CHECK','ACTIVATE_BUNDLE','ACTIVATE_POINTER'],
            'switch_model':['VERIFY_RELEASE','STAGE_MODEL','WARM_MODEL','IMPORT_BUNDLE_RESOURCES','ACTIVATE_BUNDLE','SELF_CHECK','ACTIVATE_POINTER'],
        }
        prior={}
        for stage in sequences[row['operation']]:
            self.native_port.registry.recheck(plan)
            if stage in {'WARM_MODEL','SELF_CHECK'}:
                expected=plan.payload['artifact']['bundle'];proxy=self.core.scorer
                try:observed=proxy.readiness()
                except Exception:observed={}
                configured=getattr(proxy,'manifest_id',None)==expected['model_manifest_id'] and getattr(proxy,'manifest_sha256',None)==expected['model_manifest_sha256']
                if configured and observed.get('ready') is not True:
                    deadline=row['plan'].get('model_probe_deadline_ns')
                    if deadline is None:
                        deadline=self.clock()+120_000_000_000
                        with self.db.engine.begin() as c:c.execute(m.commands.update().where(m.commands.c.id==row['id'],m.commands.c.status=='pending').values(plan=row['plan']|{'model_probe_deadline_ns':deadline},updated_at=now()))
                    if self.clock()>=deadline:raise DomainError('MODEL_WARMUP_TIMEOUT','Fresh authenticated model observation did not return within warmup budget',503)
                    self.update(row['id'],stage='WARMING_MODEL_PROBE',state='MAINTENANCE');return
            with self.db.engine.connect() as c:
                fresh=c.execute(sa.select(m.commands).where(m.commands.c.id==row['id'])).mappings().one()
                self.authorize(c,row['actor_id'])
                if fresh['status']!='pending':return
                if c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status.in_(ACTIVE_SESSIONS)).limit(1)).first():raise DomainError('MAINTENANCE_SESSION_ACTIVE','A new business session blocks maintenance',409)
                saved=c.execute(sa.select(m.steps).where(m.steps.c.command_id==row['id'],m.steps.c.stage==stage)).mappings().first()
            if saved:
                if saved['evidence'].get('plan_fingerprint')!=plan.fingerprint:raise DomainError('MAINTENANCE_RECOVERY_REQUIRED','Recovery must use the exact previously confirmed artifact',409)
                if saved['status']=='completed' and not row['plan'].get('recovering'):
                    prior[stage]=saved['evidence'].get('result',{});continue
                if stage=='VERIFY_RELEASE':
                    self.native_port.registry.recheck(plan);prior[stage]=saved['evidence'].get('result',{});continue
                if stage not in {'ACTIVATE_BUNDLE','IMPORT_BUNDLE_RESOURCES'}:
                    reconciled=self.native_port.reconcile(stage,plan,prior)
                    if reconciled.get('completed') is True:
                        result=reconciled.get('result',{})
                        self.record_stage(row['id'],stage,'completed',plan.fingerprint,result)
                        if stage=='CREATE_BACKUP':self.observe_backup(row,result['backup'])
                        prior[stage]=result
                        continue
                    if reconciled.get('safe_to_retry') is not True:raise DomainError('MAINTENANCE_RECOVERY_REQUIRED','Ambiguous native stage must be reconciled',409)
            self.record_stage(row['id'],stage,'running',plan.fingerprint,{})
            if stage=='CREATE_BACKUP':self.begin_backup(row)
            if stage=='VERIFY_RELEASE':
                self.native_port.registry.recheck(plan);result={'manifest_sha256':plan.payload['manifest_sha256']}
            elif stage=='IMPORT_BUNDLE_RESOURCES':
                resource=plan.payload['artifact'].get('bundle_resource')
                if resource:
                    if self.resource_issuer is None or not callable(getattr(self.core,'import_verified_bundle_resources',None)):raise DomainError('DEPENDENCY_UNAVAILABLE','Verified bundle resource import port missing',503)
                    name=resource.get('path');expected=resource.get('sha256')
                    path=self.native_port.registry.path(name)
                    if str(path) not in plan.payload['artifact_paths'] or path.stat().st_size>64*1024*1024 or __import__('app.modules.operations.runtime_artifacts',fromlist=['sha_file']).sha_file(path)!=expected:raise DomainError('MAINTENANCE_ARTIFACT_CHANGED','Bundle resource file changed',409)
                    raw=path.read_bytes();identity=plan.payload['artifact']['bundle']
                    permit=self.resource_issuer(identity,raw,expected,row['actor_id'],row['id'])
                    with self.db.engine.begin() as c:result=self.core.import_verified_bundle_resources(c,permit)
                else:result={'resource_import_not_required':True}
            elif stage=='ACTIVATE_BUNDLE':
                if self.bundle_issuer is None or not callable(getattr(self.core,'activate_verified_bundle',None)):raise DomainError('DEPENDENCY_UNAVAILABLE','Verified bundle activation port missing',503)
                bundle=plan.payload['artifact']['bundle'];permit=self.bundle_issuer(bundle,row['actor_id'],row['id'])
                with self.db.engine.begin() as c:result=self.core.activate_verified_bundle(c,permit)
                result={'bundle_digest':fingerprint(bundle),'activation':result}
            else:
                try:result=self.native_port.run_stage(stage,plan,prior)
                except DomainError as error:
                    if stage=='WARM_MODEL' and error.code=='MODEL_RESTART_REQUIRED':
                        self.restart_bundle(row,plan);return
                    raise
            self.record_stage(row['id'],stage,'completed',plan.fingerprint,result)
            if stage=='CREATE_BACKUP':self.observe_backup(row,result['backup'])
            prior[stage]=result
        if row['operation'] in {'backup_local','verify_local_backup'}:
            source=prior.get('CREATE_BACKUP',{}).get('backup') or plan.payload.get('artifact',{}).get('backup')
            if not source:raise DomainError('BACKUP_EVIDENCE_REQUIRED','Fixed backup metadata is missing',409)
            entry={key:source.get(key) for key in ['backup_id','last_completed_at','last_verified_at','app_version','schema_version','model_manifest_id','location_hint','last_error']}
            entry['status']='verified' if row['operation']=='verify_local_backup' else 'completed'
            entry['last_error']=None
            if entry['status']=='verified':entry['last_verified_at']=now().isoformat(timespec='milliseconds').replace('+00:00','Z')
            elif entry['last_completed_at'] is None:entry['last_completed_at']=now().isoformat(timespec='milliseconds').replace('+00:00','Z')
            with self.db.engine.begin() as c:c.execute(insert(m.backups).values(id=entry['backup_id'],installation_id=self.installation_id,command_id=row['id'],status=entry['status'],payload=entry,updated_at=now()).on_conflict_do_update(index_elements=[m.backups.c.id],set_={'status':entry['status'],'payload':entry,'command_id':row['id'],'updated_at':now()}))
        needs_restart=row['operation']=='upgrade_app' and not row['plan'].get('restart_resumed')
        self.update(row['id'],status='applied',stage='RESTART_REQUIRED' if needs_restart else 'COMPLETED',result={'requires_restart':needs_restart},state='MAINTENANCE' if needs_restart else 'CHECKING')

    def record_stage(self,cid,stage,status,plan_fingerprint,result):
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            command=c.execute(sa.select(m.commands).where(m.commands.c.id==cid).with_for_update()).mappings().one()
            if command['status']!='pending':raise DomainError('MAINTENANCE_STAGE_CONFLICT','Command was cancelled or replaced',409)
            c.execute(insert(m.steps).values(command_id=cid,stage=stage,status=status,evidence={'plan_fingerprint':plan_fingerprint,'result':result},updated_at=now()).on_conflict_do_update(index_elements=[m.steps.c.command_id,m.steps.c.stage],set_={'status':status,'evidence':{'plan_fingerprint':plan_fingerprint,'result':result},'updated_at':now()}))
            c.execute(m.commands.update().where(m.commands.c.id==cid).values(stage=stage,resource_version=instance['version']+1,updated_at=now()))
            c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(version=instance['version']+1,state='MAINTENANCE',updated_at=now()))

    def begin_backup(self,row):
        stamp=now()
        payload=dict(backup_id=row['id'],status='running',last_completed_at=None,last_verified_at=None,app_version=getattr(self.core,'app_version','0.1.0'),schema_version=row['plan']['payload']['before']['schema_version'],model_manifest_id=row['plan']['payload']['before'].get('model_manifest_id'),location_hint='private-backup:'+row['id'],last_error=None)
        with self.db.engine.begin() as c:
            existing=c.execute(sa.select(m.backups.c.id).where(m.backups.c.command_id==row['id']).limit(1)).first()
            if not existing:c.execute(m.backups.insert().values(id=row['id'],installation_id=self.installation_id,command_id=row['id'],status='running',payload=payload,updated_at=stamp))

    def observe_backup(self,row,payload):
        entry={k:payload.get(k) for k in ['backup_id','last_completed_at','last_verified_at','app_version','schema_version','model_manifest_id','location_hint','last_error']}
        entry['status']='running';entry['last_error']=None
        with self.db.engine.begin() as c:
            c.execute(m.backups.delete().where(m.backups.c.command_id==row['id'],m.backups.c.id!=entry['backup_id']))
            c.execute(insert(m.backups).values(id=entry['backup_id'],installation_id=self.installation_id,command_id=row['id'],status='running',payload=entry,updated_at=now()).on_conflict_do_update(index_elements=[m.backups.c.id],set_={'status':'running','payload':entry,'command_id':row['id'],'updated_at':now()}))

    def fail_backup(self,cid,error):
        with self.db.engine.begin() as c:
            for row in c.execute(sa.select(m.backups).where(m.backups.c.command_id==cid)).mappings():
                c.execute(m.backups.update().where(m.backups.c.id==row['id']).values(status='failed',payload=row['payload']|{'status':'failed','last_error':error},updated_at=now()))

    def _prepare_key_import(self,capability,actor_id,source_id):
        if capability is not self._confirmation_key:raise DomainError('FORBIDDEN','Native confirmation capability required',403)
        if self.native_port is None or not callable(self.control_importer):raise DomainError('NATIVE_CONTROL_UNAVAILABLE','Registered native trust import is not configured',503)
        path,raw,file_hash=self.native_port.registry.read_control(source_id)
        from app.modules.auth.evidence import parse_envelope
        envelope=parse_envelope(raw);kind=envelope.get('type');domain=envelope.get('domain')
        if (domain,kind)==('wordheat-live.readiness','freshness_proof'):raise DomainError('FRESHNESS_PROOF_CHANNEL_REQUIRED','Freshness proof must use its current private challenge channel',403)
        if (domain,kind) not in {('wordheat-live.trust','key_bundle'),('wordheat-live.readiness','gate_evidence'),('wordheat-live.readiness','live_grant'),('wordheat-live.readiness','revocation')}:raise DomainError('NATIVE_CONTROL_TYPE_UNSUPPORTED','This signed object is not accepted through native file import',403)
        cid=secrets.token_urlsafe(24);nonce=secrets.token_urlsafe(32)
        payload={'kind':'import_signed_control','declared_type':kind,'domain':domain,'command_id':cid,'source_id':source_id,'canonical_path':str(path),'file_sha256':file_hash,'installation_id':self.installation_id,'boot_id':self.boot_id}
        plan_hash=digest(payload)
        with self.db.engine.begin() as c:
            self.authorize(c,actor_id)
            c.execute(m.native_requests.insert().values(id=cid,installation_id=self.installation_id,actor_id=actor_id,boot_id=self.boot_id,status='pending',stage='AWAITING_NATIVE_CONFIRMATION',resource_version=1,payload=payload|{'fingerprint':plan_hash,'nonce_hash':hashlib.sha256(nonce.encode()).hexdigest()},updated_at=now()))
        return {'command_id':cid,'installation_id':self.installation_id,'fingerprint':plan_hash,'confirmation_nonce':nonce,'operation':'import_signed_control','declared_type':kind,'domain':domain,'canonical_path':str(path),'file_sha256':file_hash}

    def _confirm_key_import(self,capability,cid,fingerprint,nonce,installation_id):
        import hmac
        if capability is not self._confirmation_key:raise DomainError('FORBIDDEN','Native confirmation capability required',403)
        with self.db.engine.connect() as c:
            row=c.execute(sa.select(m.native_requests).where(m.native_requests.c.id==cid)).mappings().first()
            if not row:raise DomainError('RESOURCE_NOT_FOUND','Native command not found',404)
            self.authorize(c,row['actor_id'])
        p=row['payload']
        if row['boot_id']!=self.boot_id or row['installation_id']!=installation_id or installation_id!=self.installation_id or row['status']!='pending' or p['fingerprint']!=fingerprint or not isinstance(nonce,str) or not hmac.compare_digest(p['nonce_hash'],hashlib.sha256(nonce.encode()).hexdigest()):raise DomainError('NATIVE_CONFIRMATION_INVALID','Confirmation binding is invalid or already consumed',403)
        path,raw,file_hash=self.native_port.registry.read_control(p['source_id'],p['file_sha256'])
        if str(path)!=p['canonical_path']:raise DomainError('MAINTENANCE_ARTIFACT_CHANGED','Selected control file changed',409)
        confirmation_id=secrets.token_urlsafe(24)
        try:
            with self.db.engine.begin() as c:
                c.execute(sa.text("SET LOCAL lock_timeout='1000ms'"));c.execute(sa.text("SET LOCAL statement_timeout='2500ms'"))
                current=c.execute(sa.select(m.native_requests).where(m.native_requests.c.id==cid).with_for_update()).mappings().one()
                self.authorize(c,current['actor_id'])
                if current['status']!='pending' or current['payload']!=p:raise DomainError('NATIVE_CONFIRMATION_INVALID','Confirmation already consumed or replaced',403)
                result=self.control_importer(c,raw,native_command_id=cid,confirmation_id=confirmation_id)
                if isinstance(result,(dict,list,str,bool)) or not all(hasattr(result,k) for k in ['object_id','digest','imported']):raise DomainError('NATIVE_EXECUTION_UNVERIFIED','Typed trusted repository result required',403)
                evidence={'object_id':result.object_id,'signature_input_digest':result.digest,'file_sha256':file_hash,'imported':bool(result.imported),'confirmation_id':confirmation_id}
                c.execute(m.native_requests.update().where(m.native_requests.c.id==cid).values(status='applied',stage='IMPORTED',resource_version=current['resource_version']+1,payload=p|{'nonce_hash':None,'confirmation_id':confirmation_id},result=evidence,updated_at=now()))
            return {'command_id':cid,'status':'applied','object_id':result.object_id,'signature_input_digest':result.digest,'file_sha256':file_hash}
        except DomainError as error:
            with self.db.engine.begin() as c:c.execute(m.native_requests.update().where(m.native_requests.c.id==cid,m.native_requests.c.status=='pending').values(status='failed',stage='FAILED',error=brief(error.code,error.message,error.retryable),updated_at=now()))
            raise

    def restart_bundle(self,row,plan):
        if not callable(getattr(self.stop_port,'prepare_restart',None)):
            raise DomainError('RUNTIME_RESTART_UNAVAILABLE','Registered suite restart port is not connected',503)
        artifact=plan.payload['artifact'];bundle=artifact['bundle']
        relative=artifact.get('model_manifest')
        roots=self.native_port.registry.model_roots
        model_root=roots.get(artifact.get('model_root_id'))
        if not relative or not model_root:raise DomainError('MODEL_RESTART_PLAN_INVALID','Registered model manifest/root required',409)
        path=self.native_port.registry.path(relative)
        if str(path) not in plan.payload['artifact_paths']:raise DomainError('MODEL_RESTART_PLAN_INVALID','Model manifest must be on the signed file whitelist',409)
        from app.modules.operations.runtime_artifacts import sha_file
        expected={'command_id':row['id'],'plan_fingerprint':plan.fingerprint,'confirmation_id':row['plan']['confirmation_id'],'installation_id':self.installation_id,'manifest_path':str(path),'manifest_file_sha256':sha_file(path),'model_root':model_root,'model_manifest_id':bundle['model_manifest_id'],'model_manifest_sha256':bundle['model_manifest_sha256']}
        with self.db.engine.begin() as c:
            from app.modules.wordbank.selection import catalog_lock
            catalog_lock(c)
            instance=c.execute(sa.select(m.instances).where(m.instances.c.installation_id==self.installation_id).with_for_update()).mappings().one()
            current=c.execute(sa.select(m.commands).where(m.commands.c.id==row['id']).with_for_update()).mappings().one()
            if current['status']!='pending' or current['boot_id']!=self.boot_id:raise DomainError('MAINTENANCE_STAGE_CONFLICT','Restart intent changed',409)
            c.execute(m.commands.update().where(m.commands.c.id==row['id']).values(stage='RESTART_REQUIRED',plan=current['plan']|{'restart_expected':expected},resource_version=instance['version']+1,updated_at=now()))
            c.execute(m.instances.update().where(m.instances.c.installation_id==self.installation_id).values(state='MAINTENANCE',version=instance['version']+1,updated_at=now()))
        evidence=self.stop_port.prepare_restart(expected,self.boot_id,15)
        if not isinstance(evidence,dict) or evidence.get('authorized') is not True:raise DomainError('RUNTIME_RESTART_UNAUTHORIZED','Private parent restart authority was not proven',403)
        self.stop_port.finish_exit()
