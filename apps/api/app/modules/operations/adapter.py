"""Server-owned observations; no gate/grant is inferred from browser input."""
import hashlib
from pathlib import Path
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.auth.service import now
from app.modules.operations.readiness import evaluate_readiness

class ReadinessAdapter:
    def __init__(self,database,scorer,display_probe=None,*,trusted_control=None,connector=None):
        self.db=database; self.scorer=scorer; self.display_probe=display_probe
        self.trusted_control=trusted_control;self.connector=connector
        self.runtime_controller=None
        self._transaction_display_probe=None;self._display_probe_binding=None
        digest=hashlib.sha256()
        root=Path(__file__).resolve().parents[2]
        for source in sorted(root.rglob('*.py')):
            digest.update(source.relative_to(root).as_posix().encode()); digest.update(source.read_bytes())
        self.digest=digest.hexdigest()  # Local source fingerprint; never RC/gate evidence.

    def bind_display_probe(self,probe,*,in_transaction):
        self.display_probe=probe
        self._display_probe_binding=probe
        self._transaction_display_probe=in_transaction

    def probe_display(self,c,*,session_id,display_instance_id):
        probe=self.display_probe
        if probe is self._display_probe_binding and callable(self._transaction_display_probe):
            return bool(self._transaction_display_probe(c,session_id=session_id,display_instance_id=display_instance_id))
        # Explicit custom/replaced observers retain their existing keyword-only
        # callback contract; only the standard DB observer owns a SQL port.
        return bool(probe and probe(session_id=session_id,display_instance_id=display_instance_id))

    def evaluate(self,c,session,permissions):
        sid=session['id'] if session else None
        context={'session_id':sid,'mode':session['mode'] if session else None,'run_purpose':session['run_purpose'] if session else None,'live_profile':session.get('live_profile') if session else None,'app_id':session.get('app_id') if session else None,'room_binding_id':session.get('room_binding_id') if session else None,'installation_id':self.db.settings.instance_id,'candidate_digest':session.get('candidate_digest') if session and session['mode']=='official' else self.digest,'model_manifest_id':None,'wordbank_manifest_id':None,'calibration_id':None,'authorization_id':session.get('authorization_id') if session else None}
        scope={key:context[key] for key in ('installation_id','candidate_digest')}
        def item(status,reason,scope=scope): return {'status':status,'reason_code':reason,'message':'','scope':scope}
        try:
            self.db.verify_connection(c)
            marker=c.execute(sa.select(s.instance)).mappings().one()
            role=c.execute(sa.text("SELECT r.rolsuper,r.rolcreatedb,r.rolcreaterole,has_schema_privilege(current_user,'public','CREATE') AS ddl,pg_has_role(current_user,d.datdba,'USAGE') AS owner FROM pg_roles r,pg_database d WHERE r.rolname=current_user AND d.datname=current_database()")).mappings().one()
            db_ok=marker['instance_id']==self.db.settings.instance_id and marker['schema_version']==1 and not any(role.values())
        except Exception: db_ok=False
        model={}
        try:
            observer=getattr(self.scorer,'readiness',None)
            model=observer() if callable(observer) else {}
        except Exception: model={}
        model_ok=model.get('ready') is True and bool(model.get('manifest_id'))
        context['model_manifest_id']=model.get('manifest_id') if model_ok else None
        query=sa.select(s.puzzles).where(s.puzzles.c.status=='published',s.puzzles.c.manifest_id==context['model_manifest_id'],s.puzzles.c.calibration_id.is_not(None),s.puzzles.c.evaluation_id.is_not(None),s.puzzles.c.approved_by.is_not(None))
        joint={}
        if 'joint_identity' in s.catalog.c:
            joint=c.execute(sa.select(s.catalog.c.joint_identity).where(s.catalog.c.id=='global')).scalar_one_or_none() or {}
            if joint:
                actual_sha=model.get('manifest_sha256') or getattr(self.scorer,'manifest_sha256',None)
                if actual_sha!=joint.get('model_manifest_sha256'):model_ok=False;context['model_manifest_id']=None
                query=query.where(s.puzzles.c.id.in_([ref['puzzle_version_id'] for ref in joint.get('puzzles',[])]))
        if session:
            rules=session['rules']; query=query.where(s.puzzles.c.difficulty.between(rules['difficulty_min'],rules['difficulty_max']))
            if rules.get('category_id'): query=query.where(s.puzzles.c.category_id==rules['category_id'])
        puzzle=c.execute(query.limit(1)).mappings().first() if model_ok else None
        if puzzle:
            context['calibration_id']=puzzle['calibration_id']
            inventory=getattr(self.trusted_control,'component_inventory',{}) if self.trusted_control else {}
            context['wordbank_manifest_id']=joint.get('wordbank_version') or inventory.get('wordbank_manifest_id')
            if joint:
                frozen=next((ref for ref in joint.get('puzzles',[]) if ref['puzzle_version_id']==puzzle['id']),None)
                if not frozen or frozen.get('calibration_id')!=puzzle['calibration_id'] or frozen.get('evaluation_id')!=puzzle['evaluation_id'] or joint.get('model_manifest_id')!=context['model_manifest_id']:puzzle=None
                else:context['calibration_id']=joint['calibration_version']
        if session and session['mode']=='official' and not joint:puzzle=None
        if not joint:context['wordbank_manifest_id']=None;context['calibration_id']=None
        primary=c.execute(sa.select(s.displays.c.id).where(s.displays.c.session_id==sid,s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False),s.displays.c.expires_at>now())).scalar_one_or_none() if sid else None
        display_ok=bool(primary and self.probe_display(c,session_id=sid,display_instance_id=primary))
        checks={'db':item('pass' if db_ok else 'fail','OK' if db_ok else 'DATABASE_NOT_READY'),'model':item('pass' if model_ok else 'fail','OK' if model_ok else 'SCORER_NOT_READY'),'wordbank':item('pass' if puzzle else 'fail','OK' if puzzle else 'NO_MATCHING_PUBLISHED_PUZZLE'),'relay':item('fail','RELAY_NOT_CONNECTED'),'lease':item('fail','LIVE_AUTHORIZATION_REQUIRED'),'display':item('pass' if display_ok else 'fail','OK' if display_ok else 'CURRENT_PRIMARY_NOT_READY',{'session_id':sid})}
        gates={key:item('pending','SIGNED_EVIDENCE_REQUIRED') for key in ('G0','G1_Q','G1_R','G2','rc_verified')}
        authorization={'operator_permissions':permissions}
        if session and session['mode']=='official' and self.trusted_control and self.connector:
            from app.errors import DomainError
            observations=self.connector.observations(c,session)
            checks.update(observations['checks']);authorization['platform_binding']=observations['platform_binding']
            gates.update(self.trusted_control.gate_items(c,context))
            try:
                authorization['grant']=self.trusted_control.get_grant(c,session['authorization_id'])
                authorization['runtime']=self.trusted_control.require_current(c,session)
            except DomainError as exc:authorization['failure']=exc.code
        return evaluate_readiness(context,checks,gates,authorization,now())

    @staticmethod
    def c1(readiness):
        return {key:readiness[key] for key in ('checks','gates','can_rehearse','can_test_live','can_go_live')}

    def runtime(self,c,readiness,extended=False):
        if self.runtime_controller is not None:return self.runtime_controller.status(c,readiness,extended=extended)
        active=c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status.in_(['ACTIVE','ENDING','RECOVERY_REQUIRED'])).order_by(s.sessions.c.created_at.desc()).limit(1)).scalar_one_or_none()
        basic=all(readiness['checks'][key]['status']=='pass' for key in ('db','model','wordbank'))
        backup={'backup_id':None,'status':'none','last_completed_at':None,'last_verified_at':None,'app_version':None,'schema_version':None,'model_manifest_id':None,'location_hint':None,'last_error':None}
        return {'version':1,'launcher_state':'RECOVERY_REQUIRED' if active and c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.id==active,s.sessions.c.status=='RECOVERY_REQUIRED')).first() else 'READY_REHEARSAL' if basic else 'WARMING','active_session_id':active,'installation_id':self.db.settings.instance_id,'app_version':'0.1.0','model_manifest_id':readiness['context']['model_manifest_id'],'schema_version':1,'readiness':readiness if extended else self.c1(readiness),'backup_status':{'local':dict(backup),'relay':dict(backup)},'maintenance_status':{'command_id':None,'operation':None,'stage':'unavailable','requires_native_confirmation':True,'last_error':{'code':'NATIVE_MAINTENANCE_UNAVAILABLE','message':'Native maintenance executor is not connected','retryable':False}}}
