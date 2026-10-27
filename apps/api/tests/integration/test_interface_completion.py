"""Interface regressions on real isolated PostgreSQL; no model or platform IO."""
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from app.config import Settings,read_fields
from app.infrastructure.db.database import Database
from app.infrastructure.db import schema as s
from app.modules.auth.service import PERMISSIONS,hash_password,uid
from app.modules.connectors import models as m
from app.modules.connectors.coordinator import OfficialCoordinator
from app.modules.connectors.transport import ConnectorSupervisor
from app.modules.sessions.service import RULES


class ComponentScorer:
    def readiness(self):return {'ready':True,'manifest_id':'component-model'}


@pytest.fixture
def scene(postgres_isolation):
    from app.main import create_app
    values=read_fields(Path(os.environ['WORDHEAT_TEST_CONFIG']),{'WORDHEAT_TEST_DATABASE_URL'})
    db=Database(Settings(values['WORDHEAT_TEST_DATABASE_URL'],allowed_hosts=('testserver',),allowed_origins=('http://testserver',)))
    actor=uid();username='interface-'+actor
    with db.engine.begin() as c:
        c.execute(s.operators.insert().values(id=actor,username=username,password_hash=hash_password('component-password-123'),display_name='Component operator',permissions=PERMISSIONS))
    app=create_app(db.settings,scorer=ComponentScorer(),display_probe=lambda **kw:True)
    client=TestClient(app,headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'})
    login=client.post('/api/v1/auth/sessions',json={'username':username,'password':'component-password-123'})
    assert login.status_code==200,login.text
    client.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
    yield SimpleNamespace(db=db,actor=actor,username=username,client=client,app=app)
    db.engine.dispose()


def post(client,path,body,key):return client.post(path,json=body,headers={'Idempotency-Key':key})


def confirmed_import(scene,command='confirm_valid_rows'):
    client=scene.client
    raw='word,aliases,category_id,difficulty,hints,explanation,source_ref\n苹果,[],interface-csv,1,[],水果,owned\n'.encode()
    response=client.post('/api/v1/puzzle-imports',files={'file':('fixture.csv',raw)},headers={'Idempotency-Key':uid()})
    assert response.status_code==202,response.text
    jid=response.json()['data']['job_id'];assert scene.app.state.wordbank_worker.tick()
    report=client.get('/api/v1/puzzle-imports/'+jid).json()['data']
    body={'command':command,'expected_version':report['version'],'args':{'rows':[2]}}
    key=uid();response=post(client,f'/api/v1/puzzle-imports/{jid}/commands',body,key)
    assert response.status_code==200,response.text
    with scene.db.engine.connect() as c:
        job=c.execute(sa.select(s.wordbank_jobs).where(s.wordbank_jobs.c.id==jid)).mappings().one()
    return jid,body,key,response.json()['data'],job['result']['puzzle_version_ids']


def test_logout_uses_static_route_and_revokes_only_current_cookie(scene):
    client=scene.client
    other=TestClient(scene.app,headers={'Origin':'http://testserver'})
    login=other.post('/api/v1/auth/sessions',json={'username':scene.username,'password':'component-password-123'})
    other.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
    old_token=client.cookies.get('lw_operator')
    response=client.delete('/api/v1/auth/sessions/current')
    assert response.status_code==200,response.text
    assert response.json()['data']=={'logged_out':True}
    assert 'max-age=0' in response.headers['set-cookie'].lower()
    assert client.get('/api/v1/auth/sessions/current').status_code==401
    from app.modules.auth.service import token_hash
    with scene.db.engine.connect() as c:
        assert c.execute(sa.select(s.auth_sessions.c.revoked_at).where(s.auth_sessions.c.token_hash==token_hash(old_token))).scalar_one() is not None
    assert other.get('/api/v1/auth/sessions/current').status_code==200
    session=next(item for item in other.get('/api/v1/auth/sessions').json()['data']['sessions'] if item['current'])
    assert other.delete('/api/v1/auth/sessions/'+session['session_id']).json()['data']=={'revoked':True}
    assert other.get('/api/v1/auth/sessions/current').status_code==401


def test_confirm_ids_survive_job_command_and_idempotent_queries(scene):
    jid,body,key,result,created=confirmed_import(scene)
    assert result['state']=={'validation_id':jid,'operation':'confirm_valid_rows','puzzle_version_ids':created}
    assert scene.client.get('/api/v1/puzzle-imports/'+jid).json()['data']['result']['puzzle_version_ids']==created
    assert scene.client.get('/api/v1/commands/'+result['command_id']).json()['data']==result
    assert post(scene.client,f'/api/v1/puzzle-imports/{jid}/commands',body,key).json()['data']==result
    with scene.db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.puzzles).where(s.puzzles.c.category_id=='interface-csv')).scalar_one()==len(created)


