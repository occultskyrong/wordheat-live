"""Native-confirmed signed-resource import; no browser vectors or filesystem IO.

Runtime verifies file whitelist/signature/confirmation before obtaining a permit.
Core rechecks exact bytes, frozen model identity, historical evaluation bindings,
finite vectors/calibration and immutable IDs inside one catalog transaction.
"""
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime
import hashlib,json,math,re
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid
from app.modules.wordbank.models import Draft
from app.modules.wordbank.service import puzzle_snapshot
from app.modules.wordbank import selection,bundle

MAX_RESOURCE_BYTES=64*1024*1024
@dataclass(frozen=True)
class BundleResourcePermit:
    _token:object
    identity:dict
    identity_sha256:str
    content:dict
    content_sha256:str
    file_sha256:str
    actor_id:str
    command_id:str

def parse(raw,expected):
    if not isinstance(raw,bytes) or not 1<=len(raw)<=MAX_RESOURCE_BYTES or not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected) or hashlib.sha256(raw).hexdigest()!=expected:raise DomainError('BUNDLE_RESOURCE_INVALID','Native resource byte hash/limit mismatch',422)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError()
            result[key]=value
        return result
    try:
        content=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError):raise DomainError('BUNDLE_RESOURCE_INVALID','Strict UTF-8 resource JSON required',422) from None
    return content

def definitions(content):
    if not isinstance(content,dict) or set(content)!={'version','model_manifest','puzzles'} or type(content['version']) is not int or content['version']!=1 or not isinstance(content['model_manifest'],dict) or not isinstance(content['puzzles'],list) or not 1<=len(content['puzzles'])<=10000:raise DomainError('BUNDLE_RESOURCE_INVALID','Invalid joint resource shape',422)
    seen=set();versions=set();calibrations=set();evaluations=set();prepared=[]
    for entry in content['puzzles']:
        keys={'puzzle_version_id','puzzle_id','version','definition','calibration_id','evaluation_id','scoring_artifact','evaluation_input','evaluation_result','annotations_locked_at'}
        if not isinstance(entry,dict) or set(entry)!=keys or type(entry['version']) is not int or entry['version']<1:raise DomainError('BUNDLE_RESOURCE_INVALID','Invalid immutable resource entry',422)
        for key in ('puzzle_version_id','puzzle_id','calibration_id','evaluation_id'):
            if not isinstance(entry[key],str) or not 1<=len(entry[key])<=128:raise DomainError('BUNDLE_RESOURCE_INVALID','Opaque bounded resource IDs required',422)
        try:
            definition=Draft.model_validate(entry['definition']).model_dump()
            locked=datetime.fromisoformat(entry['annotations_locked_at'].replace('Z','+00:00'))
        except (ValueError,TypeError,AttributeError):raise DomainError('BUNDLE_RESOURCE_INVALID','Invalid definition or annotation lock',422) from None
        if definition!=entry['definition'] or locked.tzinfo is None:raise DomainError('BUNDLE_RESOURCE_INVALID','Canonical definition and UTC annotation lock required',422)
        if entry['puzzle_version_id'] in seen or (entry['puzzle_id'],entry['version']) in versions or entry['calibration_id'] in calibrations or entry['evaluation_id'] in evaluations:raise DomainError('BUNDLE_RESOURCE_INVALID','Duplicate immutable resource identity',422)
        seen.add(entry['puzzle_version_id']);versions.add((entry['puzzle_id'],entry['version']));calibrations.add(entry['calibration_id']);evaluations.add(entry['evaluation_id'])
        prepared.append((entry,definition,locked))
    return prepared

def describe(content):
    entries=definitions(content);manifest=content['model_manifest'];refs=[];words=[]
    for entry,definition,_ in sorted(entries,key=lambda item:item[0]['puzzle_version_id']):
        refs.append({'puzzle_version_id':entry['puzzle_version_id'],'calibration_id':entry['calibration_id'],'evaluation_id':entry['evaluation_id'],'scoring_artifact_sha256':bundle.digest(entry['scoring_artifact'])})
        words.append({'id':entry['puzzle_version_id'],'puzzle_id':entry['puzzle_id'],'version':entry['version'],**definition})
    return {'version':1,'model_manifest_id':manifest.get('manifest_id'),'model_manifest_sha256':bundle.digest(manifest),'wordbank_version':'sha256:'+bundle.digest(words),'calibration_version':'sha256:'+bundle.digest(refs),'puzzles':refs}

