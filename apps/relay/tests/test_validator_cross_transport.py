"""Independent reciprocal HTTP/WS timestamp conflict check in an owned schema."""
import base64
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from test_ws_display_fulfillment import live_relay, resumed, send_stage


@pytest.mark.parametrize('stage', ['display.received', 'display.processed'])
def test_ws_first_http_same_millisecond_and_conflicting_time(relay_environment, stage):
    original = '2026-10-27T01:02:03.123Z'
    with live_relay(relay_environment) as (service, grant, lease, url, reported):
        token = 'synthetic-validator-http-time'
        with service.tx() as connection:
            connection.execute("INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test,device_token_hash,device_token_expires_at) VALUES(%s,%s,%s,'synthetic-binding',%s,'synthetic-admin',%s,'unused','bound',%s,%s,'synthetic-room-proof',true,%s,%s)", (str(uuid4()), grant.app_id, grant.room_id, grant.installation_id, b'0'*32, grant.expires_at, grant.expires_at, hashlib.sha256(token.encode()).hexdigest(), grant.expires_at))
            stream = connection.execute('SELECT stream_id FROM relay.relay_streams WHERE app_id=%s AND room_id=%s', (grant.app_id, grant.room_id)).fetchone()['stream_id']
        credential = base64.urlsafe_b64encode(json.dumps({'device_token': token, 'authorization_id': 'synthetic-no-live-grant', 'session_id': grant.session_id}).encode()).rstrip(b'=').decode()
        with resumed(url, lease) as websocket:
            send_stage(websocket, 'display.received', 2, original)
            if stage == 'display.processed':
                send_stage(websocket, stage, 2, original)
            endpoint = url.replace('ws://', 'http://').replace('/streams', '/delivery-stages')
            body = {'stage': stage, 'stream_id': stream, 'stream_seq': '2', 'receipt_id': f'ws-{stage}-2', 'client_time': '2026-10-27T09:02:03.123456+08:00'}
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
                headers = {'Authorization': 'Bearer '+credential, 'X-BE-Extension-Version': 'LW-BE/1.1.0'}
                same = client.post(endpoint, headers=headers, json=body)
                assert same.status_code == 200
                different = client.post(endpoint, headers=headers, json=body | {'client_time': '2026-10-27T09:02:03.124+08:00'})
                assert different.status_code == 409, {'status': different.status_code, 'body': different.json()}
                assert different.json()['error']['code'] == 'RECEIPT_CONFLICT'
        with service.tx() as connection:
            stored = connection.execute('SELECT payload FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s', (stage,)).fetchone()['payload']
            assert stored['client_time'] == original