def test_legacy_import_cache_is_projected_without_creating_more_drafts(scene):
    jid,body,key,result,created=confirmed_import(scene,command='confirm')
    path=f'/api/v1/puzzle-imports/{jid}/commands'
    legacy=result|{'state':'confirm'}
    with scene.db.engine.begin() as c:
        c.execute(s.idempotency.update().where(s.idempotency.c.actor_id==scene.actor,s.idempotency.c.path==path,s.idempotency.c.key==key).values(response=legacy))
        before=c.execute(sa.select(sa.func.count()).select_from(s.puzzles)).scalar_one()
    queried=scene.client.get('/api/v1/commands/'+result['command_id'])
    assert queried.status_code==200,queried.text
    expected={'validation_id':jid,'operation':'confirm_valid_rows','puzzle_version_ids':created}
    assert queried.json()['data']['state']==expected
    assert post(scene.client,path,body,key).json()['data']['state']==expected
    with scene.db.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.puzzles)).scalar_one()==before


def test_import_ids_require_permission_and_job_ownership(scene):
    jid,body,key,result,created=confirmed_import(scene)
    actor=uid();name='other-'+actor
    with scene.db.engine.begin() as c:c.execute(s.operators.insert().values(id=actor,username=name,password_hash=hash_password('component-password-123'),display_name='Other',permissions=['puzzle.edit']))
    other=TestClient(scene.app,headers={'Origin':'http://testserver'})
    login=other.post('/api/v1/auth/sessions',json={'username':name,'password':'component-password-123'})
    other.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
    for response in [other.get('/api/v1/puzzle-imports/'+jid),other.get('/api/v1/commands/'+result['command_id']),post(other,f'/api/v1/puzzle-imports/{jid}/commands',body,key)]:
        assert response.status_code==403,response.text
        assert all(identity not in response.text for identity in created)
    with scene.db.engine.begin() as c:c.execute(s.operators.update().where(s.operators.c.id==scene.actor).values(permissions=[]))
    response=scene.client.get('/api/v1/commands/'+result['command_id'])
    assert response.status_code==403 and all(identity not in response.text for identity in created)


def official_scene(scene):
    clock=[1000000000];core=scene.app.state.service
    trust=getattr(scene.app.state,'trusted_control',None) or SimpleNamespace(boot_id=core.boot_id,_runtime={},_nonces={})
    coordinator=OfficialCoordinator(scene.db,trust,hooks=core,now_ns=lambda:clock[0]);core.official=coordinator
    if hasattr(scene.app.state,'trusted_control'):core.readiness.connector=coordinator
    sid=uid();scope=dict(app_id='fixture-app',room_id='fixture-room',room_binding_id=uid(),installation_id=scene.db.settings.instance_id,live_profile='beta',authorization_id=uid(),candidate_digest='a'*64,grant_digest='b'*64,run_purpose='test_live')
    stamp='2026-10-27T00:00:00.000Z'
    with scene.db.engine.begin() as c:
        c.execute(s.sessions.insert().values(id=sid,actor_id=scene.actor,label='Official fixture',mode='official',status='CREATED',rules=RULES,**scope))
        c.execute(s.configuration.insert().values(id=uid(),session_id=sid));c.execute(s.selection_state.insert().values(id=uid(),session_id=sid,revision=0))
        c.execute(m.bindings.insert().values(id=uid(),session_id=sid,connector_id='fixture',scope=scope,state='CONNECTED',boot_id=core.boot_id,last_contact_ns=clock[0],lease_deadline_ns=clock[0]+12000000000,lease={'official_ingress_ready':True,'platform_ready':True,'lease_id':'private-lease'},connection={'connection_nonce':'private-nonce','_last_confirmed_at':stamp}))
    ticket=post(scene.client,'/api/v1/display-tickets',{'session_id':sid,'purpose':'primary'},uid()).json()['data']
    display=TestClient(scene.app,headers={'Origin':'http://testserver'})
    assert display.post('/api/v1/display-sessions',json={'ticket':ticket['ticket']}).status_code==200
    return coordinator,core,sid,clock,display


