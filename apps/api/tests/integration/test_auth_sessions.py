"""Operator session revocation on real PostgreSQL and real HTTP/WS; never mocked."""
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


def client(db, **extra):
    from fastapi.testclient import TestClient
    from app.main import create_app
    headers = {'Origin':'http://testserver','X-Contract-Version':'LW-C1/1.0.0'}
    headers.update(extra)
    return TestClient(create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **scope:True), headers=headers)


def login(client, username='testoperator', password='private-test-password-123'):
    response = client.post('/api/v1/auth/sessions', json={'username':username,'password':password})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['data']['csrf_token']
    return response.json()['data']


def own_session(client):
    sessions = client.get('/api/v1/auth/sessions').json()['data']['sessions']
    return [item for item in sessions if item['current']][0]['session_id']


def test_revoked_session_cannot_act_and_listing_has_no_token_material(environment):
    db, _ = environment
    operator = client(db)
    data = login(operator)
    listed = operator.get('/api/v1/auth/sessions')
    assert listed.status_code == 200, listed.text
    sessions = listed.json()['data']['sessions']
    assert sessions and all(set(item)=={'session_id','actor_id','created_at','expires_at','current'} for item in sessions)
    assert all(item['actor_id']==data['actor']['actor_id'] for item in sessions)
    assert [item for item in sessions if item['current']]
    session_id = [item for item in sessions if item['current']][0]['session_id']
    assert operator.get('/api/v1/auth/sessions/current').status_code == 200
    revoked = operator.delete(f'/api/v1/auth/sessions/{session_id}')
    assert revoked.status_code == 200 and revoked.json()['data']=={'revoked':True}, revoked.text
    # Revocation is durable: the cookie is still presented but no longer valid.
    assert operator.get('/api/v1/auth/sessions/current').status_code == 401
    assert operator.get('/api/v1/auth/sessions').status_code == 401
    assert operator.delete(f'/api/v1/auth/sessions/{session_id}').status_code == 401
    brand_new = client(db)
    fresh = login(brand_new)
    assert fresh['actor']['actor_id']==data['actor']['actor_id']
    assert brand_new.delete('/api/v1/auth/sessions/does-not-exist').status_code == 404
    assert brand_new.get('/api/v1/auth/sessions/current').status_code == 200


def test_revoking_a_session_closes_its_studio_websocket(environment):
    from starlette.websockets import WebSocketDisconnect
    db, _ = environment
    operator = client(db, **{'X-BE-Extension-Version':'LW-BE/1.1.0'})
    login(operator)
    created = operator.post('/api/v1/live-sessions', json={'label':'Revocation','mode':'rehearsal','run_purpose':'local','rules':{}}, headers={'Idempotency-Key':'revoke-ws'})
    assert created.status_code == 200, created.text
    sid = created.json()['data']['session_id']
    with operator.websocket_connect('/api/v1/studio/ws') as ws:
        ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':'0','be_extension_version':'LW-BE/1.1.0'})
        assert ws.receive_json()['kind']=='resume_ready'
        session_id = own_session(operator)
        assert operator.delete(f'/api/v1/auth/sessions/{session_id}').status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(20): ws.receive_json()
        assert closed.value.code == 4403


def test_revoking_another_operator_session_requires_maintenance_permission(environment):
    db, _ = environment
    from app.modules.auth.service import hash_password, uid
    from app.infrastructure.db import schema as s
    with db.engine.begin() as c:
        restricted = uid()
        c.execute(s.operators.insert().values(id=restricted, username='restricted-operator', password_hash=hash_password('private-test-password-123'), display_name='Restricted', permissions=['session.control']))
    admin = client(db); login(admin)
    other = client(db); login(other, 'restricted-operator')
    other_session = own_session(other)
    # The bootstrap operator holds maintenance.request and may revoke any session.
    assert admin.delete(f'/api/v1/auth/sessions/{other_session}').status_code == 200
    assert other.get('/api/v1/auth/sessions/current').status_code == 401
    # A restricted operator may revoke its own session but never another actor's.
    restricted = client(db); login(restricted, 'restricted-operator')
    admin_session = own_session(admin)
    denied = restricted.delete(f'/api/v1/auth/sessions/{admin_session}')
    assert denied.status_code == 403, denied.text
    own = own_session(restricted)
    assert restricted.delete(f'/api/v1/auth/sessions/{own}').status_code == 200
    assert admin.get('/api/v1/auth/sessions/current').status_code == 200