def historical_headers(service,grant):
    token='synthetic-r2-http-time-token'
    with service.tx() as connection:
        connection.execute("INSERT INTO relay.device_pairings(pairing_id,app_id,room_id,room_binding_id,installation_id,actor_id,device_public_key,challenge,status,expires_at,room_verified_until,verification_id,provider_test,device_token_hash,device_token_expires_at) VALUES(%s,%s,%s,'synthetic-binding',%s,'synthetic-admin',%s,'unused','bound',%s,%s,'synthetic-room-proof',true,%s,%s)",(str(uuid4()),grant.app_id,grant.room_id,grant.installation_id,b'0'*32,grant.expires_at,grant.expires_at,hashlib.sha256(token.encode()).hexdigest(),grant.expires_at))
        stream=connection.execute('SELECT stream_id FROM relay.relay_streams WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['stream_id']
    credential=base64.urlsafe_b64encode(json.dumps({'device_token':token,'authorization_id':'synthetic-no-live-grant','session_id':grant.session_id}).encode()).rstrip(b'=').decode()
    return stream,{'Authorization':'Bearer '+credential,'X-BE-Extension-Version':'LW-BE/1.1.0'}


@pytest.mark.parametrize('stage',['display.received','display.processed'])
def test_http_retry_failed_cannot_change_ws_actual_time_or_retry_state(relay_environment,stage):
    original='2026-10-27T01:02:03.123Z'
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        stream,headers=historical_headers(service,grant)
        with resumed(url,lease) as websocket:
            send_stage(websocket,'display.received',2,original)
            if stage=='display.processed':send_stage(websocket,stage,2,original)
            with service.tx() as connection:
                connection.execute("UPDATE relay.relay_outbox SET status='failed',last_error='SYNTHETIC_FAILED',claim_token=NULL,lease_until=NULL WHERE stream_seq=2 AND kind=%s",(stage,))
            endpoint=url.replace('ws://','http://').replace('/streams','/delivery-stages')
            body={'stage':stage,'stream_id':stream,'stream_seq':'2','receipt_id':f'ws-{stage}-2','client_time':'2026-10-27T01:02:03.124+00:00','retry_failed':True}
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as client:
                response=client.post(endpoint,headers=headers|{'Idempotency-Key':'synthetic-conflicting-retry'},json=body)
                assert response.status_code==409,response.json()
                assert response.json()['error']['code']=='RECEIPT_CONFLICT'
            with service.tx() as connection:
                row=connection.execute('SELECT status,payload,retry_cycle FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()
                assert row['payload']['client_time']==original and row['status']=='failed' and row['retry_cycle']==0


@pytest.mark.parametrize('stage',['display.received','display.processed'])
def test_http_cannot_fill_missing_legacy_time_during_retry(relay_environment,stage):
    original='2026-10-27T01:02:03.123Z'
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        stream,headers=historical_headers(service,grant)
        with resumed(url,lease) as websocket:
            send_stage(websocket,'display.received',2,original)
            if stage=='display.processed':send_stage(websocket,stage,2,original)
            with service.tx() as connection:
                connection.execute("UPDATE relay.relay_outbox SET status='failed',last_error='DISPLAY_CLIENT_TIME_REQUIRED',payload=payload-'client_time',claim_token=NULL,lease_until=NULL WHERE stream_seq=2 AND kind=%s",(stage,))
            endpoint=url.replace('ws://','http://').replace('/streams','/delivery-stages')
            body={'stage':stage,'stream_id':stream,'stream_seq':'2','receipt_id':f'ws-{stage}-2','client_time':original,'retry_failed':True}
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as client:
                response=client.post(endpoint,headers=headers|{'Idempotency-Key':'synthetic-legacy-time-retry'},json=body)
                assert response.status_code==409,response.json()
                assert response.json()['error']['code']=='RECEIPT_CONFLICT'
            with service.tx() as connection:
                row=connection.execute('SELECT status,payload,retry_cycle FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()
                assert 'client_time' not in row['payload'] and row['status']=='failed' and row['retry_cycle']==0


@pytest.mark.parametrize('stage',['display.received','display.processed'])
def test_http_same_time_after_sdk_completion_never_requeues_or_reports_twice(relay_environment,stage):
    from time import monotonic,sleep
    original='2026-10-27T01:02:03.123Z'
    with live_relay(relay_environment) as (service,grant,lease,url,reported):
        stream,headers=historical_headers(service,grant)
        with resumed(url,lease) as websocket:
            send_stage(websocket,'display.received',2,original)
            if stage=='display.processed':send_stage(websocket,stage,2,original)
            deadline=monotonic()+5
            expected_stage='platform.received' if stage=='display.received' else 'platform.fulfilled'
            while expected_stage not in service.stages(grant.app_id,grant.room_id,2):
                assert monotonic()<deadline,'Original SDK report did not complete'
                sleep(.02)
            with service.tx() as connection:
                before=connection.execute('SELECT job_id,status,attempts,retry_cycle,payload FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()
            endpoint=url.replace('ws://','http://').replace('/streams','/delivery-stages')
            body={'stage':stage,'stream_id':stream,'stream_seq':'2','receipt_id':f'ws-{stage}-2','client_time':'2026-10-27T09:02:03.123999+08:00'}
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=3) as client:
                response=client.post(endpoint,headers=headers,json=body)
                assert response.status_code==200,response.json()
            with service.tx() as connection:
                after=connection.execute('SELECT job_id,status,attempts,retry_cycle,payload FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()
                assert after==before and after['status']=='succeeded'
                assert after['payload']['client_time']==original
                assert connection.execute('SELECT count(*) AS n FROM relay.relay_outbox WHERE stream_seq=2 AND kind=%s',(stage,)).fetchone()['n']==1
            ack_type=1 if stage=='display.received' else 2
            assert sum(len(items) for kind,items in reported if kind==ack_type)==1
