"""Real PG small-pool regression for transaction-owned display readiness."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contextlib import ExitStack
import pytest
import sqlalchemy as sa
from test_local_flow import environment,prepared_round,ComponentScorer
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid
from app.modules.rounds.worker import LocalRoundWorker
from app.application import create_app
from app.config import Settings


def application_with_running_round(environment,tmp_path):
    db,actor=environment;existing,body=prepared_round(db,actor,'Transaction display probe')
    settings=Settings(db.settings.database_url,allowed_hosts=('testserver',),allowed_origins=('http://testserver',),analytics_cursor_key=b'a'*32,server_root=tmp_path,source_root=Path(__file__).resolve().parents[5])
    app=create_app(settings,scorer=ComponentScorer());core=app.state.service;core.boot_id=existing.boot_id
    with db.engine.begin() as c:
        display=c.execute(sa.select(s.displays.c.id).where(s.displays.c.session_id==body['session_id'])).scalar_one()
        c.execute(s.connections.insert().values(id=uid(),display_id=display,boot_id=core.boot_id,nonce=uid(),generation=1,ready=True))
    return app,body,display

@pytest.mark.parametrize('pool_size',[1,2])
def test_held_transaction_readiness_uses_no_second_pool_checkout(environment,tmp_path,pool_size):
    app,body,display=application_with_running_round(environment,tmp_path)
    db=app.state.database;db.engine.dispose();small=sa.create_engine(db.engine.url,pool_size=pool_size,max_overflow=0,pool_timeout=.1,hide_parameters=True);db.engine=small
    checkouts=[];sa.event.listen(small,'checkout',lambda *args:checkouts.append(True))
    try:
        with ExitStack() as stack:
            for _ in range(pool_size-1):stack.enter_context(small.connect())
            with small.begin() as c:
                session=app.state.service.session(c,body['session_id'])
                count=len(checkouts)
                readiness=app.state.service.readiness.evaluate(c,session,['session.control'])
                assert readiness['checks']['display']['status']=='pass'
                assert len(checkouts)==count, 'held transaction must not acquire another connection'
    finally:small.dispose()

@pytest.mark.parametrize('pool_size',[1,2])
def test_round_worker_keeps_ready_round_and_interrupts_actual_revocation(environment,tmp_path,pool_size):
    app,body,display=application_with_running_round(environment,tmp_path)
    db=app.state.database;db.engine.dispose();small=sa.create_engine(db.engine.url,pool_size=pool_size,max_overflow=0,pool_timeout=.1,hide_parameters=True);db.engine=small
    try:
        worker=LocalRoundWorker(app.state.service)
        with ExitStack() as stack:
            for _ in range(pool_size-1):stack.enter_context(small.connect())
            worker.tick()
        with small.begin() as c:
            row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==body['round_id'])).mappings().one()
            assert row['status']=='RUNNING', 'pool contention must not masquerade as display dependency loss'
            c.execute(s.displays.update().where(s.displays.c.id==display).values(revoked=True))
        worker.tick()
        with small.connect() as c:
            row=c.execute(sa.select(s.rounds).where(s.rounds.c.id==body['round_id'])).mappings().one()
            assert row['end_reason']=='INTERRUPTED'
            assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==body['session_id'])).scalar_one()=='RECOVERY_REQUIRED'
    finally:small.dispose()


def test_custom_display_probe_replacement_keeps_legacy_keyword_interface(environment,tmp_path):
    app,body,display=application_with_running_round(environment,tmp_path);calls=[]
    def custom(*,session_id,display_instance_id):calls.append((session_id,display_instance_id));return False
    app.state.service.readiness.display_probe=custom
    with app.state.database.engine.begin() as c:
        session=app.state.service.session(c,body['session_id'])
        ready=app.state.service.readiness.evaluate(c,session,['session.control'])
        assert ready['checks']['display']['status']=='fail'
    assert calls==[(body['session_id'],display)]
    app.state.database.engine.dispose()
