"""Published Douyin wire, with explicit signature/provenance profiles.

Sources (public docs, checked 2026-10-26):
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/data-open/data-open-desc
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/interface-request-credential/get-access-token
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/data-open/start-task
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/data-open/stop-task
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/data-open/query-task-status
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/data-open/ack-data-report
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/live-info
https://developer.open-douyin.com/docs/resource/zh-CN/interaction/develop/server/live-room-scope/signature-guide

MD5 is ONLY the documented raw push protocol. RSA is a distinct OpenAPI
profile. No algorithm fallback, no automatic G0 pass, no network on import/init.
"""
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import urlencode,urlsplit
import httpx
from fastapi import Response
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding,rsa
from cryptography.exceptions import InvalidSignature
from .core import RelayError,SourceMessage,opaque_id
from .control import participant_key
from .control import VerifiedRoom
from .fulfillment import PlatformOutcome

TOKEN_URL='https://developer.toutiao.com/api/apps/v2/token'
BASE='https://webcast.bytedance.com'
TASK='/api/live_data/task/'
PUSH_FIELDS=('x-msg-type','x-nonce-str','x-roomid','x-timestamp')


def _headers(headers):
    result={str(k).lower():str(v) for k,v in headers.items()}
    for key in PUSH_FIELDS:
        if hasattr(headers,'getlist') and len(headers.getlist(key))!=1:raise RelayError('SIGNATURE_INVALID',403)
        if not result.get(key) or len(result[key])>256:raise RelayError('SIGNATURE_INVALID',403)
    return result


def push_signature(headers,raw,secret):
    values=_headers(headers)
    if not isinstance(raw,bytes) or not isinstance(secret,str) or not secret:raise RelayError('PUSH_SECRET_REQUIRED',503)
    try:raw.decode('utf-8')
    except UnicodeError:raise RelayError('INPUT_INVALID',422) from None
    prefix='&'.join(key+'='+values[key] for key in sorted(PUSH_FIELDS)).encode()
    return base64.b64encode(hashlib.md5(prefix+raw+secret.encode('utf-8'),usedforsecurity=False).digest()).decode()


def verify_push_signature(headers,raw,secret):
    expected=push_signature(headers,raw,secret)
    value={str(k).lower():str(v) for k,v in headers.items()}.get('x-signature','')
    try:valid=hmac.compare_digest(expected.encode('ascii'),value.encode('ascii'))
    except UnicodeError:valid=False
    if not valid:raise RelayError('SIGNATURE_INVALID',403)


def _json(raw,limit=1048576):
    if len(raw)>limit:raise RelayError('PAYLOAD_TOO_LARGE',413)
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise RelayError('INPUT_INVALID',422)
            result[key]=value
        return result
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        stack=[(value,0)];nodes=0
        while stack:
            item,depth=stack.pop();nodes+=1
            if depth>16 or nodes>20000:raise ValueError()
            if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
            if isinstance(item,list):stack.extend((v,depth+1) for v in item)
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):raise RelayError('INPUT_INVALID',422) from None


@dataclass(frozen=True)
class ChannelProvenance:
    profile:str
    provider_test:bool
    verification_id:str
    expires_at:datetime