def test_official_http_snapshots_use_same_read_only_transport(scene):
    coordinator,core,sid,clock,display=official_scene(scene)
    with scene.db.engine.connect() as c:before=c.execute(sa.select(s.sessions.c.analytics_commit_counter,s.sessions.c.display_seq,s.sessions.c.studio_seq).where(s.sessions.c.id==sid)).one()
    seen=[]
    def capture(c,cursor,statement,parameters,context,many):seen.append(statement)
    sa.event.listen(scene.db.engine,'before_cursor_execute',capture)
    try:
        public=display.get(f'/api/v1/display/sessions/{sid}/snapshot');studio=scene.client.get(f'/api/v1/studio/sessions/{sid}/snapshot')
    finally:sa.event.remove(scene.db.engine,'before_cursor_execute',capture)
    assert public.status_code==studio.status_code==200,(public.text,studio.text)
    transport=public.json()['data']['transport']
    assert transport=={'state':'CONNECTED','reason_code':'OK','recoverable':False,'last_confirmed_at':'2026-10-27T00:00:00.000Z'}
    assert studio.json()['data']['public_view']['transport']==transport
    assert 'private-nonce' not in public.text+studio.text and 'private-lease' not in public.text+studio.text
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) or 'FOR UPDATE' in sql.upper() for sql in seen)
    with scene.db.engine.connect() as c:assert c.execute(sa.select(s.sessions.c.analytics_commit_counter,s.sessions.c.display_seq,s.sessions.c.studio_seq).where(s.sessions.c.id==sid)).one()==before


def test_official_display_and_studio_heartbeats_use_transport_projection(scene):
    coordinator,core,sid,clock,display=official_scene(scene)
    for client,audience in [(display,'display'),(scene.client,'studio')]:
        snapshot=client.get(f'/api/v1/{audience}/sessions/{sid}/snapshot').json()['data']
        with client.websocket_connect(f'/api/v1/{audience}/ws') as ws:
            ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':snapshot['as_of_seq']})
            assert ws.receive_json()['kind']=='resume_ready'
            frame=ws.receive_json();assert frame['kind']=='heartbeat',frame
            assert frame['transport']['state']=='CONNECTED'
            assert frame['transport']['reason_code']=='OK'
            assert 'private-nonce' not in str(frame) and 'private-lease' not in str(frame)


def test_transport_maps_boot_lease_contact_and_terminal_facts_without_writes(scene):
    coordinator,core,sid,clock,display=official_scene(scene)
    cases=[({'state':'CONNECTING'},'CONNECTING','RELAY_CONNECTING',True),({'state':'RECONNECTING','disconnected_ns':1000000000},'RECONNECTING','RELAY_DISCONNECTED',True),({'state':'CONNECTED','lease_deadline_ns':clock[0]},'FAULTED','LEASE_EXPIRED',False),({'state':'CONNECTED','boot_id':'old-boot'},'FAULTED','BOOT_CHANGED',False),({'state':'GAP','last_error':'RELAY_GAP'},'FAULTED','RELAY_GAP',False),({'state':'STOPPED'},'DISCONNECTED','CONNECTOR_STOPPED',False)]
    for changes,state,reason,recoverable in cases:
        with scene.db.engine.begin() as c:
            c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(state='CONNECTED',boot_id=core.boot_id,lease_deadline_ns=clock[0]+12000000000,disconnected_ns=0,last_error=None))
            c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(**changes))
        with scene.db.read_snapshot() as c:
            session=core.session(c,sid,lock=False)
            projected=core.transport(c,session)
        assert (projected['state'],projected['reason_code'],projected['recoverable'])==(state,reason,recoverable)
    with scene.db.engine.begin() as c:c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(state='CONNECTED',boot_id=core.boot_id,lease_deadline_ns=clock[0]+12000000000,last_contact_ns=clock[0]))
    clock[0]+=6000000000
    with scene.db.read_snapshot() as c:assert core.transport(c,core.session(c,sid,lock=False))['reason_code']=='RELAY_DISCONNECTED'


