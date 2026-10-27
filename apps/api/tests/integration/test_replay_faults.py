"""Replay system failures preserve the committed prefix; real isolated PostgreSQL."""
from datetime import timedelta
import pytest
import sqlalchemy as sa
from test_replays import environment,prepared_replay
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import now

class ReadyButTimeout:
    def readiness(self):return {'ready':True,'manifest_id':'reviewed-manifest'}
    async def score(self,**kwargs):raise DomainError('SCORER_TIMEOUT','Synthetic mid-inference timeout',503)

def assert_fault(db,jid,sid,cursor=0,applied=0,points=None):
    with db.engine.connect() as c:
        job=c.execute(sa.select(s.replay_jobs).where(s.replay_jobs.c.id==jid)).mappings().one()
        roundrow=c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
        assert job['status']=='failed' and job['state']=='FAILED'
        assert job['cursor']==cursor and job['events_applied']==applied and job['events_rejected']==0
        assert job['error_code']=='SCORER_TIMEOUT'
        assert roundrow['end_reason']=='INTERRUPTED'
        if points is not None:assert c.execute(sa.select(sa.func.coalesce(sa.func.sum(s.ledger.c.delta),0)).where(s.ledger.c.round_id==roundrow['id'])).scalar_one()==points

def test_ready_scorer_timeout_is_terminal_replay_fault(environment):
    db,actor=environment;client,jid,sid=prepared_replay(db,'ready-inference-timeout',[{'source_id':'one','offset_ms':1000,'guess':'香蕉'}])
    client.app.state.service.scorer=ReadyButTimeout()
    client.app.state.replay_worker.tick();client.app.state.replay_worker.tick()
    assert_fault(db,jid,sid,points=0)

@pytest.mark.parametrize('phase',['pending','waiting'])
def test_recovered_score_failure_does_not_advance_replay_cursor(environment,monkeypatch,phase):
    from app.modules.scoring.worker import ScoreWorker
    db,actor=environment;client,jid,sid=prepared_replay(db,'recover-score-'+phase,[{'source_id':'one','offset_ms':1000,'guess':'香蕉'}])
    core=client.app.state.service;worker=client.app.state.replay_worker;core.scorer=ReadyButTimeout()
    original=core.score_admitted
    monkeypatch.setattr(core,'score_admitted',lambda actor,key,body,result,path='/api/v1/replays':result)
    worker.tick()
    monkeypatch.setattr(core,'score_admitted',original)
    with db.engine.begin() as c:
        gid=c.execute(sa.select(s.replay_events.c.feedback_id).where(s.replay_events.c.job_id==jid)).scalar_one()
        # Controlled lease recovery; no wall-clock sleep or alteration of rate limits.
        c.execute(s.replay_jobs.update().where(s.replay_jobs.c.id==jid).values(lease_until=now()-timedelta(seconds=1)))
        if phase=='waiting':c.execute(s.score_jobs.update().where(s.score_jobs.c.guess_id==gid).values(status='waiting',lease_until=None))
    ScoreWorker(core).tick()
    worker.tick();worker.tick()
    assert_fault(db,jid,sid,points=0)

def test_later_timeout_preserves_successful_prefix_and_ledger(environment):
    db,actor=environment;client,jid,sid=prepared_replay(db,'committed-before-timeout',[{'source_id':'one','offset_ms':1000,'guess':'香蕉'},{'source_id':'two','offset_ms':3000,'guess':'芒果'}])
    class FirstSucceeds(ReadyButTimeout):
        calls=0
        async def score(self,**kwargs):
            self.calls+=1
            if self.calls==1:return 80.
            return await super().score(**kwargs)
    client.app.state.service.scorer=FirstSucceeds()
    worker=client.app.state.replay_worker;worker.tick();worker.tick();worker.tick()
    assert_fault(db,jid,sid,cursor=1,applied=1,points=5)