class DouyinPushProvider:
    provider_test=False  # Real wire implementation, not SyntheticTestProvider.
    def __init__(self,app_id,push_secret,identity_key,*,provenance=None):
        opaque_id(app_id);self.app_id=app_id;self._secret=push_secret;self._identity_key=identity_key;self.provenance=provenance
        self.available=bool(push_secret and isinstance(identity_key,bytes) and len(identity_key)>=32 and provenance)

    def verify_and_decode(self,raw,headers):
        if len(raw)>1048576:raise RelayError('PAYLOAD_TOO_LARGE',413)
        verify_push_signature(headers,raw,self._secret)
        h=_headers(headers);room=opaque_id(h['x-roomid'])
        if h['x-msg-type']!='live_comment':raise RelayError('UNSUPPORTED_PLATFORM_MESSAGE_TYPE',422)
        if {str(k).lower():str(v) for k,v in headers.items()}.get('content-type','application/json').split(';',1)[0].strip().lower()!='application/json':raise RelayError('INPUT_INVALID',422)
        policy=self.provenance(self.app_id,room) if self.provenance else None
        if not isinstance(policy,ChannelProvenance) or not policy.verification_id or policy.expires_at<=datetime.now(timezone.utc) or type(policy.provider_test) is not bool or policy.profile not in ('controlled_test_live','production_verified') or policy.profile=='controlled_test_live' and not policy.provider_test:raise RelayError('PLATFORM_PROVENANCE_REQUIRED',403)
        value=_json(raw)
        if not isinstance(value,list) or not value or len(value)>1000:raise RelayError('PAYLOAD_TOO_LARGE' if isinstance(value,list) and len(value)>1000 else 'INPUT_INVALID',413 if isinstance(value,list) and len(value)>1000 else 422)
        messages=[]
        for item in value:
            if not isinstance(item,dict) or not {'msg_id','sec_openid','content','nickname','timestamp'}<=set(item):raise RelayError('INPUT_INVALID',422)
            opaque_id(item['msg_id']);opaque_id(item['sec_openid'])
            if not isinstance(item['content'],str) or not isinstance(item['nickname'],str) or type(item['timestamp']) not in (int,str):raise RelayError('INPUT_INVALID',422)
            name=''.join(ch for ch in item['nickname'] if ord(ch)>=32).strip()[:80] or 'Viewer'
            raw_time=item['timestamp'];digits=str(raw_time)
            if not re.fullmatch('[0-9]{1,19}',digits):raise RelayError('INPUT_INVALID',422)
            meta={'timestamp_raw':raw_time,'timestamp_unit_diagnostic':'declared_ms_example_10_digits_ambiguous' if len(digits)==10 else 'declared_milliseconds','header_timestamp_raw':h['x-timestamp'],'push_profile':'data_open_md5','provenance_profile':policy.profile,'provenance_verification_id':policy.verification_id}
            fingerprint=hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            messages.append(SourceMessage(item['msg_id'],'comment',{'text':item['content']},participant_key(item['sec_openid'],self._identity_key,self.app_id),name,policy.provider_test,fingerprint,meta))
        return self.app_id,room,messages

    def success_response(self,sequences):return Response(status_code=204)


class RSAProfile:
    def __init__(self,app_id,key_version,application_private_pem,platform_public_pem):
        try:
            if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',app_id) or not re.fullmatch('[A-Za-z0-9_.-]{1,64}',key_version):raise ValueError()
            private=serialization.load_pem_private_key(application_private_pem,password=None);public=serialization.load_pem_public_key(platform_public_pem)
            if not isinstance(private,rsa.RSAPrivateKey) or not isinstance(public,rsa.RSAPublicKey) or private.key_size!=2048 or public.key_size!=2048:raise ValueError()
        except Exception:raise RelayError('DOUYIN_RSA_CONFIGURATION_REQUIRED',503) from None
        self.app_id=app_id;self.key_version=key_version;self._private=private;self._public=public

    def authorization(self,method,path_query,raw,*,timestamp=None,nonce=None):
        timestamp=str(int(time.time())) if timestamp is None else str(timestamp);nonce=nonce or secrets.token_hex(16)
        if not re.fullmatch('[0-9]{1,19}',timestamp) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',nonce) or not path_query.startswith('/'):raise RelayError('INPUT_INVALID',422)
        signed=method.upper().encode()+b'\n'+path_query.encode()+b'\n'+timestamp.encode()+b'\n'+nonce.encode()+b'\n'+raw+b'\n'
        signature=base64.b64encode(self._private.sign(signed,padding.PKCS1v15(),hashes.SHA256())).decode()
        return f'SHA256-RSA2048 appid="{self.app_id}",nonce_str="{nonce}",timestamp="{timestamp}",key_version="{self.key_version}",signature="{signature}"'

    def verify_response(self,raw,headers):
        h={str(k).lower():str(v) for k,v in headers.items()}
        try:
            stamp=h['byte-timestamp'];nonce=h['byte-nonce-str'];signature=base64.b64decode(h['byte-signature'],validate=True)
            if not re.fullmatch('[0-9]{1,19}',stamp) or not nonce or len(nonce)>256:raise ValueError()
            self._public.verify(signature,stamp.encode()+b'\n'+nonce.encode()+b'\n'+raw+b'\n',padding.PKCS1v15(),hashes.SHA256())
        except (KeyError,ValueError,InvalidSignature,UnicodeError):raise RelayError('DOUYIN_RESPONSE_SIGNATURE_INVALID',503) from None


