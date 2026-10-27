"""Coordinator seams against the current core. All provisioned state is synthetic.

These tests never import live gate passes and never admit synthetic production.
"""
import asyncio
from datetime import datetime,timedelta,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import threading
import time
from uuid import uuid4
from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import bootstrap_operator
from app.modules.sessions.service import LocalService,RULES
from app.modules.connectors import models as m
from app.modules.connectors.coordinator import OfficialCoordinator
from app.modules.connectors.transport import ConnectorSupervisor,RelayControlPort
from test_trusted_control import database


def component(database,*,now_ns=None):
    sid=uuid4().hex;boot='synthetic-'+uuid4().hex
    class TrustProbe:
        boot_id=boot
        def require_current(self,c,session):return None
    owner=LocalService(database);trust=TrustProbe()
    class ClockCoordinator(OfficialCoordinator):
        def require_current(self,c,session,round=None,*,result=False):return None
    coordinator=ClockCoordinator(database,trust,hooks=owner,now_ns=now_ns or time.monotonic_ns);owner.official=coordinator
    scope=dict(app_id='synthetic-app',room_id=sid,room_binding_id=sid,installation_id=database.settings.instance_id,live_profile='beta',authorization_id=sid,candidate_digest='0'*64,grant_digest='1'*64,run_purpose='test_live')
    with database.engine.begin() as c:
        actor=c.execute(sa.select(s.operators.c.id)).scalar_one_or_none()
        if actor is None:actor=bootstrap_operator(c,'synthetic-'+sid,'synthetic-test-password-123','Synthetic operator')
        c.execute(s.sessions.insert().values(id=sid,actor_id=actor,label='Synthetic control seam',mode='official',run_purpose='test_live',status='CREATED',rules=RULES,**{k:v for k,v in scope.items() if k!='run_purpose'}))
        c.execute(s.configuration.insert().values(id=uuid4().hex,session_id=sid));c.execute(s.selection_state.insert().values(id=uuid4().hex,session_id=sid,revision=0))
        c.execute(m.room_bindings.insert().values(id=sid,app_id=scope['app_id'],room_id=sid,installation_id=database.settings.instance_id,connector_id='synthetic',status='pass',expires_at=datetime.now(timezone.utc)+timedelta(minutes=1),authorization={'provider_test':True}))
        c.execute(m.bindings.insert().values(id=sid,session_id=sid,connector_id='synthetic',scope=scope,state='CONNECTED',boot_id=boot,last_contact_ns=coordinator.now_ns(),lease_deadline_ns=coordinator.now_ns()+100000000000,lease={'owner_generation':'1'}))
    return coordinator,owner,sid,actor


def test_controlled_clock_enforces_six_second_disconnect_and_persistent_stop(database):
    clock=[1000000000];coordinator,owner,sid,actor=component(database,now_ns=lambda:clock[0])
    clock[0]=6999999999;coordinator.monitor()
    with database.engine.connect() as c:assert c.execute(sa.select(m.bindings.c.state).where(m.bindings.c.session_id==sid)).scalar_one()=='CONNECTED'
    clock[0]=7000000000;coordinator.monitor()
    with database.engine.connect() as c:
        assert c.execute(sa.select(m.bindings.c.last_error).where(m.bindings.c.session_id==sid)).scalar_one()=='RELAY_DISCONNECTED'
        assert coordinator.stop_projection(c)['platform_stop_pending'] is True
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id==sid)).scalar_one()=='RECOVERY_REQUIRED'


def test_transport_drain_timeout_and_command_scope_are_persistent(database):
    clock=[1000000000];coordinator,owner,sid,actor=component(database,now_ns=lambda:clock[0])
    with database.engine.begin() as c:
        session=coordinator.lock_session(c,sid);command=coordinator.queue(c,session,'close',{'reason':'TIMEOUT'})
    clock[0]=6000000000;coordinator.monitor()
    with database.engine.connect() as c:
        result=coordinator.lookup_command(c,command['command_id'],actor,['session.control'])
        assert result['status']=='failed' and result['error']['code']=='TRANSPORT_DRAIN_TIMEOUT'
        with pytest.raises(DomainError):coordinator.lookup_command(c,command['command_id'],'other-actor',['session.control'])


