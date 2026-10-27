"""Public golden vector and MockTransport only; never real account/live calls."""
import base64
import importlib
import importlib.util
import json
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from app.core import RelayError


def module():
    assert importlib.util.find_spec('app.provider') is not None,'Missing published Douyin wire adapter'
    return importlib.import_module('app.provider')


def test_official_public_push_signature_golden_vector():
    p=module();headers={'x-nonce-str':'123456','x-timestamp':'456789','x-roomid':'268','x-msg-type':'live_gift'}
    assert p.push_signature(headers,'abc123你好'.encode(),'123abc')=='PDcKhdlsrKEJif6uMKD2dw=='
    p.verify_push_signature(headers|{'x-signature':'PDcKhdlsrKEJif6uMKD2dw=='},'abc123你好'.encode(),'123abc')
    with pytest.raises(RelayError):p.verify_push_signature(headers|{'x-signature':'invalid'},b'changed','123abc')


def test_comment_normalization_preserves_timestamp_diagnostic_and_rejects_unknown_provenance():
    p=module();raw=json.dumps([{'msg_id':'synthetic-msg','sec_openid':'synthetic-raw-uid','content':'测试','nickname':'Viewer','avatar_url':'https://not-fetched.invalid/avatar','timestamp':1649068964,'is_test':False}],ensure_ascii=False).encode()
    h={'x-nonce-str':'n','x-timestamp':'1234567890123','x-roomid':'synthetic-room','x-msg-type':'live_comment'};h['x-signature']=p.push_signature(h,raw,'synthetic-push-secret')
    unknown=p.DouyinPushProvider('synthetic-app','synthetic-push-secret',b's'*32)
    with pytest.raises(RelayError,match='PLATFORM_PROVENANCE_REQUIRED'):unknown.verify_and_decode(raw,h)
    policy=lambda app,room:p.ChannelProvenance('controlled_test_live',True,'synthetic-contract-policy',datetime.now(timezone.utc)+timedelta(minutes=1))
    adapter=p.DouyinPushProvider('synthetic-app','synthetic-push-secret',b's'*32,provenance=policy)
    app,room,messages=adapter.verify_and_decode(raw,h)
    assert (app,room)==('synthetic-app','synthetic-room') and messages[0].provider_test is True
    assert messages[0].payload=={'text':'测试'} and 'synthetic-raw-uid' not in str(messages[0].payload)
    assert messages[0].participant_key.startswith('p:')
    assert messages[0].source_metadata['timestamp_raw']==1649068964
    assert 'ambiguous' in messages[0].source_metadata['timestamp_unit_diagnostic']


def test_fixed_endpoints_memory_token_singleflight_task_and_ack_wire():
    p=module();calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.scheme=='https' and request.url.host in {'developer.toutiao.com','webcast.bytedance.com'}
        if request.url.path=='/api/apps/v2/token':return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-memory-token','expires_in':7200}})
        assert request.headers.get('access-token')=='synthetic-memory-token' or request.headers.get('x-token')=='synthetic-memory-token'
        if request.url.path.endswith('/start'):return httpx.Response(200,json={'err_no':0,'data':{'task_id':'synthetic-task'}})
        if request.url.path.endswith('/get'):return httpx.Response(200,json={'err_no':0,'data':{'status':3}})
        if request.url.path.endswith('/info'):return httpx.Response(200,json={'data':{'info':{'room_id':1234567890123456789,'anchor_open_id':'synthetic-anchor'}}})
        return httpx.Response(200,json={'err_no':0,'data':{}})
    client=p.DouyinClient('synthetic-app-token-test','synthetic-app-secret',profile='data_open_access_token',transport=httpx.MockTransport(handler))
    with ThreadPoolExecutor(max_workers=8) as pool:assert list(pool.map(lambda _:client.access_token(),range(8)))==['synthetic-memory-token']*8
    assert len([c for c in calls if c.url.path.endswith('/token')])==1
    assert client.start_task('synthetic-room')['task_id']=='synthetic-task'
    assert client.query_task('synthetic-room')['status']==3
    client.stop_task('synthetic-room')
    client.ack('synthetic-room','display.processed',[{'msg_id':'synthetic-msg','msg_type':'live_comment','client_time':1234567890123}])
    posted=json.loads(calls[-1].content);assert posted['ack_type']==2 and posted['app_id']==client.app_id
    assert json.loads(posted['data'])==[{'msg_id':'synthetic-msg','msg_type':'live_comment','client_time':1234567890123}]
    assert client.verify_live_token('synthetic-launch-token').room_id=='1234567890123456789'