@dataclass(repr=False)
class _TokenState:
    lock:object
    token:str|None=None
    refresh_at:float=0

_TOKEN_STATES={};_TOKEN_LOCK=threading.Lock();_RATE={};_RATE_LOCK=threading.Lock()


@dataclass(frozen=True,repr=False)
class LiveRoomInfo:
    room_id:str
    anchor_open_id:str
    verification_id:str


class DouyinClient:
    def __init__(self,app_id,app_secret,*,profile,transport=None,rsa_profile=None,allow_network=False,now=time.monotonic):
        opaque_id(app_id)
        if not app_secret or profile not in ('data_open_access_token','openapi_rsa') or profile=='openapi_rsa' and not isinstance(rsa_profile,RSAProfile):raise RelayError('DOUYIN_PROFILE_CONFIGURATION_REQUIRED',503)
        self.app_id=app_id;self._secret=app_secret;self.profile=profile;self.rsa=rsa_profile;self.transport=transport;self.allow_network=allow_network;self.now=now
        cache_key=(app_id,hashlib.sha256(app_secret.encode()).digest())
        with _TOKEN_LOCK:self._state=_TOKEN_STATES.setdefault(cache_key,_TokenState(threading.Lock()))

    def _http(self,method,url,body=None,*,token_header=None,token=None,rsa_sign=False):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ('developer.toutiao.com','webcast.bytedance.com') or parsed.username or parsed.password or parsed.fragment:raise RelayError('DOUYIN_TARGET_FORBIDDEN',403)
        if self.transport is None and not self.allow_network:raise RelayError('DOUYIN_EXTERNAL_CALLS_DISABLED',503)
        raw=b'' if body is None else json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>1048576:raise RelayError('PAYLOAD_TOO_LARGE',413)
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if token_header:headers[token_header]=token
        if rsa_sign:
            headers['Byte-Authorization']=self.rsa.authorization(method,parsed.path+('?' + parsed.query if parsed.query else ''),raw)
        try:
            with httpx.Client(transport=self.transport,trust_env=False,follow_redirects=False,timeout=4) as client:response=client.request(method,url,content=raw,headers=headers)
            if not 200<=response.status_code<300:raise RelayError('DOUYIN_HTTP_RETRY' if response.status_code in (429,500,502,503,504) else 'DOUYIN_HTTP_REJECTED',503)
            if rsa_sign:self.rsa.verify_response(response.content,response.headers)
            data=_json(response.content)
            if not isinstance(data,dict):raise RelayError('DOUYIN_RESPONSE_INVALID',503)
            return data,response.content
        except RelayError:raise
        except Exception:raise RelayError('DOUYIN_TRANSPORT_UNCERTAIN',503) from None

    @staticmethod
    def _ok(data,*,required=True):
        number=data.get('err_no',data.get('errcode'))
        if number is None and not required:return
        if type(number) is not int or number!=0:raise RelayError('DOUYIN_ERR_'+str(number) if type(number) is int else 'DOUYIN_RESPONSE_INVALID',503)

    def access_token(self):
        with self._state.lock:
            if self._state.token and self.now()<self._state.refresh_at:return self._state.token
            data,_=self._http('POST',TOKEN_URL,{'appid':self.app_id,'secret':self._secret,'grant_type':'client_credential'})
            self._ok(data);value=data.get('data',{});token=value.get('access_token');ttl=value.get('expires_in')
            if not isinstance(token,str) or not token or type(ttl) is not int or ttl<=0:raise RelayError('DOUYIN_TOKEN_INVALID',503)
            self._state.token=token;self._state.refresh_at=self.now()+min(3600,max(.1,ttl-min(60,ttl/5)))
            return token

    def _limit(self,endpoint):
        with _RATE_LOCK:
            now=self.now();events=_RATE.setdefault((self.app_id,endpoint),deque())
            while events and events[0]<=now-1:events.popleft()
            if len(events)>=10:raise RelayError('DOUYIN_RATE_LIMITED',429)
            events.append(now)

    def _call(self,method,path,body=None,*,header='access-token',rate_limit=True):
        if rate_limit:self._limit(path.split('?',1)[0])
        data,raw=self._http(method,BASE+path,body,token_header=header,token=self.access_token(),rsa_sign=self.profile=='openapi_rsa')
        try:self._ok(data,required=not path.startswith('/api/webcastmate/info'))
        except RelayError:
            if data.get('err_no')==40022:self._state.token=None;self._state.refresh_at=0
            raise
        return data,raw

    def start_task(self,room):
        opaque_id(room);data,_=self._call('POST',TASK+'start',{'appid':self.app_id,'roomid':room,'msg_type':'live_comment'})
        task=data.get('data',{}).get('task_id')
        if not isinstance(task,(str,int)) or isinstance(task,bool) or not str(task):raise RelayError('DOUYIN_TASK_ID_MISSING',503)
        return {'task_id':str(task)}

    def stop_task(self,room):
        opaque_id(room);data,_=self._call('POST',TASK+'stop',{'appid':self.app_id,'roomid':room,'msg_type':'live_comment'});return data.get('data',{})

    def query_task(self,room):
        opaque_id(room);query=urlencode({'appid':self.app_id,'roomid':room,'msg_type':'live_comment'})
        data,_=self._call('GET',TASK+'get?'+query);status=data.get('data',{}).get('status')
        if type(status) is not int or status not in (1,2,3):raise RelayError('DOUYIN_TASK_STATUS_UNKNOWN',503)
        return {'status':status}

    def ack(self,room,stage,items):
        opaque_id(room)
        if stage not in ('display.received','display.processed') or not items or len(items)>1000:raise RelayError('INPUT_INVALID',422)
        for item in items:
            if set(item)!={'msg_id','msg_type','client_time'} or item['msg_type']!='live_comment' or type(item['client_time']) is not int or not 0<=item['client_time']<=9223372036854775807:raise RelayError('INPUT_INVALID',422)
            opaque_id(item['msg_id'])
        self._call('POST','/api/live_data/ack',{'app_id':self.app_id,'room_id':room,'ack_type':1 if stage=='display.received' else 2,'data':json.dumps(items,ensure_ascii=False,separators=(',',':'))},rate_limit=False)

    def verify_live_token(self,launch_token):
        if not isinstance(launch_token,str) or not 0<len(launch_token)<=16384:raise RelayError('OFFICIAL_LAUNCH_TOKEN_REQUIRED',403)
        data,raw=self._call('POST','/api/webcastmate/info',{'token':launch_token},header='x-token')
        info=data.get('data',{}).get('info',{});room=info.get('room_id');anchor=info.get('anchor_open_id')
        if type(room) not in (str,int) or not str(room) or not isinstance(anchor,str) or not anchor:raise RelayError('DOUYIN_ROOM_VERIFICATION_FAILED',403)
        return LiveRoomInfo(str(room),anchor,hashlib.sha256(raw).hexdigest())