def test_half_second_monitor_runs_while_real_loopback_http_is_delayed(database):
    coordinator,owner,sid,actor=component(database);entered=threading.Event();finished=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            entered.set();time.sleep(1.5);finished.set()
            raw=b'{"error":{"code":"RELAY_UNAVAILABLE"}}'
            try:
                self.send_response(503);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    registry={'synthetic':{'url':f'ws://127.0.0.1:{http.server_port}/relay/v1/streams','test_only':True}}
    port=RelayControlPort(registry,lambda scope:'synthetic-device-material',test_mode=True);coordinator.port=port
    with database.engine.begin() as c:coordinator.queue(c,coordinator.lock_session(c,sid),'prepare')
    ticks=[];original=coordinator.monitor
    def observed():ticks.append(time.monotonic());original()
    coordinator.monitor=observed
    async def run():
        stop=asyncio.Event();task=asyncio.create_task(ConnectorSupervisor(coordinator,port).run(stop))
        try:
            until=time.monotonic()+2
            while not entered.is_set():
                assert time.monotonic()<until;await asyncio.sleep(.01)
            await asyncio.sleep(1.05)
            assert not finished.is_set() and len(ticks)>=2
        finally:
            stop.set();await asyncio.wait_for(task,4)
    try:asyncio.run(run())
    finally:http.shutdown();http.server_close();thread.join(3)


def test_official_admission_sampled_commit_latency_like_rehearsal(database):
    """Official deliveries must own commit latency samples, not only rehearsal."""
    from app.modules.analytics.service import AnalyticsService
    from app.modules.analytics import models as am
    coordinator,owner,sid,actor=component(database)
    owner.analytics=AnalyticsService(database,b'x'*32)
    moment=datetime.now(timezone.utc);pid=uuid4().hex;rid=uuid4().hex
    with database.engine.begin() as c:
        c.execute(s.puzzles.insert().values(id=pid,puzzle_id=pid,word='苹果',aliases=['林檎'],category_id='fixture',difficulty=1,explanation='fixture',status='published',manifest_id='reviewed-manifest',calibration_id='reviewed-calibration',evaluation_id='reviewed-evaluation',approved_by=actor))
        c.execute(s.rounds.insert().values(id=rid,session_id=sid,puzzle_version_id=pid,status='RUNNING',short_code='ABCD',epoch=1,boot_id='synthetic-boot',deadline_at=moment+timedelta(minutes=5),frozen_puzzle={'word':'苹果','aliases':['林檎'],'category_id':'fixture','difficulty':1,'manifest_id':'reviewed-manifest'},frozen_rules=RULES))
        c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(state='CONNECTED',stream_id='stream-1',persisted_seq=1,admitted_seq=0,lease={'owner_generation':'1'}))
        c.execute(m.deliveries.insert().values(id=uuid4().hex,session_id=sid,stream_id='stream-1',stream_seq=1,immutable_sha256='0'*64,raw_body=b'',state='persisted',body={'type':'comment','eligible':True,'round_id':rid,'window_epoch':'1','stream_id':'stream-1','stream_seq':'1','original_generation':'1','run_purpose':'test_live','source_mode':'synthetic','provider_test':True,'relay_received_at':moment.isoformat(timespec='milliseconds').replace('+00:00','Z'),'payload':{'source_msg_id':'msg-1','participant_key':'viewer-1','display_name':'Viewer','content':{'text':'猜 ABCD 苹果'}}}))
    assert coordinator.process_one(sid) is True
    with database.engine.connect() as c:
        guess=c.execute(sa.select(s.guesses).where(s.guesses.c.session_id==sid)).mappings().one()
        payload=c.execute(sa.select(am.latencies.c.payload).where(am.latencies.c.guess_id==guess['id'])).scalar_one_or_none() or {}
    assert guess['epoch']==1
    assert 'admission_commit_mono_ns' in payload and 'feedback_commit_mono_ns' in payload
    # Leave no session for the shared monitor loop to interrupt in later tests.
    with database.engine.begin() as c: c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(state='STOPPED'))


def test_thirty_second_preparation_budget_is_persistent(database):
    """The 30s transport budget is distinct from the 5s drain and 6s disconnect."""
    clock=[1000000000];coordinator,owner,sid,actor=component(database,now_ns=lambda:clock[0])
    with database.engine.begin() as c:
        session=coordinator.lock_session(c,sid);command=coordinator.queue(c,session,'prepare',{'reason':'budget probe'})
    def advance(seconds):
        clock[0]+=seconds*1000000000
        with database.engine.begin() as c:
            c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(last_contact_ns=clock[0],lease_deadline_ns=clock[0]+100000000000))
        coordinator.monitor()
    advance(29)
    with database.engine.connect() as c:
        assert c.execute(sa.select(m.intents.c.status).where(m.intents.c.command_id==command['command_id'])).scalar_one()=='pending'
    advance(2)
    with database.engine.connect() as c:
        row=c.execute(sa.select(m.intents).where(m.intents.c.command_id==command['command_id'])).mappings().one()
        assert row['status']=='failed' and row['last_error']=='CONNECTOR_PREPARATION_TIMEOUT'
        assert c.execute(sa.select(m.bindings.c.state).where(m.bindings.c.session_id==sid)).scalar_one()=='INTERRUPTED'
