"""Internal C1 transport validation; no vendor wire protocol is assumed."""
import base64
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from app.errors import DomainError

CONTRACT='LW-C1/1.0.0'


def reject(code='INPUT_INVALID', message='Invalid relay protocol'):
    raise DomainError(code,message,409 if code!='INPUT_INVALID' else 422)


def cursor(value):
    if not isinstance(value,str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}',value): reject()
    result=int(value)
    if result>9223372036854775807: reject()
    return result


def strict_json(raw):
    if not isinstance(raw,(str,bytes)) or len(raw)>65536: reject('PAYLOAD_TOO_LARGE')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: reject()
            result[key]=value
        return result
    def invalid(_): reject()
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)
        stack=[(value,0)];nodes=0
        while stack:
            item,depth=stack.pop();nodes+=1
            if depth>16 or nodes>4096: reject()
            if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
            if isinstance(item,list):stack.extend((v,depth+1) for v in item)
        if not isinstance(value,dict):reject()
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):reject()


def registered_target(connector_id, registry, *, test_mode=False):
    target=registry.get(connector_id)
    if not isinstance(target,dict):reject('CONNECTOR_NOT_REGISTERED')
    parsed=urlsplit(target.get('url',''))
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path!='/relay/v1/streams':reject('CONNECTOR_TARGET_INVALID')
    if target.get('test_only') is True:
        if not test_mode or parsed.scheme!='ws' or parsed.hostname not in ('127.0.0.1','::1'):reject('TEST_PROVIDER_FORBIDDEN')
    elif parsed.scheme!='wss' or not parsed.hostname:reject('CONNECTOR_TARGET_INVALID')
    return dict(target)


def decode_delivery(frame, connection):
    expected={'kind','stream_id','stream_seq','body_b64','immutable_sha256','lease_id','installation_id','delivery_generation','connection_nonce','persisted_seq'}
    if not isinstance(frame,dict) or set(frame)!=expected or frame['kind']!='relay.frame':reject()
    for key in ('lease_id','installation_id','delivery_generation','connection_nonce'):
        if frame[key]!=connection.get(key):reject('CONNECTION_STALE')
    for key in ('stream_seq','persisted_seq','delivery_generation'):cursor(frame[key])
    if not isinstance(frame['immutable_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',frame['immutable_sha256']):reject()
    try:raw=base64.b64decode(frame['body_b64'],validate=True)
    except (ValueError,TypeError):reject()
    if hashlib.sha256(raw).hexdigest()!=frame['immutable_sha256']:reject('FRAME_INTEGRITY_FAILURE')
    body=strict_json(raw)
    required={'schema_version','contract_version','stream_id','stream_seq','original_generation','round_id','window_epoch','relay_received_at','type','eligible','provider_test','run_purpose','source_mode','payload'}
    if set(body)!=required or type(body['schema_version']) is not int or body['schema_version']!=1 or body['contract_version']!=CONTRACT:reject('CONTRACT_VERSION_UNSUPPORTED')
    if any(body[k]!=frame[k] for k in ('stream_id','stream_seq')):reject('FRAME_INTEGRITY_FAILURE')
    for key in ('stream_seq','original_generation','window_epoch'):cursor(body[key])
    if body['type'] not in ('comment','window.open','window.close','window.pause','window.resume','stream.bootstrap') or type(body['eligible']) is not bool or type(body['provider_test']) is not bool or not isinstance(body['payload'],dict):reject()
    if body['type']=='stream.bootstrap' and (body['payload'].get('be_extension_version')!='LW-BE/1.1.0' or body['payload'].get('start_seq')!=body['stream_seq'] or body['eligible']):reject('BE_EXTENSION_VERSION_UNSUPPORTED')
    if body['run_purpose'] not in ('test_live','production') or body['source_mode'] not in ('official','synthetic'):reject()
    if body['source_mode']=='synthetic' and (body['run_purpose']!='test_live' or not body['provider_test']):reject('TEST_PROVIDER_FORBIDDEN')
    if not body['provider_test'] and body['source_mode']!='official':reject()
    try:
        stamp=datetime.fromisoformat(body['relay_received_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError()
    except (ValueError,TypeError,AttributeError):reject()
    return body
