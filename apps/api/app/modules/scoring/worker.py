"""Bounded parallel durable score claims; inference never owns SQL locks."""
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.auth.service import now

class ScoreWorker:
    def __init__(self,core,concurrency=8):
        if type(concurrency) is not int or not 1<=concurrency<=32: raise ValueError('SCORE_CONCURRENCY_INVALID')
        self.core=core; self.db=core.db;self.concurrency=concurrency
        self.stop_event=threading.Event(); self.thread=None; self.last_error=None
    def start(self):
        self.thread=threading.Thread(target=self.run,name='wordheat-score-jobs',daemon=True); self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=3)
    @staticmethod
    def available():
        return sa.or_(s.score_jobs.c.status=='queued',sa.and_(s.score_jobs.c.status=='waiting',sa.or_(s.score_jobs.c.lease_until.is_(None),s.score_jobs.c.lease_until<now())),sa.and_(s.score_jobs.c.status=='running',s.score_jobs.c.lease_until<now()))
    def run(self):
        inflight={}
        with ThreadPoolExecutor(max_workers=self.concurrency,thread_name_prefix='wordheat-score') as executor:
            while not self.stop_event.wait(.02):
                for gid,future in list(inflight.items()):
                    if not future.done():continue
                    try:future.result()
                    except Exception:self.last_error='SCORE_WORKER_FAILED'
                    del inflight[gid]
                free=self.concurrency-len(inflight)
                if not free:continue
                try:
                    with self.db.engine.connect() as c:
                        query=sa.select(s.score_jobs.c.guess_id).where(self.available())
                        if inflight:query=query.where(s.score_jobs.c.guess_id.not_in(inflight))
                        ids=list(c.execute(query.order_by(s.score_jobs.c.created_at).limit(free)).scalars())
                    for gid in ids:inflight[gid]=executor.submit(self.tick,gid)
                except Exception:self.last_error='SCORE_WORKER_FAILED'
            # At most concurrency<=32 tasks exist; each scorer has its own 2s budget.
            for future in inflight.values():future.cancel()
    def tick(self,guess_id=None):
        with self.db.engine.connect() as c:
            query=sa.select(s.score_jobs,s.guesses.c.session_id,s.guesses.c.round_id,s.guesses.c.normalized_word,s.guesses.c.admission_seq,s.participants.c.display_name,s.sessions.c.actor_id).join(s.guesses,s.guesses.c.id==s.score_jobs.c.guess_id).join(s.participants,s.participants.c.id==s.guesses.c.participant_id).join(s.sessions,s.sessions.c.id==s.guesses.c.session_id).where(self.available())
            if guess_id is not None:query=query.where(s.score_jobs.c.guess_id==guess_id)
            row=c.execute(query.order_by(s.score_jobs.c.created_at).limit(1)).mappings().first()
            if not row:return False
            row=dict(row)
        result={'feedback_id':row['guess_id'],'word':row['normalized_word'],'display_name':row['display_name'],'status':'queued','display_score':None,'solved':False,'message':'SCORING_PENDING','admission_seq':str(row['admission_seq'])}
        self.core.score_admitted(row['actor_id'],None,{'round_id':row['round_id'],'session_id':row['session_id']},result)
        return True
