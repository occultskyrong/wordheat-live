"""Joint model/wordbank/calibration authority, activated by a native capability.

The capability issuer is handed only to the trusted runtime controller at
composition time. No HTTP input shape can instantiate an activation permit.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib,re
import rfc8785
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid
from app.modules.wordbank import selection

@dataclass(frozen=True)
class BundlePermit:
    _token:object
    identity:dict
    identity_sha256:str
    actor_id:str
    command_id:str

def digest(value):
    try: return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except (ValueError,TypeError): raise DomainError('BUNDLE_INVALID','Invalid JCS bundle value',422) from None

def model_identity(core):
    scorer=core.scorer
    observation=scorer.readiness() if callable(getattr(scorer,'readiness',None)) else {}
    if observation.get('ready') is not True: raise DomainError('SCORER_NOT_READY','Actual model must be ready',503)
    identity={'model_manifest_id':getattr(scorer,'manifest_id',None),'model_manifest_sha256':getattr(scorer,'manifest_sha256',None)}
    if not identity['model_manifest_id'] or not re.fullmatch('[0-9a-f]{64}',identity['model_manifest_sha256'] or ''): raise DomainError('BUNDLE_INVALID','Actual frozen model identity required',409)
    return identity

def components(c,identity):
    records=[]; words=[]; calibrations=[]
    for reference in sorted(identity['puzzles'],key=lambda v:v['puzzle_version_id']):
        row=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==reference['puzzle_version_id'])).mappings().first()
        if not row or row['status']!='published' or not row['approved_by'] or row['manifest_id']!=identity['model_manifest_id'] or row['calibration_id']!=reference['calibration_id'] or row['evaluation_id']!=reference['evaluation_id'] or row['scoring_artifact'].get('manifest_sha256')!=identity['model_manifest_sha256'] or digest(row['scoring_artifact'])!=reference['scoring_artifact_sha256']:
            raise DomainError('BUNDLE_VERSION_MISMATCH','Published puzzle/model/calibration binding changed',409)
        calibration=c.execute(sa.select(s.calibrations).where(s.calibrations.c.id==row['calibration_id'])).mappings().first()
        if not calibration or calibration['puzzle_version_id']!=row['id'] or calibration['evaluation_id']!=row['evaluation_id'] or calibration['artifact_sha256']!=reference['scoring_artifact_sha256'] or calibration['manifest_sha256']!=identity['model_manifest_sha256']: raise DomainError('BUNDLE_VERSION_MISMATCH','Frozen calibration authority missing',409)
        job=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==row['evaluation_id'])).mappings().first()
        if not job or job['status']!='succeeded' or not job['annotations_locked_at'] or job['input'].get('split')!='blind' or job['input'].get('puzzle_version_id')!=row['id'] or job['result'].get('manifest_sha256')!=identity['model_manifest_sha256'] or job['result'].get('calibration_id')!=row['calibration_id']: raise DomainError('BUNDLE_VERSION_MISMATCH','Locked evaluation authority missing',409)
        words.append({key:row[key] for key in ('id','puzzle_id','version','word','aliases','category_id','difficulty','hints','explanation','source_ref')})
        calibrations.append(reference)
        records.append(dict(row))
    return records,'sha256:'+digest(words),'sha256:'+digest(calibrations)

def validate(core,c,identity):
    if not isinstance(identity,dict) or set(identity)!={'version','model_manifest_id','model_manifest_sha256','wordbank_version','calibration_version','puzzles'} or type(identity['version']) is not int or identity['version']!=1:
        raise DomainError('BUNDLE_INVALID','Unsupported joint bundle shape',422)
    if any(not isinstance(identity[k],str) or not identity[k] for k in ('model_manifest_id','model_manifest_sha256','wordbank_version','calibration_version')) or not re.fullmatch('[0-9a-f]{64}',identity['model_manifest_sha256']): raise DomainError('BUNDLE_INVALID','Frozen bundle identity required',422)
    if not isinstance(identity['puzzles'],list) or not 1<=len(identity['puzzles'])<=10000: raise DomainError('BUNDLE_INVALID','Bounded nonempty puzzle manifest required',422)
    seen=set()
    for reference in identity['puzzles']:
        if not isinstance(reference,dict) or set(reference)!={'puzzle_version_id','calibration_id','evaluation_id','scoring_artifact_sha256'} or any(not isinstance(v,str) or not v for v in reference.values()) or not re.fullmatch('[0-9a-f]{64}',reference['scoring_artifact_sha256']) or reference['puzzle_version_id'] in seen: raise DomainError('BUNDLE_INVALID','Invalid or duplicate puzzle reference',422)
        seen.add(reference['puzzle_version_id'])
    actual=model_identity(core)
    if any(identity[k]!=actual[k] for k in actual): raise DomainError('BUNDLE_VERSION_MISMATCH','Actual model does not match bundle',409)
    model=c.execute(sa.select(s.model_manifests).where(s.model_manifests.c.manifest_sha256==identity['model_manifest_sha256'],s.model_manifests.c.model_manifest_id==identity['model_manifest_id'])).mappings().first()
    if not model or not {'revision','tokenizer','pooling','normalize','precision','preprocessing','dimension'}<=model['manifest'].keys() or digest(model['manifest'])!=identity['model_manifest_sha256']: raise DomainError('BUNDLE_VERSION_MISMATCH','Full frozen model manifest authority missing',409)
    _,words,calibrations=components(c,identity)
    if identity['wordbank_version']!=words or identity['calibration_version']!=calibrations: raise DomainError('BUNDLE_VERSION_MISMATCH','Joint catalog manifest digest changed',409)
    return deepcopy(identity)

def export(core,c,requested=None):
    actual=model_identity(core)
    if requested is not None and requested!=actual: raise DomainError('BUNDLE_VERSION_MISMATCH','Requested model identity mismatch',409)
    rows=c.execute(sa.select(s.puzzles).where(s.puzzles.c.status=='published',s.puzzles.c.manifest_id==actual['model_manifest_id'],s.puzzles.c.scoring_artifact['manifest_sha256'].astext==actual['model_manifest_sha256']).order_by(s.puzzles.c.id)).mappings()
    references=[{'puzzle_version_id':row['id'],'calibration_id':row['calibration_id'],'evaluation_id':row['evaluation_id'],'scoring_artifact_sha256':digest(row['scoring_artifact'])} for row in rows]
    identity={'version':1,**actual,'wordbank_version':'pending','calibration_version':'pending','puzzles':references}
    if not references: raise DomainError('NO_ELIGIBLE_PUZZLE','No complete published joint bundle',409)
    _,identity['wordbank_version'],identity['calibration_version']=components(c,identity)
    return validate(core,c,identity)

def activate(core,c,permit):
    if not isinstance(permit,BundlePermit) or permit._token is not core._bundle_token or digest(permit.identity)!=permit.identity_sha256: raise DomainError('FORBIDDEN','Trusted runtime activation capability required',403)
    operator=c.execute(sa.select(s.operators).where(s.operators.c.id==permit.actor_id)).mappings().first()
    if not operator or operator['disabled'] or 'maintenance.request' not in operator['permissions']: raise DomainError('FORBIDDEN','Current maintenance permission required',403)
    lock=int.from_bytes(hashlib.sha256(('bundle:'+permit.actor_id+':'+permit.command_id).encode()).digest()[:8],'big',signed=True)
    c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
    previous_activation=c.execute(sa.select(s.bundle_activations).where(s.bundle_activations.c.id==permit.command_id)).mappings().first()
    if previous_activation:
        if previous_activation['actor_id']!=permit.actor_id: raise DomainError('FORBIDDEN','Activation command ownership required',403)
        if previous_activation['identity_sha256']!=permit.identity_sha256: raise DomainError('IDEMPOTENCY_KEY_REUSED','Activation command binds another bundle',409)
        return previous_activation['result']
    selection.catalog_lock(c)
    sessions=list(c.execute(sa.select(s.sessions).order_by(s.sessions.c.id).with_for_update()).mappings())
    if c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']))).first(): raise DomainError('MAINTENANCE_SESSION_ACTIVE','All active rounds must be stopped before bundle activation',409)
    identity=validate(core,c,permit.identity); bundle_digest=digest(identity)
    previous=c.execute(sa.select(s.catalog)).mappings().one()
    if previous['bundle_digest']==bundle_digest:
        result={'bundle_digest':bundle_digest,'changed':False,'invalidated_sessions':[]}
        c.execute(s.bundle_activations.insert().values(id=permit.command_id,actor_id=permit.actor_id,identity_sha256=permit.identity_sha256,result=result)); return result
    c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(joint_identity=identity,bundle_digest=bundle_digest,model_manifest_id=identity['model_manifest_id'],model_manifest_sha256=identity['model_manifest_sha256'],revision=s.catalog.c.revision+1))
    for session in sessions:
        if session['status']=='ENDED': continue
        session=core.session(c,session['id'])
        core.reselect(c,session,'MANIFEST_CHANGED','bundle:'+bundle_digest,content={'bundle_digest':bundle_digest})
    invalidated=[]
    if core.official is not None:
        invalidated=core.official.invalidate_bundle(c,identity,bundle_digest)
    for session in sessions:
        if session['status']!='ENDED': core.touch(c,session['id'])
    core.audit(c,permit.actor_id,'activate_verified_bundle',bundle_digest,reason='NATIVE_CONFIRMED_VERIFIED_BUNDLE',details={'command_id':permit.command_id,'previous_bundle_digest':previous['bundle_digest']})
    result={'bundle_digest':bundle_digest,'changed':True,'invalidated_sessions':invalidated}
    c.execute(s.bundle_activations.insert().values(id=permit.command_id,actor_id=permit.actor_id,identity_sha256=permit.identity_sha256,result=result))
    return result