def test_controlled_operator_disable_stamps_time_and_revokes_sessions(environment):
    db, _ = environment
    from app.modules.auth.service import set_operator_state
    from app.infrastructure.db import schema as s
    operator = client(db)
    login(operator)
    assert operator.get('/api/v1/auth/sessions/current').status_code == 200
    with db.engine.begin() as c:
        actor_id = set_operator_state(c, 'testoperator', True)
    with db.engine.connect() as c:
        row = c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id)).mappings().one()
        assert row['disabled'] is True and row['disabled_at'] is not None
        assert c.execute(sa.select(sa.func.count()).select_from(s.auth_sessions).where(s.auth_sessions.c.actor_id==actor_id, s.auth_sessions.c.revoked_at.is_(None))).scalar_one() == 0
    assert operator.get('/api/v1/auth/sessions/current').status_code == 401
    with db.engine.begin() as c:
        assert set_operator_state(c, 'testoperator', False) == actor_id
    with db.engine.connect() as c:
        row = c.execute(sa.select(s.operators).where(s.operators.c.id==actor_id)).mappings().one()
        assert row['disabled'] is False and row['disabled_at'] is None
    restored = client(db); login(restored)
    assert restored.get('/api/v1/auth/sessions/current').status_code == 200
    with pytest.raises(Exception):
        with db.engine.begin() as c: set_operator_state(c, 'missing-operator', True)


def test_admitted_guess_persists_its_own_window_epoch(environment):
    db, _ = environment
    from app.infrastructure.db import schema as s
    operator = client(db, **{'X-BE-Extension-Version':'LW-BE/1.1.0'})
    login(operator)
    created = operator.post('/api/v1/live-sessions', json={'label':'Epoch','mode':'rehearsal','run_purpose':'local','rules':{}}, headers={'Idempotency-Key':'epoch-create'})
    assert created.status_code == 200, created.text
    sid = created.json()['data']['session_id']
    ticket = operator.post('/api/v1/display-tickets', json={'session_id':sid,'purpose':'primary'}, headers={'Idempotency-Key':'epoch-ticket'}).json()['data']
    display = client(db)
    assert display.post('/api/v1/display-sessions', json={'ticket':ticket['ticket']}).status_code == 200
    with display.websocket_connect('/api/v1/display/ws') as ws:
        ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':'0'})
        assert ws.receive_json()['kind']=='resume_ready'
        started = operator.post(f'/api/v1/live-sessions/{sid}/commands', json={'command':'start','expected_version':1,'args':{'run_purpose':'local'}}, headers={'Idempotency-Key':'epoch-start'})
    assert started.status_code == 200, started.text
    with db.engine.connect() as c:
        row = c.execute(sa.select(s.rounds).where(s.rounds.c.session_id==sid)).mappings().one()
    admitted = operator.post('/api/v1/rehearsal/guesses', json={'session_id':sid,'round_id':row['id'],'short_code':row['short_code'],'word':'苹果','display_name':'Epoch Player'}, headers={'Idempotency-Key':'epoch-guess'})
    assert admitted.status_code == 200, admitted.text
    with db.engine.connect() as c:
        guess = c.execute(sa.select(s.guesses).where(s.guesses.c.id==admitted.json()['data']['guess_id'])).mappings().one()
    assert guess['epoch'] == row['epoch']


def test_display_ticket_revocation_closes_display_websocket(environment):
    from starlette.websockets import WebSocketDisconnect
    db, _ = environment
    operator = client(db, **{'X-BE-Extension-Version':'LW-BE/1.1.0'})
    login(operator)
    created = operator.post('/api/v1/live-sessions', json={'label':'Display revoke','mode':'rehearsal','run_purpose':'local','rules':{}}, headers={'Idempotency-Key':'revoke-display'})
    assert created.status_code == 200, created.text
    sid = created.json()['data']['session_id']
    ticket = operator.post('/api/v1/display-tickets', json={'session_id':sid,'purpose':'primary'}, headers={'Idempotency-Key':'revoke-ticket'}).json()['data']
    display = client(db)
    exchanged = display.post('/api/v1/display-sessions', json={'ticket':ticket['ticket']})
    assert exchanged.status_code == 200, exchanged.text
    with display.websocket_connect('/api/v1/display/ws') as ws:
        ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':'0'})
        assert ws.receive_json()['kind']=='resume_ready'
        assert operator.delete(f"/api/v1/display-tickets/{ticket['ticket_id']}", headers={'Idempotency-Key':'revoke-ticket-delete'}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(20): ws.receive_json()
        assert closed.value.code == 4403
    assert display.get('/api/v1/display-sessions/current').status_code == 403