def export(core,c,identity):
    identity=bundle.validate(core,c,identity)
    manifest=c.execute(sa.select(s.model_manifests.c.manifest).where(s.model_manifests.c.manifest_sha256==identity['model_manifest_sha256'])).scalar_one();entries=[]
    for ref in sorted(identity['puzzles'],key=lambda item:item['puzzle_version_id']):
        puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==ref['puzzle_version_id'])).mappings().one()
        job=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==ref['evaluation_id'])).mappings().one()
        definition={key:puzzle[key] for key in ('word','aliases','category_id','difficulty','hints','explanation','source_ref')}
        entries.append({'puzzle_version_id':puzzle['id'],'puzzle_id':puzzle['puzzle_id'],'version':puzzle['version'],'definition':definition,'calibration_id':puzzle['calibration_id'],'evaluation_id':puzzle['evaluation_id'],'scoring_artifact':puzzle['scoring_artifact'],'evaluation_input':job['input'],'evaluation_result':job['result'],'annotations_locked_at':job['annotations_locked_at'].isoformat()})
    content={'version':1,'model_manifest':manifest,'puzzles':entries}
    if describe(content)!=identity:raise DomainError('BUNDLE_RESOURCE_INVALID','Resource export does not match descriptor',409)
    return content

def check_entry(entry,definition,identity,dimension):
    artifact=entry['scoring_artifact'];body=entry['evaluation_input'];result=entry['evaluation_result']
    if not all(isinstance(v,dict) for v in (artifact,body,result)):raise DomainError('BUNDLE_RESOURCE_INVALID','Complete frozen evaluation/artifact required',422)
    frozen={'id':entry['puzzle_version_id'],'puzzle_id':entry['puzzle_id'],'version':entry['version'],**definition}
    review=artifact.get('review',{})
    if review.get('decision')!='approve' or not review.get('actor_id') or review.get('evaluation_id')!=entry['evaluation_id'] or body.get('puzzle_version_id')!=entry['puzzle_version_id'] or body.get('manifest_id')!=identity['model_manifest_id'] or body.get('model_manifest_sha256')!=identity['model_manifest_sha256'] or body.get('split')!='blind' or bundle.digest(body.get('model_manifest',{}))!=identity['model_manifest_sha256'] or body.get('frozen_puzzle')!=frozen or body.get('puzzle_input_sha256')!=bundle.digest(frozen) or body.get('evaluation_input_sha256')!=bundle.digest({k:v for k,v in body.items() if k!='evaluation_input_sha256'}):raise DomainError('BUNDLE_RESOURCE_INVALID','Locked approval/input binding mismatch',409)
    if result.get('manifest_id')!=identity['model_manifest_id'] or result.get('manifest_sha256')!=identity['model_manifest_sha256'] or result.get('calibration_id')!=entry['calibration_id'] or result.get('global_G1_Q') is not False:raise DomainError('BUNDLE_RESOURCE_INVALID','Evaluation model/calibration authority mismatch',409)
    if type(artifact.get('dimension')) is not int or type(result.get('dimension')) is not int or artifact.get('dimension')!=dimension or result.get('dimension')!=dimension or artifact.get('normalization')!='unit-l2/1' or result.get('normalization')!='unit-l2/1' or result.get('quality_scope')!='single-puzzle-evaluation' or artifact.get('manifest_sha256')!=identity['model_manifest_sha256']:raise DomainError('BUNDLE_RESOURCE_INVALID','Frozen embedding identity mismatch',409)
    vector=artifact.get('target_vector');calibration=artifact.get('calibration',{})
    if not isinstance(vector,list) or len(vector)!=dimension or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector) or abs(sum(v*v for v in vector)-1)>.001 or result.get('target_vector')!=vector or result.get('calibration')!=calibration:raise DomainError('BUNDLE_RESOURCE_INVALID','Finite normalized target and matching calibration required',422)
    try:b,u,gamma=(calibration[key] for key in ('b','u','gamma'))
    except (KeyError,TypeError):raise DomainError('BUNDLE_RESOURCE_INVALID','Missing frozen calibration',422) from None
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (b,u,gamma)) or not u>b or not gamma>0:raise DomainError('BUNDLE_RESOURCE_INVALID','Invalid frozen calibration values',422)
    pairs=result.get('pairs');annotations=body.get('pairs')
    if not isinstance(pairs,list) or not 1<=len(pairs)<=1000 or not isinstance(annotations,list) or len(pairs)!=len(annotations):raise DomainError('BUNDLE_RESOURCE_INVALID','Complete locked word pairs required',422)
    samples=[]
    for pair,human in zip(pairs,annotations):
        if pair.get('guess')!=human.get('guess') or pair.get('human_rating')!=human.get('human_rating') or pair.get('human_rating') not in {'cold','warm','hot','exclude'}:raise DomainError('BUNDLE_RESOURCE_INVALID','Annotation pair binding changed',409)
        raw=pair.get('raw_score');score=pair.get('display_score')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (raw,score)) or not -1.001<=raw<=1.001 or not 0<=score<=99.9 or abs(score-round(min(99.9,99.9*min(1,max(0,(raw-b)/(u-b)))**gamma),1))>1e-8:raise DomainError('BUNDLE_RESOURCE_INVALID','Raw/display scoring evidence mismatch',422)
        if pair['human_rating']!='exclude':samples.append((raw,{'cold':0,'warm':1,'hot':2}[pair['human_rating']]))
    comparisons=[(a,b) for index,a in enumerate(samples) for b in samples[index+1:] if a[1]!=b[1]]
    accuracy=sum((a[0]-b[0])*(a[1]-b[1])>0 for a,b in comparisons)/len(comparisons) if comparisons else None
    claimed=result.get('metrics',{}).get('pairwise_accuracy')
    if isinstance(claimed,bool) or not isinstance(claimed,(int,float)) or not math.isfinite(claimed) or accuracy is None or accuracy<.9 or abs(claimed-accuracy)>1e-9:raise DomainError('BUNDLE_RESOURCE_INVALID','Signed per-puzzle evaluation is not qualified',409)
    return frozen

