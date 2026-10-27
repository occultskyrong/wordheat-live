"""Durable private catalog use cases with authorization before idempotency."""
import base64
import csv
import hashlib
import io
import json
import secrets
import sqlalchemy as sa
from pydantic import ValidationError
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid,now
from app.modules.wordbank.models import Draft,Evaluation
from app.modules.wordbank import selection
from app.modules.wordbank.catalog import disable

def parse_csv(raw):
    if len(raw)>2*1024*1024: raise DomainError('PAYLOAD_TOO_LARGE','CSV limit is 2 MiB',413)
    try:
        reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''),strict=True)
        if reader.fieldnames!=['word','aliases','category_id','difficulty','hints','explanation','source_ref']: raise ValueError('columns')
        valid=[]; errors=[]
        for index,row in enumerate(reader,2):
            if index>10001: raise DomainError('PAYLOAD_TOO_LARGE','CSV limit is 10000 data rows',413)
            try:
                if None in row or any(v is None for v in row.values()): raise ValueError('columns')
                row['aliases']=json.loads(row['aliases']); row['hints']=json.loads(row['hints']); row['difficulty']=int(row['difficulty'])
                valid.append({'row':index,'draft':Draft.model_validate(row).model_dump()})
            except ValidationError as error:
                errors.append({'row':index,'code':'INPUT_INVALID','errors':[{'field':'.'.join(map(str,item['loc'])) or 'row','code':item['type'],'message':'Invalid field'} for item in error.errors(include_input=False)]})
            except (ValueError,TypeError): errors.append({'row':index,'code':'INPUT_INVALID','errors':[{'field':'row','code':'INPUT_INVALID','message':'Invalid CSV field encoding'}]})
        return valid,errors
    except DomainError: raise
    except (UnicodeError,csv.Error,ValueError): raise DomainError('INPUT_INVALID','CSV must use UTF-8 and the documented columns',422) from None

def puzzle_snapshot(row):
    return {key:row[key] for key in ('id','puzzle_id','version','word','aliases','category_id','difficulty','hints','explanation','source_ref')}

def puzzle_dto(row):
    return {key:row[key] for key in ('id','puzzle_id','version','word','aliases','category_id','difficulty','hints','explanation','source_ref','status','manifest_id','calibration_id','evaluation_id','approved_by')}

