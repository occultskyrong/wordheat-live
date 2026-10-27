"""Short code allocation on real PostgreSQL; the fast path must never fake exhaustion."""
import os
from pathlib import Path
import pytest
import sqlalchemy as sa

class ComponentScorer:
    def readiness(self): return {'ready':True,'manifest_id':'reviewed-manifest'}
    def score(self,**kwargs):
        from app.errors import DomainError
        raise DomainError('SCORER_NOT_READY','Component scorer only observes readiness',503)

"""Requires the explicitly provisioned dedicated PostgreSQL roles, never SQLite."""

@pytest.fixture(scope='module')
def environment():
    from app.config import read_fields, Settings
    from app.infrastructure.db.database import Database
    from app.infrastructure.db.migrate import migrate
    from app.modules.auth.service import bootstrap_operator
    from app.infrastructure.db import schema as s
    path = os.environ.get('WORDHEAT_TEST_CONFIG')
    if not path: pytest.skip('WORDHEAT_TEST_CONFIG dedicated PG configuration required')
    fields = read_fields(Path(path), {'WORDHEAT_TEST_DATABASE_URL','WORDHEAT_TEST_MIGRATION_DATABASE_URL'})
    run, migration = fields['WORDHEAT_TEST_DATABASE_URL'], fields['WORDHEAT_TEST_MIGRATION_DATABASE_URL']
    migrate(migration, 'wordheat-local-development', 'wordheat_test_app')
    db = Database(Settings(run, allowed_hosts=('testserver',), allowed_origins=('http://testserver',)))
    db.verify_runtime()
    with db.engine.begin() as c:
        for table in reversed(s.metadata.sorted_tables):
            if table.name not in {'service_instance','wordbank_catalog'}: c.execute(table.delete())
        actor = bootstrap_operator(c, 'testoperator', 'private-test-password-123', 'Operator')
        c.execute(s.puzzles.insert().values(id='pv-test', puzzle_id='p-test', word='苹果', aliases=['林檎'], category_id='fruit', difficulty=1, explanation='测试题', status='published', manifest_id='reviewed-manifest', calibration_id='reviewed-calibration', evaluation_id='reviewed-evaluation', approved_by=actor))
    yield db, actor
    db.engine.dispose()


def client(db):
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **scope:True), headers={'Origin':'http://testserver','X-Contract-Version':'LW-C1/1.0.0','X-BE-Extension-Version':'LW-BE/1.1.0'})


def open_round(db, operator, sid, key):
    ticket = operator.post('/api/v1/display-tickets', json={'session_id':sid,'purpose':'primary'}, headers={'Idempotency-Key':key+'-ticket'}).json()['data']
    display = client(db)
    assert display.post('/api/v1/display-sessions', json={'ticket':ticket['ticket']}).status_code == 200
    started = operator.post(f'/api/v1/live-sessions/{sid}/commands', json={'command':'start','expected_version':1,'args':{'run_purpose':'local'}}, headers={'Idempotency-Key':key+'-start'})
    assert started.status_code == 200, started.text
    with db.engine.connect() as c:
        from app.infrastructure.db import schema as s
        return c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()


def test_round_code_comes_from_the_published_alphabet(environment):
    db, _ = environment
    operator = client(db)
    login = operator.post('/api/v1/auth/sessions', json={'username':'testoperator','password':'private-test-password-123'})
    operator.headers['X-CSRF-Token'] = login.json()['data']['csrf_token']
    sid = operator.post('/api/v1/live-sessions', json={'label':'Codes','mode':'rehearsal','run_purpose':'local','rules':{}}, headers={'Idempotency-Key':'codes-create'}).json()['data']['session_id']
    row = open_round(db, operator, sid, 'codes')
    assert len(row['short_code']) == 4 and set(row['short_code']) <= set('ABCDEFGHJKLMNPQRSTUVWXYZ23456789')


def test_random_probe_miss_falls_back_to_a_free_code_instead_of_false_exhaustion(environment, monkeypatch):
    db, _ = environment
    from app.infrastructure.db import schema as s
    from app.modules.sessions import service as core
    operator = client(db)
    login = operator.post('/api/v1/auth/sessions', json={'username':'testoperator','password':'private-test-password-123'})
    operator.headers['X-CSRF-Token'] = login.json()['data']['csrf_token']
    with db.engine.begin() as c:
        actor = c.execute(sa.select(s.operators.c.id)).scalar_one()
        c.execute(s.sessions.insert().values(id='session-occupied',actor_id=actor,label='Occupied',mode='rehearsal',run_purpose='local',status='ENDED',rules={}))
        puzzle = c.execute(sa.select(s.puzzles.c.id).where(s.puzzles.c.puzzle_id=='p-test')).scalar_one()
        # One retained round occupies the only code the degenerate probe can produce.
        c.execute(s.rounds.insert().values(id='round-occupied',session_id='session-occupied',puzzle_version_id=puzzle,status='REVEALED',short_code='AAAA',epoch=1,boot_id='test',frozen_puzzle={'word':'苹果','aliases':[],'category_id':'fruit','difficulty':1},frozen_rules={}))
    monkeypatch.setattr(core.secrets,'choice',lambda sequence:'A')
    sid = operator.post('/api/v1/live-sessions', json={'label':'Fallback','mode':'rehearsal','run_purpose':'local','rules':{}}, headers={'Idempotency-Key':'fallback-create'}).json()['data']['session_id']
    row = open_round(db, operator, sid, 'fallback')
    assert row['short_code'] == 'AAAB'
