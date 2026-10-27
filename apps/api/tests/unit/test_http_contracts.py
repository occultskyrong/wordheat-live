"""Schema checks only: no lifespan, configuration file or database connection."""
import pytest
from pydantic import ValidationError
from app.config import Settings
from app.application import create_app

@pytest.fixture()
def application():
    app=create_app(Settings('postgresql+psycopg://schema:inert-schema-only@127.0.0.1/wordheat_schema_only',analytics_cursor_key=bytes(range(32)),allowed_hosts=('testserver',),allowed_origins=('http://testserver',)))
    yield app
    app.state.database.engine.dispose()

def test_every_registered_http_success_is_concrete_and_422_is_c1(application):
    schema=application.openapi()
    for path,operations in schema['paths'].items():
        for method,operation in operations.items():
            if method not in {'get','post','patch','delete','put'}: continue
            for status,response in operation['responses'].items():
                if status.startswith('2'):
                    assert response.get('content'),(method,path,status)
                    for media,content in response['content'].items():
                        shape=content.get('schema'); assert shape,(method,path,status,media)
                        if media=='application/json': assert '$ref' in shape or 'anyOf' in shape,(method,path,shape)
            response=operation['responses']['422']
            assert response['content']['application/json']['schema']=={'$ref':'#/components/schemas/C1ErrorEnvelope'}
    assert 'HTTPValidationError' not in schema['components']['schemas']

def test_known_envelopes_and_analytics_csv_are_not_arbitrary_objects(application):
    schema=application.openapi()
    for path,method in [('/api/v1/auth/sessions','post'),('/api/v1/live-sessions/{session_id}','get'),('/api/v1/rounds/{round_id}/private-state','get'),('/api/v1/puzzle-imports/{job_id}','get'),('/api/v1/scoring-evaluations/{job_id}','get'),('/api/v1/replays/{job_id}','get'),('/api/v1/live-sessions/{session_id}/analytics','get'),('/api/v1/display/receipts','post')]:
        shape=schema['paths'][path][method]['responses']['200']['content']['application/json']['schema']
        assert '$ref' in shape
        model=schema['components']['schemas'][shape['$ref'].split('/')[-1]]
        assert model['properties']['data']!={'type':'object'}
    csv=schema['paths']['/api/v1/live-sessions/{session_id}/analytics']['get']['responses']['200']['content']['text/csv']['schema']
    assert csv=={'type':'string','format':'binary'}
    assert '/api/v1/display/ws' not in schema['paths']
    assert '/api/v1/display/ws' in schema['x-websocket-protocols']

def test_nested_command_state_rejects_unknown_shape():
    from app.dto import CommandResult
    with pytest.raises(ValidationError): CommandResult(command_id='c',status='applied',resource_version=1,state={'made_up':True},error=None)

def test_c1_error_model_and_analytics_models_match_known_payloads():
    from app.http_contract import C1ErrorEnvelope,AnalyticsAggregate
    result=C1ErrorEnvelope.model_validate({'error':{'code':'INPUT_INVALID','message':'Invalid','retryable':False,'details':{}},'request_id':'r','contract_version':'LW-C1/1.0.0'})
    assert result.error.code=='INPUT_INVALID'
    from app.modules.analytics.service import calculate,AnalyticsService,CursorSigner
    from datetime import datetime,timezone
    payload=calculate({'id':'s','mode':'rehearsal','run_purpose':'local','status':'CREATED','analytics_commit_counter':0,'studio_seq':0},[],[],[],[],[],{}, {},datetime.now(timezone.utc))
    payload|={'snapshot_id':'snap','expires_at':'2026-10-26T00:00:00.000Z'}
    service=AnalyticsService.__new__(AnalyticsService); service.signer=CursorSigner(bytes(range(32)))
    observed=service.aggregate(payload,'s')
    assert AnalyticsAggregate.model_validate(observed).participants_count==0

def test_http_known_auth_and_validation_envelopes_without_database(application,monkeypatch):
    from fastapi.testclient import TestClient
    def forbidden_connection(*args,**kwargs): raise AssertionError('Schema test must not connect to PG')
    monkeypatch.setattr(application.state.database.engine,'connect',forbidden_connection)
    monkeypatch.setattr(application.state.auth,'operator',lambda *args,**kwargs:{'actor_id':'a','display_name':'Operator','permissions':['session.control'],'csrf_token':'csrf','expires_at':'2026-10-26T00:00:00.000Z'})
    client=TestClient(application,headers={'Origin':'http://testserver'})
    response=client.get('/api/v1/auth/sessions/current')
    assert response.status_code==200 and response.json()['data']['actor']['actor_id']=='a'
    response=client.post('/api/v1/auth/sessions',json={'username':5})
    assert response.status_code==422 and response.json()['error']['code']=='INPUT_INVALID'
    assert set(response.json())=={'error','request_id','contract_version'}

def test_analytics_adapter_executes_strict_projection_without_database(application,monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime,timezone
    from fastapi.testclient import TestClient
    from app.modules.analytics.service import calculate
    payload=calculate({'id':'s','mode':'rehearsal','run_purpose':'local','status':'CREATED','analytics_commit_counter':0,'studio_seq':0},[],[],[],[],[],{}, {},datetime.now(timezone.utc))
    payload|={'snapshot_id':'snap','expires_at':'2026-10-26T00:00:00.000Z'}
    @contextmanager
    def connection(): yield object()
    @contextmanager
    def pinned(*args): yield payload
    analytics=application.state.analytics
    monkeypatch.setattr(application.state.database.engine,'connect',connection)
    monkeypatch.setattr(application.state.auth,'operator',lambda *args,**kwargs:{'actor_id':'a'})
    monkeypatch.setattr(analytics,'authorize',lambda *args:None)
    monkeypatch.setattr(analytics,'snapshot',lambda *args,**kwargs:payload)
    monkeypatch.setattr(analytics,'pinned',pinned)
    client=TestClient(application,headers={'Origin':'http://testserver','X-BE-Extension-Version':'LW-BE/1.1.0'},cookies={'lw_operator':'synthetic-test-token'})
    response=client.get('/api/v1/live-sessions/s/analytics')
    assert response.status_code==200,response.text
    assert response.json()['data']['snapshot_id']=='snap'
    for path,params in [('/api/v1/live-sessions/s/analytics',{'snapshot_id':'snap','section':'minutes'}),('/api/v1/live-sessions/s/analytics/rounds',{'snapshot_id':'snap'})]:
        response=client.get(path,params=params)
        assert response.status_code==200,response.text
        assert response.json()['data']['items']==[]

def test_analytics_legacy_missing_required_fields_is_not_silently_relaxed():
    from app.http_contract import RoundAnalyticsRow
    with pytest.raises(ValidationError):
        RoundAnalyticsRow.model_validate({'round_id':'r','hints_revealed':None,'difficulty':None})
