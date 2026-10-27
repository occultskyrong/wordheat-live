"""Standard application factory lifecycle, not the core-only fixture entry."""
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from test_local_flow import environment
from app.config import Settings
from app.application import create_app

class WarmingModel:
    def readiness(self):return {'ready':False,'manifest_id':None}


def test_standard_factory_starts_real_workers_and_stops_them_before_database_dispose(environment,monkeypatch):
    db,actor=environment
    with tempfile.TemporaryDirectory(prefix='wh-assembly-',dir='/private/tmp') as directory:
        root=Path(directory);root.chmod(0o700)
        settings=Settings(db.settings.database_url,allowed_hosts=('testserver',),allowed_origins=('http://testserver',),analytics_cursor_key=b'a'*32)
        settings.server_root=root;settings.source_root=Path(__file__).resolve().parents[5]
        app=create_app(settings,scorer=WarmingModel())
        calls={'maintenance':0,'runtime':0,'monitor':0,'dispatch':0,'history':0}
        def watch(obj,name,label):
            original=getattr(obj,name)
            def invoke(*args,**kwargs):calls[label]+=1;return original(*args,**kwargs)
            monkeypatch.setattr(obj,name,invoke)
        watch(app.state.analytics,'maintenance_tick','maintenance');watch(app.state.runtime_controller,'tick','runtime')
        watch(app.state.connector,'monitor','monitor');watch(app.state.connector,'worker_step','dispatch');watch(app.state.connector_supervisor,'flush_historical_ack','history')
        dispose=app.state.database.engine.dispose
        def checked_dispose(*args,**kwargs):
            assert app.state.runtime_worker.task is None and app.state.analytics_maintenance_worker.task is None
            assert app.state.connector_task.done() and app.state.candidate_resolver.task is None
            assert app.state.native_control_server.listener.fileno()==-1
            assert not app.state.score_worker.thread.is_alive()
            return dispose(*args,**kwargs)
        monkeypatch.setattr(app.state.database.engine,'dispose',checked_dispose)
        with TestClient(app,headers={'Origin':'http://testserver'}) as client:
            assert hasattr(app.state,'analytics_maintenance_worker'), 'MAC-CA-001: maintenance worker is not assembled'
            assert app.state.analytics_maintenance_worker.task is not None
            assert app.state.connector_task is not None and not app.state.connector_task.done()
            assert app.state.runtime_worker.task is not None
            assert app.state.native_control_server.listener is not None
            assert app.state.service.runtime_guard.__self__ is app.state.runtime_controller
            assert app.state.service.official.hooks is app.state.service
            assert app.state.runtime_controller.pairing_bridge is not None
            assert app.state.runtime_controller.pairing_bridge.validator is not None
            assert app.state.runtime_controller.pairing_bridge.credentials is app.state.device_credentials
            assert app.state.service.readiness.runtime_controller is app.state.runtime_controller
            login=client.post('/api/v1/auth/sessions',json={'username':'testoperator','password':'private-test-password-123'})
            assert login.status_code==200
            assert all(value>=1 for value in calls.values()),calls
            client.headers['X-CSRF-Token']=login.json()['data']['csrf_token']
            created=client.post('/api/v1/live-sessions',json={'label':'WARMING preparation','mode':'rehearsal','run_purpose':'local','rules':{}},headers={'Idempotency-Key':'factory-warming-create','X-BE-Extension-Version':'LW-BE/1.1.0'})
            assert created.status_code==200,created.text
        assert app.state.analytics_maintenance_worker.task is None
        assert app.state.runtime_worker.task is None
        assert app.state.connector_task.done()
        assert app.state.native_control_server.listener.fileno()==-1
        assert not app.state.native_control_server.socket_path.exists()
