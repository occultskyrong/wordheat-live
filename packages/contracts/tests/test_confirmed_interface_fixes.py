"""Synthetic contract probes for the four confirmed interface corrections."""
import copy,json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parents[3]
PACKAGE=ROOT/'packages/contracts'
# date-time is an annotation unless an explicit checker is selected. Register
# only this test instance's stdlib calendar validation, with no optional plugin
# dependency and no change to the application's HTTP timestamp parser.
FORMATS=FormatChecker()
@FORMATS.checks('date-time',raises=(ValueError,TypeError))
def valid_date_time(value):
    if not isinstance(value,str):return True
    from datetime import datetime
    return datetime.fromisoformat(value.replace('Z','+00:00')).utcoffset() is not None


def api():return json.loads((PACKAGE/'openapi.yaml').read_text())
def schema_validator(name,document=None):
    document=document or api()
    return Draft202012Validator({'$ref':'#/components/schemas/'+name,'components':document['components']},format_checker=FORMATS)

@pytest.mark.parametrize('ids',[[],['draft-1'],['draft-1','draft-2']])
def test_confirm_import_exposes_frozen_ids_in_command_state_and_job_result(ids):
    doc=api();state={'validation_id':'import-1','operation':'confirm_valid_rows','puzzle_version_ids':ids}
    command={'command_id':'command-1','status':'applied','resource_version':2,'state':state,'error':None}
    result={'validation_id':'import-1','filename':'synthetic.csv','row_count':2,'valid_count':2,'invalid_count':0,'rows':[],'next_cursor':None,'puzzle_version_ids':ids}
    assert schema_validator('CommandResult',doc).is_valid(command)
    assert schema_validator('ImportResult',doc).is_valid(result)
    for broken in [dict(state,operation='confirm'),{k:v for k,v in state.items() if k!='puzzle_version_ids'}]:
        assert not schema_validator('CommandResult',doc).is_valid(dict(command,state=broken))
    assert not schema_validator('ImportResult',doc).is_valid({k:v for k,v in result.items() if k!='puzzle_version_ids'})
    for path,method,data in [('/api/v1/puzzle-imports/{job_id}/commands','post',command),('/api/v1/commands/{command_id}','get',command),('/api/v1/puzzle-imports/{job_id}','get',{'job_id':'job-1','status':'succeeded','version':2,'progress':{'done':2,'total':2},'result':result,'error':None})]:
        wire=doc['paths'][path][method]['responses']['200']['content']['application/json']['schema']
        envelope={'data':data,'request_id':'synthetic-request','contract_version':'LW-C1/1.0.0'}
        validator=Draft202012Validator(dict(wire,components=doc['components']),format_checker=FORMATS)
        assert validator.is_valid(envelope)
        missing=copy.deepcopy(envelope)
        missing['data']['result' if 'result' in data else 'state'].pop('puzzle_version_ids')
        assert not validator.is_valid(missing)


def test_cancel_import_returns_explicit_empty_ids_and_legacy_confirm_request_is_retained():
    doc=api();command={'command_id':'cancel-1','status':'applied','resource_version':2,'state':{'validation_id':'import-1','operation':'cancel','puzzle_version_ids':[]},'error':None}
    assert schema_validator('CommandResult',doc).is_valid(command)
    wire=doc['paths']['/api/v1/puzzle-imports/{job_id}/commands']['post']['requestBody']['content']['application/json']['schema']
    validator=Draft202012Validator(dict(wire,components=doc['components']))
    for name in ['confirm','confirm_valid_rows']:assert validator.is_valid({'command':name,'expected_version':1,'args':{'rows':[1,2]}})