def test_http_200_vendor_failure_is_not_success_and_rsa_profile_never_downgrades():
    p=module()
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-error-token','expires_in':7200}})
        return httpx.Response(200,json={'err_no':40023,'err_msg':'synthetic private echo must not escape'})
    client=p.DouyinClient('synthetic-app-error','synthetic-secret',profile='data_open_access_token',transport=httpx.MockTransport(handler))
    with pytest.raises(RelayError) as error:client.start_task('synthetic-room')
    assert 'private echo' not in str(error.value)
    with pytest.raises(RelayError):p.DouyinClient('synthetic','synthetic',profile='openapi_rsa',transport=httpx.MockTransport(handler))


def test_rsa_request_exact_lines_and_success_response_signature_required():
    import re
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa,padding
    p=module();app=rsa.generate_private_key(public_exponent=65537,key_size=2048);platform=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    private=app.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption());public=platform.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    profile=p.RSAProfile('synthetic-rsa-app','1',private,public);raw='{"text":"你好"}'.encode()
    authorization=profile.authorization('POST','/api/example?q=1',raw,timestamp='123',nonce='nonce')
    signature=re.search('signature="([^"]+)"',authorization).group(1)
    app.public_key().verify(base64.b64decode(signature),b'POST\n/api/example?q=1\n123\nnonce\n'+raw+b'\n',padding.PKCS1v15(),hashes.SHA256())
    response=b'{"err_no":0,"data":{}}';signed=platform.sign(b'124\nserver-nonce\n'+response+b'\n',padding.PKCS1v15(),hashes.SHA256())
    headers={'Byte-Timestamp':'124','Byte-Nonce-Str':'server-nonce','Byte-Signature':base64.b64encode(signed).decode()}
    profile.verify_response(response,headers)
    with pytest.raises(RelayError):profile.verify_response(response+b' ',headers)
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-rsa-token','expires_in':7200}})
        assert request.headers['Byte-Authorization'].startswith('SHA256-RSA2048 ')
        return httpx.Response(200,json={'err_no':0,'data':{'task_id':'synthetic'}})  # Missing Byte-Signature must fail.
    client=p.DouyinClient('synthetic-rsa-app','synthetic-rsa-secret',profile='openapi_rsa',rsa_profile=profile,transport=httpx.MockTransport(handler))
    with pytest.raises(RelayError,match='DOUYIN_RESPONSE_SIGNATURE_INVALID'):client.start_task('synthetic-room')


def test_task_qps_limit_does_not_invent_an_ack_ten_per_second_limit():
    p=module();calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-rate-token','expires_in':7200}}) if request.url.path.endswith('/token') else httpx.Response(200,json={'err_no':0,'data':{'status':3}})
    client=p.DouyinClient('synthetic-rate-app','synthetic-rate-secret',profile='data_open_access_token',transport=httpx.MockTransport(handler),now=lambda:1)
    for _ in range(10):client.query_task('synthetic-room')
    with pytest.raises(RelayError,match='DOUYIN_RATE_LIMITED'):client.query_task('synthetic-room')
    for _ in range(12):client.ack('synthetic-room','display.received',[{'msg_id':'synthetic','msg_type':'live_comment','client_time':1000}])
    assert calls.count('/api/live_data/ack')==12
