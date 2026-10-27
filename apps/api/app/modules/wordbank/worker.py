"""Short claims and frozen evaluation inputs; no inference owns SQL locks."""
import asyncio,base64,threading
from datetime import timedelta
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid,now
from app.modules.wordbank.service import parse_csv,puzzle_snapshot
from app.modules.wordbank.evaluation import evaluate
from app.modules.wordbank.bundle import digest
from app.modules.wordbank import selection

class WordbankWorker:
    def __init__(self,wordbank):
        self.wordbank=wordbank;self.db=wordbank.db;self.boot=wordbank.core.boot_id
        self.stop_event=threading.Event();self.thread=None;self.last_error=None
    def start(self):
        self.thread=threading.Thread(target=self.run,name='wordheat-wordbank',daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=3)
    def run(self):
        while not self.stop_event.wait(.2):
            try:self.tick();self.last_error=None
            except Exception:self.last_error='WORDBANK_WORKER_FAILED'
    def check(self,c,row):
        self.wordbank.authorize(c,row['actor_id'],'scoring.evaluate' if row['kind']=='evaluation' else 'puzzle.edit')
        if self.wordbank.core.runtime_guard:self.wordbank.core.runtime_guard(c,action='prepare')
        if row['kind']=='evaluation':
            body=row['input'];puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==body['puzzle_version_id'])).mappings().first()
            if not puzzle or body.get('puzzle_input_sha256')!=digest(puzzle_snapshot(puzzle)) or body.get('evaluation_input_sha256')!=digest({k:v for k,v in body.items() if k!='evaluation_input_sha256'}) or getattr(self.wordbank.core.scorer,'manifest_id',None)!=body['manifest_id'] or getattr(self.wordbank.core.scorer,'manifest_sha256',None)!=body.get('model_manifest_sha256'):raise DomainError('RESULT_STALE','Frozen evaluation input changed',409)
    def tick(self):
        with self.db.engine.begin() as c:
            selection.catalog_lock(c)
            row=c.execute(sa.select(s.wordbank_jobs).where(sa.or_(s.wordbank_jobs.c.status=='queued',sa.and_(s.wordbank_jobs.c.status=='running',s.wordbank_jobs.c.lease_until<now()))).order_by(s.wordbank_jobs.c.created_at).limit(1).with_for_update(skip_locked=True)).mappings().first()
            if not row:return False
            row=dict(row);token=uid()
            try:self.check(c,row)
            except DomainError as error:
                c.execute(s.wordbank_jobs.update().where(s.wordbank_jobs.c.id==row['id']).values(status='failed',error_code=error.code,lease_until=None,version=s.wordbank_jobs.c.version+1));return True
            c.execute(s.wordbank_jobs.update().where(s.wordbank_jobs.c.id==row['id']).values(status='running',claim_boot=self.boot,claim_token=token,lease_until=now()+timedelta(seconds=120),version=s.wordbank_jobs.c.version+1))
            puzzle=None
            if row['kind']=='evaluation':
                puzzle=dict(row['input']['frozen_puzzle'])
                if row['input']['split']!='calibration':
                    calibration=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==row['input'].get('calibration_job_id'),s.wordbank_jobs.c.status=='succeeded')).mappings().first()
                    if calibration:puzzle|={'calibration':calibration['result']['calibration'],'calibration_id':calibration['result']['calibration_id'],'calibration_binding':calibration['result']}
        result={};error=None
        try:
            if row['kind']=='import':
                valid,errors=parse_csv(base64.b64decode(row['input']['csv_base64'],validate=True));result={'valid_rows':valid,'row_errors':errors,'confirmed_rows':[]}
            else:
                async def compute():
                    async with asyncio.timeout(90):return await evaluate(self.wordbank.core.scorer,puzzle,row['input'])
                result=asyncio.run(compute());result['calibration_id']=uid() if row['input']['split']=='calibration' else puzzle.get('calibration_id')
        except DomainError as exc:error=exc.code
        except Exception as exc:error=getattr(exc,'code','SCORER_EVALUATION_FAILED' if row['kind']=='evaluation' else 'IMPORT_VALIDATION_FAILED')
        with self.db.engine.begin() as c:
            selection.catalog_lock(c)
            current=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==row['id']).with_for_update()).mappings().one()
            if current['status']!='running' or current['claim_token']!=token:return True
            try:self.check(c,current)
            except DomainError as exc:error=exc.code
            c.execute(s.wordbank_jobs.update().where(s.wordbank_jobs.c.id==row['id'],s.wordbank_jobs.c.status=='running',s.wordbank_jobs.c.claim_token==token).values(status='failed' if error else 'succeeded',result=result if not error else {},error_code=error,progress=100,lease_until=None,version=s.wordbank_jobs.c.version+1))
        return True