class WordbankService:
    def __init__(self,core):
        self.core=core; self.db=core.db
        key=getattr(self.db.settings,'analytics_cursor_key',None)
        self.paging_key=key if isinstance(key,bytes) and len(key)==32 else secrets.token_bytes(32)

    @staticmethod
    def authorize(c,actor,permission):
        operator=c.execute(sa.select(s.operators).where(s.operators.c.id==actor)).mappings().first()
        if not operator or operator['disabled']: raise DomainError('AUTH_REQUIRED','Active operator required',401)
        if permission not in operator['permissions']: raise DomainError('FORBIDDEN','Permission required',403)
        return operator

    @staticmethod
    def job(c,actor,jid,review=False):
        row=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==jid)).mappings().first()
        if not row: raise DomainError('RESOURCE_NOT_FOUND','Job not found',404)
        if row['actor_id']!=actor and not (review and row['kind']=='evaluation'): raise DomainError('FORBIDDEN','Job ownership required',403)
        return row

    def mutate(self,actor,method,path,key,body,permission,operation,job_id=None,request_id=None):
        if not key or len(key)>128: raise DomainError('INPUT_INVALID','Idempotency-Key required',422)
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with self.db.engine.begin() as c:
            self.authorize(c,actor,permission)
            if job_id: self.job(c,actor,job_id)
            lock=int.from_bytes(hashlib.sha256((actor+method+path+key).encode()).digest()[:8],'big',signed=True)
            c.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
            old=c.execute(sa.select(s.idempotency).where(s.idempotency.c.actor_id==actor,s.idempotency.c.method==method,s.idempotency.c.path==path,s.idempotency.c.key==key)).mappings().first()
            if old:
                if old['request_hash']!=digest: raise DomainError('IDEMPOTENCY_KEY_REUSED','Key binds different content',409)
                response=old['response']
                if response.get('job_id'):
                    existing=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==response['job_id'])).mappings().first()
                    if existing:
                        if existing['actor_id']!=actor:raise DomainError('FORBIDDEN','Job ownership required',403)
                        return self.job_dto(existing)
                    from app.modules.replays.service import ReplayService
                    return ReplayService.dto(ReplayService.job(c,actor,response['job_id']))
                if path.startswith('/api/v1/puzzle-imports/') and path.endswith('/commands'):
                    return self.import_command_result(c,actor,path.split('/')[4],response)
                return response
            if body.get('command')!='cancel':
                selection.catalog_lock(c)
                if self.core.runtime_guard:
                    action='catalog_mutation' if body.get('command') in {'approve','publish','disable','confirm','confirm_valid_rows'} else 'prepare'
                    self.core.runtime_guard(c,action=action)
            result=operation(c)
            c.execute(s.idempotency.insert().values(id=uid(),actor_id=actor,method=method,path=path,key=key,request_hash=digest,response=result))
            self.core.audit(c,actor,body.get('command',method),result.get('puzzle_id') or result.get('job_id') or job_id,result.get('resource_version',result.get('version',0)),body.get('args',{}).get('reason') or body.get('args',{}).get('notes'),request_id=request_id,details={'path':path})
            return result

    def create(self,c,body,puzzle_id=None,version=1):
        draft=Draft.model_validate(body).model_dump(); identity=uid(); puzzle_id=puzzle_id or uid()
        c.execute(s.puzzles.insert().values(id=identity,puzzle_id=puzzle_id,version=version,status='draft',**draft))
        return puzzle_dto(c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==identity)).mappings().one())

    def patch(self,c,pid,body):
        selection.catalog_lock(c)
        row=c.execute(sa.select(s.puzzles).where(s.puzzles.c.puzzle_id==pid).order_by(s.puzzles.c.version.desc()).limit(1).with_for_update()).mappings().first()
        if not row: raise DomainError('RESOURCE_NOT_FOUND','Puzzle not found',404)
        self.core.check_version(row,body['expected_version'])
        # Every modification creates a new immutable version, including draft edits.
        return self.create(c,{k:v for k,v in body.items() if k!='expected_version'},pid,row['version']+1)

    def enqueue(self,c,actor,kind,body):
        if kind=='evaluation':
            try: body=Evaluation.model_validate(body).model_dump()
            except ValidationError: raise DomainError('INPUT_INVALID','Invalid evaluation input',422) from None
            puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==body['puzzle_version_id'])).mappings().first()
            if not puzzle: raise DomainError('RESOURCE_NOT_FOUND','Puzzle version not found',404)
            if body['split']!='calibration':
                calibration=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.kind=='evaluation',s.wordbank_jobs.c.status=='succeeded',s.wordbank_jobs.c.input['puzzle_version_id'].astext==puzzle['id'],s.wordbank_jobs.c.input['manifest_id'].astext==body['manifest_id'],s.wordbank_jobs.c.input['split'].astext=='calibration').order_by(s.wordbank_jobs.c.created_at.desc()).limit(1)).mappings().first()
                if not calibration: raise DomainError('CALIBRATION_REQUIRED','A completed calibration split is required',409)
                if body['split']=='blind' and {p['guess'] for p in body['pairs']}&{p['guess'] for p in calibration['input']['pairs']}:
                    raise DomainError('BLIND_SET_OVERLAP','Blind pairs must be independent of calibration pairs',409)
                body=body|{'calibration_job_id':calibration['id']}
            from app.modules.wordbank.bundle import digest
            manifest_sha=getattr(self.core.scorer,'manifest_sha256',None)
            if getattr(self.core.scorer,'manifest_id',None)!=body['manifest_id'] or not isinstance(manifest_sha,str) or len(manifest_sha)!=64: raise DomainError('SCORER_NOT_READY','Frozen model identity required for evaluation',503)
            frozen=puzzle_snapshot(puzzle)
            body=body|{'frozen_puzzle':frozen,'puzzle_input_sha256':digest(frozen),'model_manifest_sha256':manifest_sha,'model_manifest':getattr(self.core.scorer,'manifest_metadata',None) or {}}
            body['evaluation_input_sha256']=digest(body)
        jid=uid()
        c.execute(s.wordbank_jobs.insert().values(id=jid,actor_id=actor,kind=kind,status='queued',input=body,annotations_locked_at=now() if kind=='evaluation' and body['split']=='blind' else None))
        return {'job_id':jid,'status':'queued','version':1,'progress':{'done':0,'total':1},'result':None,'error':None}

    def import_command_result(self,c,actor,jid,response):
        """Read-only projection also upgrades legacy successful command caches.

        Only the persisted report supplies IDs. Replaying an old cache never
        re-enters confirmation or creates another draft.
        """
        row=self.job(c,actor,jid)
        if row['kind']!='import': raise DomainError('RESOURCE_NOT_FOUND','Import job not found',404)
        state=response.get('state')
        operation=state.get('operation') if isinstance(state,dict) else state
        if operation not in {'confirm','confirm_valid_rows','cancel'}: return response
        operation='cancel' if operation=='cancel' else 'confirm_valid_rows'
        report=row['result'] or {}
        return response|{'state':{'validation_id':jid,'operation':operation,'puzzle_version_ids':list(report.get('puzzle_version_ids',[])) if operation=='confirm_valid_rows' else []}}

    def import_command(self,c,actor,jid,body):
        selection.catalog_lock(c)
        row=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==jid).with_for_update()).mappings().one()
        self.core.check_version(row,body['expected_version'])
        if row['kind']!='import': raise DomainError('INPUT_INVALID','Import job required',422)
        cmd=body['command']; args=body.get('args',{})
        if cmd=='cancel':
            if row['result'].get('confirmed_rows'): raise DomainError('JOB_STATE_CONFLICT','Confirmed import cannot be cancelled',409)
            if args: raise DomainError('INPUT_INVALID','Cancel args must be empty',422)
            c.execute(s.wordbank_jobs.update().where(s.wordbank_jobs.c.id==jid).values(status='cancelled',version=row['version']+1))
        elif cmd in {'confirm','confirm_valid_rows'}:
            if row['status']!='succeeded' or row['result'].get('confirmed_rows'): raise DomainError('JOB_STATE_CONFLICT','Validated unconfirmed import required',409)
            selected=args.get('rows')
            if set(args)!={'rows'} or not isinstance(selected,list) or not selected or len(set(selected))!=len(selected) or any(type(n)!=int for n in selected): raise DomainError('INPUT_INVALID','Explicit unique rows required',422)
            valid={entry['row']:entry['draft'] for entry in row['result']['valid_rows']}
            if not set(selected)<=valid.keys(): raise DomainError('INPUT_INVALID','Only valid report rows can be confirmed',422)
            selection.catalog_lock(c)
            created=[self.create(c,valid[number])['id'] for number in selected]
            c.execute(s.wordbank_jobs.update().where(s.wordbank_jobs.c.id==jid).values(version=row['version']+1,result=row['result']|{'confirmed_rows':selected,'puzzle_version_ids':created}))
        else: raise DomainError('INPUT_INVALID','confirm or cancel required',422)
        return self.import_command_result(c,actor,jid,{'command_id':uid(),'status':'applied','resource_version':row['version']+1,'state':cmd,'error':None})

    def puzzle_command(self,c,actor,pid,body):
        cmd=body['command']; args=body.get('args',{})
        if cmd=='disable': return disable(self.core,c,actor,pid,body)
        selection.catalog_lock(c)
        if cmd=='publish': list(c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.status!='ENDED').order_by(s.sessions.c.id).with_for_update()))
        row=c.execute(sa.select(s.puzzles).where(s.puzzles.c.puzzle_id==pid).order_by(s.puzzles.c.version.desc()).limit(1).with_for_update()).mappings().first()
        if not row: raise DomainError('RESOURCE_NOT_FOUND','Puzzle not found',404)
        self.core.check_version(row,body['expected_version'])
        if cmd=='evaluate':
            if set(args)!={'manifest_id','split','pairs'}: raise DomainError('INPUT_INVALID','Evaluation fields required',422)
            return self.enqueue(c,actor,'evaluation',args|{'puzzle_version_id':row['id']})
        if cmd=='approve':
            if set(args)!={'evaluation_id','decision','notes'} or not isinstance(args['evaluation_id'],str) or args['decision'] not in {'approve','reject'} or not isinstance(args['notes'],str) or len(args['notes'])>500: raise DomainError('INPUT_INVALID','Evaluation and review reason required',422)
            job=self.job(c,actor,args['evaluation_id'],review=True)
            from app.modules.wordbank.bundle import digest
            if job['input'].get('puzzle_input_sha256')!=digest(puzzle_snapshot(row)) or job['input'].get('evaluation_input_sha256')!=digest({k:v for k,v in job['input'].items() if k!='evaluation_input_sha256'}): raise DomainError('RESULT_STALE','Evaluation input no longer matches puzzle version',409)
            if job['input']['puzzle_version_id']!=row['id']: raise DomainError('PUZZLE_NOT_QUALIFIED','Evaluation version mismatch',409)
            if args['decision']=='reject':
                if row['status']!='draft': raise DomainError('PUZZLE_NOT_QUALIFIED','Draft required',409)
                c.execute(s.puzzles.update().where(s.puzzles.c.id==row['id']).values(scoring_artifact={'review':{'actor_id':actor,'decision':'reject','notes':args['notes'],'evaluation_id':job['id'],'reviewed_at':now().isoformat()}}))
                return {'command_id':uid(),'status':'applied','resource_version':row['version'],'state':'rejected','error':None}
            if job['status']!='succeeded' or job['input']['split']!='blind' or not job['annotations_locked_at'] or not job['result'].get('calibration_id') or job['result']['metrics']['pairwise_accuracy'] is None or job['result']['metrics']['pairwise_accuracy']<.9: raise DomainError('PUZZLE_NOT_QUALIFIED','Successful locked blind evaluation required',409)
            if row['status']!='draft': raise DomainError('PUZZLE_NOT_QUALIFIED','Draft required',409)
            result=job['result']; artifact={key:result[key] for key in ('calibration','target_vector','dimension','normalization','manifest_sha256')}
            artifact['review']={'actor_id':actor,'decision':'approve','notes':args['notes'],'evaluation_id':job['id'],'reviewed_at':now().isoformat()}
            from app.modules.wordbank.bundle import digest as artifact_digest
            metadata=job['input'].get('model_manifest',{})
            if not c.execute(sa.select(s.model_manifests.c.id).where(s.model_manifests.c.manifest_sha256==result['manifest_sha256'])).first():
                c.execute(s.model_manifests.insert().values(id=uid(),model_manifest_id=job['input']['manifest_id'],manifest_sha256=result['manifest_sha256'],manifest=metadata))
            c.execute(s.calibrations.insert().values(id=result['calibration_id'],puzzle_version_id=row['id'],evaluation_id=job['id'],manifest_sha256=result['manifest_sha256'],artifact_sha256=artifact_digest(artifact),artifact=artifact))
            c.execute(s.puzzles.update().where(s.puzzles.c.id==row['id']).values(status='approved',approved_by=actor,evaluation_id=job['id'],manifest_id=job['input']['manifest_id'],calibration_id=result['calibration_id'],scoring_artifact=artifact))
        elif cmd=='publish':
            if set(args)!={'approved_version'} or args['approved_version']!=row['version'] or row['status']!='approved' or not all(row[k] for k in ('approved_by','evaluation_id','manifest_id','calibration_id')) or not row['scoring_artifact']: raise DomainError('PUZZLE_NOT_QUALIFIED','Approved frozen model/calibration required',409)
            from app.modules.wordbank.bundle import digest
            job=self.job(c,actor,row['evaluation_id'],review=True)
            calibration=c.execute(sa.select(s.calibrations).where(s.calibrations.c.id==row['calibration_id'])).mappings().first()
            if job['input'].get('puzzle_input_sha256')!=digest(puzzle_snapshot(row)) or not calibration or calibration['artifact_sha256']!=digest(row['scoring_artifact']): raise DomainError('RESULT_STALE','Approved frozen puzzle authority changed',409)
            c.execute(s.puzzles.update().where(s.puzzles.c.id==row['id']).values(status='published'))
            c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(revision=s.catalog.c.revision+1))
            self.refresh_empty(c)
        else: raise DomainError('INPUT_INVALID','Unsupported puzzle command',422)
        return {'command_id':uid(),'status':'applied','resource_version':row['version'],'state':'approved' if cmd=='approve' else 'published','error':None}

    def refresh_empty(self,c):
        sessions=list(c.execute(sa.select(s.sessions).where(s.sessions.c.status!='ENDED').order_by(s.sessions.c.id).with_for_update()).mappings())
        revision=c.execute(sa.select(s.catalog.c.revision)).scalar_one()
        for session in sessions:
            session=self.core.session(c,session['id'])
            pointer=selection.pointer(c,session['id'])
            old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
            if old and old['status']=='EMPTY' and old['catalog_revision']!=revision:
                self.core.reselect(c,session,'CATALOG_CHANGED',f'publish:{revision}',content={'catalog_revision':revision})

    @staticmethod
    def job_dto(row):
        result=None
        if row['status']=='succeeded':
            report=row['result']
            if row['kind']=='import':
                valid={entry['row']:entry['draft'] for entry in report['valid_rows']}; bad={entry['row']:entry for entry in report['row_errors']}
                result={'validation_id':row['id'],'filename':row['input'].get('filename','upload.csv'),'row_count':len(valid)+len(bad),'valid_count':len(valid),'invalid_count':len(bad),'rows':[{'row_number':number,'status':'valid' if number in valid else 'invalid','errors':[] if number in valid else bad[number].get('errors',[{'field':'row','code':'INPUT_INVALID','message':'Invalid CSV row'}]),'draft_preview':valid.get(number)} for number in sorted(set(valid)|set(bad))],'next_cursor':None,'puzzle_version_ids':list(report.get('puzzle_version_ids',[]))}
            else:
                pairs=report['pairs']; metrics=report['metrics']
                result={'manifest_id':row['input']['manifest_id'],'puzzle_version_id':row['input']['puzzle_version_id'],'annotation_locked':bool(row['annotations_locked_at']),'metrics':{'pairwise_accuracy':metrics['pairwise_accuracy'],'total_pairs':len(pairs),'evaluated_pairs':sum(p['human_rating']!='exclude' for p in pairs),'excluded_pairs':sum(p['human_rating']=='exclude' for p in pairs),'by_relation':metrics['group_means']},'pairs':[{'guess':p['guess'],'human_rating':p['human_rating'],'raw_similarity':p['raw_score'],'display_score':p['display_score'],'quality_flag':'excluded' if p['human_rating']=='exclude' else 'evaluated'} for p in pairs],'calibration_id':report['calibration_id'],'quality_scope':'single-puzzle-evaluation','global_G1_Q':False}
        return {'job_id':row['id'],'status':row['status'],'version':row['version'],'progress':{'done':1 if row['status'] in {'succeeded','failed','cancelled'} else 0,'total':1},'result':result,'error':{'code':row['error_code'],'message':'Job failed','retryable':False} if row['error_code'] else None}