def client_milliseconds(value):
    if not isinstance(value,str):raise RelayError('DISPLAY_CLIENT_TIME_REQUIRED',422)
    try:
        moment=datetime.fromisoformat(value.replace('Z','+00:00'))
        if moment.tzinfo is None:raise ValueError()
        delta=moment.astimezone(timezone.utc)-datetime(1970,1,1,tzinfo=timezone.utc)
        result=delta.days*86400000+delta.seconds*1000+delta.microseconds//1000
        if result<0:raise ValueError()
        return result
    except (ValueError,TypeError):raise RelayError('DISPLAY_CLIENT_TIME_REQUIRED',422) from None


class DouyinPlatformAdapter:
    provider_test=False
    requires_task_reference=False  # Official identity is (appid, roomid, msg_type).
    def __init__(self,client,*,scope_guard=None):self.client=client;self.scope_guard=scope_guard;self.available=client.transport is not None or client.allow_network;self.provider_test=client.transport is not None

    def _scope(self,kind,context):
        if context.get('app_id')!=self.client.app_id or self.scope_guard is None or not self.scope_guard(kind,context):raise RelayError('PLATFORM_TASK_FENCE_REQUIRED',409)

    def perform(self,kind,context,payload):
        try:
            if kind.startswith('platform.'):self._scope(kind,context)
            if kind=='platform.start':return PlatformOutcome('succeeded',response={'platform_task_id':self.client.start_task(context['room_id'])['task_id'],'status':'running','provider_test':self.provider_test})
            if kind=='platform.stop':self.client.stop_task(context['room_id']);return PlatformOutcome('succeeded',response={'status':'stopped','provider_test':self.provider_test})
            if kind=='platform.query':
                state=self.client.query_task(context['room_id'])['status'];original=payload.get('original_kind')
                target='succeeded' if original=='platform.start' and state==3 or original=='platform.stop' and state in (1,2) else 'not_found' if original=='platform.start' and state in (1,2) else 'pending'
                return PlatformOutcome('succeeded',response={'operation_status':target,'status':str(state),'provider_test':self.provider_test})
            if kind in ('display.received','display.processed'):return self.perform_batch(kind,context,[payload])
            return PlatformOutcome('failed','PLATFORM_OPERATION_UNSUPPORTED')
        except RelayError as exc:
            retry=exc.code in ('DOUYIN_HTTP_RETRY','DOUYIN_TRANSPORT_UNCERTAIN','DOUYIN_RATE_LIMITED','DOUYIN_ERR_-1','DOUYIN_ERR_10001','DOUYIN_ERR_40007','DOUYIN_ERR_40022')
            return PlatformOutcome('retry' if retry else 'failed',exc.code,retryable=retry)

    def perform_batch(self,kind,context,payloads):
        try:
            if context.get('app_id')!=self.client.app_id:raise RelayError('PLATFORM_APPLICATION_MISMATCH',403)
            items=[]
            for payload in payloads:
                source=payload.get('source_reference') or {}
                if source.get('event_type')!='comment':raise RelayError('DISPLAY_SOURCE_DELIVERY_REQUIRED',422)
                items.append({'msg_id':source['source_msg_id'],'msg_type':'live_comment','client_time':client_milliseconds(payload.get('client_time'))})
            self.client.ack(context['room_id'],kind,items)
            return PlatformOutcome('succeeded',response={'status':'reported','provider_test':self.provider_test})
        except RelayError as exc:
            retry=exc.code in ('DOUYIN_HTTP_RETRY','DOUYIN_TRANSPORT_UNCERTAIN','DOUYIN_RATE_LIMITED','DOUYIN_ERR_-1','DOUYIN_ERR_10001','DOUYIN_ERR_40007','DOUYIN_ERR_40022')
            return PlatformOutcome('retry' if retry else 'failed',exc.code,retryable=retry)


class DouyinRoomVerifier:
    """Native launch token is passed in memory; claimed room/anchor never wins."""
    def __init__(self,client,*,fact_ttl_seconds=3600):
        self.client=client;self.fact_ttl_seconds=fact_ttl_seconds
    def verify(self,app_id,room_id,actor_id,*,launch_token=None):
        if app_id!=self.client.app_id:raise RelayError('PLATFORM_AUTHORIZATION_REQUIRED',403)
        room=self.client.verify_live_token(launch_token)
        if room.room_id!=room_id:raise RelayError('PLATFORM_ROOM_MISMATCH',403)
        return VerifiedRoom(app_id,room.room_id,actor_id,room.verification_id,datetime.now(timezone.utc)+timedelta(seconds=self.fact_ttl_seconds),self.client.transport is not None,hashlib.sha256(room.anchor_open_id.encode()).hexdigest())