def test_connection_status_events_follow_actual_transition_once(scene):
    coordinator,core,sid,clock,display=official_scene(scene)
    coordinator.disconnected(sid)
    coordinator.disconnected(sid)
    with scene.db.engine.connect() as c:
        events=list(c.execute(sa.select(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.type=='connection.status')).mappings())
    assert len(events)==2 and {e['audience'] for e in events}=={'DISPLAY','STUDIO'}
    assert all(e['payload']['transport']['state']=='RECONNECTING' for e in events)
    coordinator.disconnected(sid)
    with scene.db.engine.connect() as c:assert c.execute(sa.select(sa.func.count()).select_from(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.type=='connection.status')).scalar_one()==2
    supervisor=ConnectorSupervisor(coordinator,None)
    with scene.db.engine.begin() as c:c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(state='CONNECTED',disconnected_ns=0))
    # A successful contact updates its wall-clock display timestamp, with no transition event flood.
    supervisor.contact(sid,{'connection_nonce':'private-nonce'})
    supervisor.contact(sid,{'connection_nonce':'private-nonce'})
    with scene.db.engine.connect() as c:
        events=list(c.execute(sa.select(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.type=='connection.status').order_by(s.outbox.c.created_at)).mappings())
        assert len(events)==4
        assert sum(e['payload']['transport']['state']=='CONNECTED' for e in events)==2
        assert coordinator.public_transport(c,core.session(c,sid,lock=False))['last_confirmed_at']!='2026-10-27T00:00:00.000Z'


def test_import_validation_and_cancel_return_empty_id_sets(scene):
    raw='word,aliases,category_id,difficulty,hints,explanation,source_ref\n苹果,[],cancel-fixture,1,[],水果,owned\n'.encode()
    response=scene.client.post('/api/v1/puzzle-imports',files={'file':('cancel.csv',raw)},headers={'Idempotency-Key':uid()})
    jid=response.json()['data']['job_id'];assert scene.app.state.wordbank_worker.tick()
    report=scene.client.get('/api/v1/puzzle-imports/'+jid).json()['data']
    assert report['result']['puzzle_version_ids']==[]
    cancelled=post(scene.client,f'/api/v1/puzzle-imports/{jid}/commands',{'command':'cancel','expected_version':report['version'],'args':{}},uid())
    assert cancelled.status_code==200,cancelled.text
    result=cancelled.json()['data']
    assert result['state']=={'validation_id':jid,'operation':'cancel','puzzle_version_ids':[]}
    assert scene.client.get('/api/v1/commands/'+result['command_id']).json()['data']==result
    assert scene.client.get('/api/v1/puzzle-imports/'+jid).json()['data']['status']=='cancelled'