def import_resources(core,c,permit):
    if not isinstance(permit,BundleResourcePermit) or permit._token is not core._bundle_token or bundle.digest(permit.identity)!=permit.identity_sha256 or bundle.digest(permit.content)!=permit.content_sha256:raise DomainError('FORBIDDEN','Native-verified resource capability required',403)
    operator=c.execute(sa.select(s.operators).where(s.operators.c.id==permit.actor_id)).mappings().first()
    if not operator or operator['disabled'] or 'maintenance.request' not in operator['permissions']:raise DomainError('FORBIDDEN','Current maintenance permission required',403)
    lock=int.from_bytes(hashlib.sha256(('bundle-import:'+permit.actor_id+':'+permit.command_id).encode()).digest()[:8],'big',signed=True);c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
    previous=c.execute(sa.select(s.bundle_imports).where(s.bundle_imports.c.id==permit.command_id)).mappings().first()
    if previous:
        if previous['actor_id']!=permit.actor_id:raise DomainError('FORBIDDEN','Resource import ownership required',403)
        if previous['file_sha256']!=permit.file_sha256 or previous['identity_sha256']!=permit.identity_sha256:raise DomainError('IDEMPOTENCY_KEY_REUSED','Import command binds another resource file',409)
        return previous['result']
    selection.catalog_lock(c);sessions=list(c.execute(sa.select(s.sessions).order_by(s.sessions.c.id).with_for_update()).mappings())
    if c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']))).first():raise DomainError('MAINTENANCE_SESSION_ACTIVE','Stop all active rounds before importing resources',409)
    if describe(permit.content)!=permit.identity or bundle.model_identity(core)!={k:permit.identity[k] for k in ('model_manifest_id','model_manifest_sha256')}:raise DomainError('BUNDLE_VERSION_MISMATCH','Resource descriptor or actual new model differs',409)
    manifest=permit.content['model_manifest'];actual_metadata=getattr(core.scorer,'manifest_metadata',None)
    if actual_metadata!=manifest:raise DomainError('BUNDLE_VERSION_MISMATCH','Actual complete model metadata differs',409)
    existing_model=c.execute(sa.select(s.model_manifests).where(s.model_manifests.c.manifest_sha256==permit.identity['model_manifest_sha256'])).mappings().first()
    if existing_model and existing_model['manifest']!=manifest:raise DomainError('BUNDLE_RESOURCE_CONFLICT','Existing immutable model metadata differs',409)
    if not existing_model:c.execute(s.model_manifests.insert().values(id=uid(),model_manifest_id=permit.identity['model_manifest_id'],manifest_sha256=permit.identity['model_manifest_sha256'],manifest=manifest))
    count=0;ids=[]
    for entry,definition,locked in definitions(permit.content):
        frozen=check_entry(entry,definition,permit.identity,core.scorer.dimension);artifact=entry['scoring_artifact'];artifact_sha=bundle.digest(artifact)
        existing=c.execute(sa.select(s.puzzles).where(sa.or_(s.puzzles.c.id==entry['puzzle_version_id'],sa.and_(s.puzzles.c.puzzle_id==entry['puzzle_id'],s.puzzles.c.version==entry['version'])))).mappings().first()
        if existing:
            if puzzle_snapshot(existing)!=frozen or existing['status']!='published' or existing['manifest_id']!=permit.identity['model_manifest_id'] or existing['calibration_id']!=entry['calibration_id'] or existing['evaluation_id']!=entry['evaluation_id'] or existing['scoring_artifact']!=artifact:raise DomainError('BUNDLE_RESOURCE_CONFLICT','Existing immutable puzzle differs',409)
        else:
            c.execute(s.puzzles.insert().values(id=entry['puzzle_version_id'],puzzle_id=entry['puzzle_id'],version=entry['version'],status='published',manifest_id=permit.identity['model_manifest_id'],calibration_id=entry['calibration_id'],evaluation_id=entry['evaluation_id'],approved_by=artifact['review']['actor_id'],scoring_artifact=artifact,**definition));count+=1
        job=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==entry['evaluation_id'])).mappings().first()
        if job:
            if job['kind']!='evaluation' or job['status']!='succeeded' or job['input']!=entry['evaluation_input'] or job['result']!=entry['evaluation_result'] or job['annotations_locked_at']!=locked:raise DomainError('BUNDLE_RESOURCE_CONFLICT','Existing immutable evaluation differs',409)
        else:c.execute(s.wordbank_jobs.insert().values(id=entry['evaluation_id'],actor_id=permit.actor_id,kind='evaluation',status='succeeded',version=1,input=entry['evaluation_input'],result=entry['evaluation_result'],annotations_locked_at=locked))
        calibration=c.execute(sa.select(s.calibrations).where(s.calibrations.c.id==entry['calibration_id'])).mappings().first()
        if calibration:
            if calibration['puzzle_version_id']!=entry['puzzle_version_id'] or calibration['evaluation_id']!=entry['evaluation_id'] or calibration['artifact_sha256']!=artifact_sha or calibration['artifact']!=artifact:raise DomainError('BUNDLE_RESOURCE_CONFLICT','Existing immutable calibration differs',409)
        else:c.execute(s.calibrations.insert().values(id=entry['calibration_id'],puzzle_version_id=entry['puzzle_version_id'],evaluation_id=entry['evaluation_id'],manifest_sha256=permit.identity['model_manifest_sha256'],artifact_sha256=artifact_sha,artifact=artifact))
        c.execute(s.resource_origins.insert().values(id=uid(),command_id=permit.command_id,puzzle_version_id=entry['puzzle_version_id'],evaluation_id=entry['evaluation_id'],file_sha256=permit.file_sha256,imported_by=permit.actor_id));ids.append(entry['puzzle_version_id'])
    bundle.validate(core,c,permit.identity)
    if count:
        c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(revision=s.catalog.c.revision+1))
        for session in sessions:
            if session['status']!='ENDED':core.reselect(c,core.session(c,session['id']),'MANIFEST_CHANGED','resource-import:'+permit.file_sha256,content={'resource_sha256':permit.file_sha256});core.touch(c,session['id'])
        if core.official is not None:core.official.invalidate_bundle(c,permit.identity,permit.identity_sha256)
    result={'resource_sha256':permit.file_sha256,'identity_sha256':permit.identity_sha256,'puzzle_versions':ids,'imported_count':count}
    c.execute(s.bundle_imports.insert().values(id=permit.command_id,actor_id=permit.actor_id,file_sha256=permit.file_sha256,identity_sha256=permit.identity_sha256,result=result))
    core.audit(c,permit.actor_id,'import_verified_bundle_resources',permit.identity_sha256,reason='NATIVE_SIGNED_RESOURCE_IMPORT',details={'command_id':permit.command_id,'file_sha256':permit.file_sha256,'imported_count':count})
    return result