def test_ws_display_ack_requires_be_and_actual_canonical_client_time():
    relay=json.loads((PACKAGE/'relay.schema.json').read_text());frame={'kind':'display.processed','stream_seq':'7','receipt_id':'receipt-1','client_time':'2026-10-27T00:00:00.123Z'}
    validator=Draft202012Validator({'$ref':'#/$defs/ClientControlExchange','$defs':relay['$defs']},format_checker=FORMATS)
    for kind in ['display.received','display.processed']:
        valid={'headers':{'X-BE-Extension-Version':'LW-BE/1.1.0'},'frame':dict(frame,kind=kind)}
        assert validator.is_valid(valid)
        assert not validator.is_valid(dict(valid,headers={}))
        assert not validator.is_valid(dict(valid,headers={'X-BE-Extension-Version':'LW-BE/2.0.0'}))
        missing=copy.deepcopy(valid);missing['frame'].pop('client_time');assert not validator.is_valid(missing)
        for time in [None,1729900000000,'2026-10-27T00:00:00Z','2026-10-27T08:00:00.123+08:00','2026-02-30T00:00:00.123Z','1900-02-29T00:00:00.123Z','0000-01-01T00:00:00.123Z']:
            invalid=copy.deepcopy(valid);invalid['frame']['client_time']=time;assert not validator.is_valid(invalid)
    for leap_time in ['2000-02-29T00:00:00.123Z','2024-02-29T23:59:59.999Z']:
        assert validator.is_valid({'headers':{'X-BE-Extension-Version':'LW-BE/1.1.0'},'frame':dict(frame,client_time=leap_time)})
    assert validator.is_valid({'headers':{},'frame':{'kind':'transport.persisted','persisted_seq':'7'}})
    assert validator.is_valid({'headers':{},'frame':{'kind':'resume','lease_id':'lease-1','persisted_seq':'7'}})


def test_http_display_stage_keeps_prior_date_time_compatibility():
    doc=api();wire=doc['paths']['/relay/v1/delivery-stages']['post']['requestBody']['content']['application/json']['schema'];validator=Draft202012Validator(dict(wire,components=doc['components']),format_checker=FORMATS)
    for time in ['2026-10-27T00:00:00Z','2026-10-27T08:00:00.123+08:00']:
        assert validator.is_valid({'stream_id':'stream-1','stream_seq':'7','stage':'display.processed','receipt_id':'receipt-1','client_time':time})


def test_connection_status_payload_and_transport_are_public_c1_shapes():
    local=json.loads((PACKAGE/'local-websocket.schema.json').read_text());doc=api();baseline=json.loads((PACKAGE/'c1-baseline.json').read_text())
    for state in baseline['transport_states']:
        transport={'state':state,'reason_code':'SYNTHETIC_OBSERVATION','recoverable':state!='CONNECTED','last_confirmed_at':'2026-10-27T00:00:00.000Z'}
        assert schema_validator('Transport',doc).is_valid(transport)
        frame={'kind':'event','schema_version':1,'contract_version':'LW-C1/1.0.0','event_id':'event-1','session_id':'session-1','round_id':None,'seq':'8','server_time':'2026-10-27T00:00:00.000Z','type':'connection.status','delivery_ids':[],'delivery_mode':'live','payload':{'transport':transport}}
        for path in ['/api/v1/display/ws','/api/v1/studio/ws']:
            validator=Draft202012Validator(local['protocols'][path]['server_frame_schema'])
            assert validator.is_valid(frame)
            invalid=copy.deepcopy(frame);invalid['payload']['transport']['authorization_id']='private';assert not validator.is_valid(invalid)
    assert not schema_validator('Transport',doc).is_valid({'state':'AUTHENTICATING','reason_code':'private state','recoverable':True,'last_confirmed_at':None})


def test_ws_declared_rejections_never_describe_legacy_fake_pending():
    relay=json.loads((PACKAGE/'relay.schema.json').read_text())
    fixtures=json.loads((PACKAGE/'fixtures/confirmed-interface-fixes.json').read_text())
    exchange=Draft202012Validator({'$ref':'#/$defs/ClientControlExchange','$defs':relay['$defs']},format_checker=FORMATS)
    errors=relay['x-client-control-negotiation']['errors']
    server=Draft202012Validator({'$ref':'#/$defs/ServerControl','$defs':relay['$defs']})
    for case in fixtures['ws_rejections']:
        expected=case['expected'];assert expected['reason_code'] in errors.values()
        assert expected['close_code']==1008 and expected['stage_writes']==expected['job_writes']==0
        assert server.is_valid({'kind':expected['kind'],'reason_code':expected['reason_code']})
        if 'precondition' not in case:assert not exchange.is_valid({'headers':case['headers'],'frame':case['frame']})
    same=fixtures['cross_channel_idempotency']
    from datetime import datetime
    assert datetime.fromisoformat(same['http_original_time'])==datetime.fromisoformat(same['ws_time'].replace('Z','+00:00'))
    assert same['expected_new_jobs']==0 and same['expected_retained_original_time']==same['http_original_time']