def test_connection_status_event_rollback_and_schema(scene):
    from app.http_contract import EventFrame
    from app.modules.realtime.service import RealtimeService
    coordinator,core,sid,clock,display=official_scene(scene)
    with pytest.raises(RuntimeError,match='rollback probe'):
        with scene.db.engine.begin() as c:
            session=coordinator.lock_session(c,sid)
            coordinator.interrupt(c,session,'LEASE_EXPIRED',enqueue_stop=False)
            raise RuntimeError('rollback probe')
    with scene.db.engine.connect() as c:
        assert c.execute(sa.select(m.bindings.c.state).where(m.bindings.c.session_id==sid)).scalar_one()=='CONNECTED'
        assert c.execute(sa.select(sa.func.count()).select_from(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.type=='connection.status')).scalar_one()==0
    coordinator.disconnected(sid)
    with scene.db.engine.connect() as c:
        event=c.execute(sa.select(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.type=='connection.status',s.outbox.c.audience=='DISPLAY')).mappings().one()
    assert EventFrame.model_validate(RealtimeService.envelope(event,'live')).payload.transport.state=='RECONNECTING'
    assert core.transport(None,{'mode':'rehearsal'})=={'state':'DISCONNECTED','reason_code':'LOCAL_REHEARSAL','recoverable':False,'last_confirmed_at':None}


def test_wire_import_command_and_transport_shapes_are_closed():
    from app.dto import CommandResult,Transport
    from pydantic import ValidationError
    state={'validation_id':'validation','operation':'confirm_valid_rows','puzzle_version_ids':['draft']}
    assert CommandResult.model_validate({'command_id':'command','status':'applied','resource_version':4,'state':state,'error':None}).state.puzzle_version_ids==['draft']
    with pytest.raises(ValidationError):
        Transport.model_validate({'state':'AUTHENTICATING','reason_code':'X','recoverable':False,'last_confirmed_at':None})
    with pytest.raises(ValidationError):
        CommandResult.model_validate({'command_id':'command','status':'applied','resource_version':4,'state':state|{'private_nonce':'forbidden'},'error':None})


def test_standard_factory_streams_connection_events_on_both_contiguous_cursors(scene):
    from app.application import create_app
    settings=Settings(scene.db.settings.database_url,allowed_hosts=('testserver',),allowed_origins=('http://testserver',),analytics_cursor_key=bytes(range(32)))
    app=create_app(settings,scorer=ComponentScorer(),display_probe=lambda **kw:True)
    client=TestClient(app,headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'})
    login=client.post('/api/v1/auth/sessions',json={'username':scene.username,'password':'component-password-123'})
    client.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
    standard=SimpleNamespace(app=app,client=client,db=app.state.database,actor=scene.actor)
    try:
        coordinator,core,sid,clock,display=official_scene(standard)
        public=display.get(f'/api/v1/display/sessions/{sid}/snapshot')
        studio=client.get(f'/api/v1/studio/sessions/{sid}/snapshot')
        assert public.status_code==studio.status_code==200,(public.text,studio.text)
        assert public.json()['data']['transport']['state']=='CONNECTED'
        assert studio.json()['data']['public_view']['transport']==public.json()['data']['transport']
        after_display=int(public.json()['data']['as_of_seq']);after_studio=int(studio.json()['data']['as_of_seq'])
        with display.websocket_connect('/api/v1/display/ws') as dws,client.websocket_connect('/api/v1/studio/ws') as sws:
            for ws,cursor in [(dws,after_display),(sws,after_studio)]:
                ws.send_json({'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':sid,'after_seq':str(cursor)})
                assert ws.receive_json()['kind']=='resume_ready'
            coordinator.disconnected(sid)
            for ws,cursor in [(dws,after_display),(sws,after_studio)]:
                frame=ws.receive_json()
                assert frame['type']=='connection.status' and int(frame['seq'])==cursor+1,frame
                assert frame['payload']['transport']['state']=='RECONNECTING'
                assert set(frame['payload']['transport'])=={'state','reason_code','recoverable','last_confirmed_at'}
                assert 'private-nonce' not in str(frame) and 'private-lease' not in str(frame)
            coordinator.disconnected(sid)
            current=client.get(f'/api/v1/studio/sessions/{sid}/snapshot').json()['data']
            assert int(current['as_of_seq'])==after_studio+1
            assert int(current['public_view']['as_of_seq'])==after_display+1
            assert current['public_view']['transport']['state']=='RECONNECTING'
    finally:app.state.database.engine.dispose()
