"""Source-reviewed wire adapters for handlers using raw Request/Response.

This module has no application imports. The generator records the source hashes
whose guards/return values these schemas describe; it fails on unreviewed routes.
"""
from copy import deepcopy
S={'type':'string','minLength':1}; I={'type':'integer','minimum':0}; B={'type':'boolean'}
SEQ={'type':'string','pattern':r'^(0|[1-9][0-9]*)$'}
TIME={'type':'string','format':'date-time'}; NULL={'type':'null'}
def obj(properties,required=None):return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}
def arr(items,**kw):return {'type':'array','items':items,**kw}
def enum(*values):return {'type':'string','enum':list(values)}
def const(value):return {'const':value,'type':'string' if isinstance(value,str) else 'integer'}
def nullable(value):return {'anyOf':[value,NULL]}
def ref(name):return {'$ref':'#/components/schemas/'+name}
def either(*values):return {'anyOf':list(values)}
def be(schema):
 value=deepcopy(schema);value['properties']['be_extension_version']=const('LW-BE/1.1.0');value['required'].append('be_extension_version');return value
EMPTY=obj({},[]);EMPTY['maxProperties']=0
ERROR=obj({'error':obj({'code':S,'message':S,'retryable':B})})

def schemas():
 result={}
 # JsonValue is intentionally only the C1 diagnostic-details grammar. It is a
 # concrete recursive JSON grammar, not an empty schema/Any placeholder.
 result['JsonValue']={'x-recursive-json-value':True,'anyOf':[NULL,B,{'type':'number'},{'type':'string'},arr(ref('JsonValue')),{'type':'object','additionalProperties':ref('JsonValue')}],'description':'C1 error diagnostic JSON values; never game/public event payloads.'}
 def command(kind,args):return obj({'command':const(kind),'expected_version':I,'args':args},['command','expected_version']+(['args'] if args.get('required') else []))
 result['SessionCommand']=either(command('start',obj({'run_purpose':enum('local','test_live','production')})),command('end',obj({'reason':S},[])),command('recover',obj({'strategy':enum('new_round','end_session','resume_local_round'),'reason':S},['strategy'])),command('select_next',obj({'strategy':enum('random','specific'),'puzzle_id':nullable(S),'expected_selection_id':nullable(S),'expected_selection_version':I},['strategy','expected_selection_id','expected_selection_version'])),command('configure',obj({'apply':const('next_round'),'next_rules':ref('SessionRules')})))
 result['RoundCommand']=either(command('pause',EMPTY),command('resume',EMPTY),command('skip',obj({'reason':S})),command('void',obj({'reason':S})),command('reveal_hint',obj({'stage':{'type':'integer','minimum':1}})))
 result['Command']=either(ref('SessionCommand'),ref('RoundCommand'))
 lease=obj({'lease_id':S,'owner_generation':SEQ,'expires_at':TIME})
 extended=be(obj({**lease['properties'],'server_time':TIME,'lease_ttl_ms':{'type':'integer','minimum':0,'maximum':15000},'official_ingress_ready':B,'platform_ready':B,'stream_id':S,'start_seq':SEQ,'bootstrap_seq':nullable(SEQ),'previous_stream_id':nullable(S),'rpo':nullable(ref('RelayRpo'))},list(lease['properties'])+['server_time','lease_ttl_ms','official_ingress_ready','platform_ready']))
 result['RelayRpo']=obj({'previous_stream_id':S,'previous_session_id':nullable(S),'previous_installation_id':nullable(S),'persisted_seq':SEQ,'retained_from':SEQ,'unprocessed_from':nullable(SEQ),'unprocessed_through':nullable(SEQ),'expired_gap_from':nullable(SEQ),'expired_gap_through':nullable(SEQ),'old_history_reconstructed':{'const':False}},None)
 result['RelayLeaseC1']=lease;result['RelayLeaseBE']=extended
 result['RelayLease']=either(ref('RelayLeaseC1'),ref('RelayLeaseBE'))
 result['RelayWindow']=obj({'round_id':S,'epoch':SEQ,'version':I,'state':enum('OPEN','PAUSED','CLOSED'),'close_seq':nullable(SEQ),'barrier_seq':nullable(SEQ)})
 window=obj({'epoch':SEQ,'version':I,'stream_seq':SEQ,'deadline_at':TIME,'remaining_ms':I,'state':enum('OPEN','PAUSED'),'close_seq':SEQ},['epoch','version','stream_seq','deadline_at','remaining_ms'])
 close=obj({'state':const('CLOSED'),'reason':S,'version':I,'close_seq':SEQ,'barrier_seq':SEQ,'winner_seq':nullable(SEQ)})
 result['RelayWindowResult']=either(window,be(window),close,be(close))
 result['RelayRevoke']=either(obj({'status':const('revoked'),'replacement_not_before':TIME}),be(obj({'status':const('revoked'),'replacement_not_before':TIME})))
 result['RelayError']=ERROR
 pair=obj({'pairing_id':S,'status':enum('pending','bound'),'app_id':S,'room_id':S,'room_binding_id':S,'installation_id':S,'actor_id':S,'challenge_b64':nullable(S),'expires_at':TIME,'room_verified_until':TIME,'verification_id':S,'device_key_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},'provider_test':B,'device_token':S,'server_time':TIME,'ttl_ms':I},['pairing_id','status','app_id','room_id','room_binding_id','installation_id','actor_id','challenge_b64','expires_at','room_verified_until','verification_id','device_key_sha256','provider_test'])
 result['RelayPairing']=either(be(pair),be(obj({'pairing_id':S,'status':enum('pending','bound','revoked'),'installation_id':S,'room_binding_id':S,'provider_test':B,'room_verified_until':TIME},['pairing_id','status','provider_test'])))
 result['RelayTokenChallenge']=be(obj({'challenge_id':S,'challenge_b64':S,'expires_at':TIME}))
 result['RelayToken']=be(obj({'pairing_id':S,'device_token':S,'expires_at':TIME,'server_time':TIME,'ttl_ms':I,'provider_test':B}))
 # Signed objects remain typed envelopes; payloads are discriminated below.
 common={'object_id':S,'issuer_id':S,'issuer_revision':{'type':'integer','minimum':1}}
 result['RevocationPayload']=obj({**common,'revocation_revision':I,'revoked_object_ids':arr(S)})
 result['SignedRevocation']=obj({'domain':const('wordheat-live.readiness'),'type':const('revocation'),'version':const(1),'key_id':S,'payload':ref('RevocationPayload'),'signature':{'type':'string','pattern':'^[A-Za-z0-9_-]{86}$'}})
 result['RelayRevocations']=be(obj({'items':arr(ref('SignedRevocation'),maxItems=1000)}))
 result['FreshnessPayload']=obj({'authorization_id':S,'candidate_digest':S,'installation_id':S,'boot_id':S,'connection_nonce':S,'request_nonce':S,'revocation_revision':I,'server_time':TIME,'ttl_ms':const(10000)})
 result['SignedFreshness']=obj({'domain':const('wordheat-live.readiness'),'type':const('freshness_proof'),'version':const(1),'key_id':S,'payload':ref('FreshnessPayload'),'signature':{'type':'string','pattern':'^[A-Za-z0-9_-]{86}$'}})
 result['RelayFreshness']=be(obj({'proof':ref('SignedFreshness')}))
 result['RelayState']=be(obj({'lease':either(ref('RelayLeaseC1'),ref('RelayLeaseBE'),obj({k:v for k,v in extended['properties'].items() if k!='be_extension_version'},[k for k in extended['required'] if k!='be_extension_version'])),'stream_id':S,'latest_seq':SEQ,'retained_from':SEQ,'window':nullable(ref('RelayWindow')),'remote_fulfillment_pending':nullable(I),'command_result':nullable(ref('RelayWindowResult'))},['lease','stream_id','latest_seq','retained_from','window','remote_fulfillment_pending']))
 result['RelayTask']=be(obj({'job_id':S,'status':enum('pending','running','retry','succeeded','failed','cancelled'),'attempts':I,'cycle_attempts':I,'retry_cycle':I,'reason_code':nullable(S),'platform_fulfilled':B,'provider_test':nullable(B)}))
 result['RelayDeliveryStage']=be(obj({'status':const('accepted'),'stage':enum('transport.persisted','display.received','display.processed'),'stream_id':S,'stream_seq':SEQ,'remote_fulfillment_pending':nullable(I),'provider_test':B}))
 bootstrap=deepcopy(extended);bootstrap['properties']['fresh_start']={'const':True};bootstrap['required'].append('fresh_start');result['RelayBootstrap']=bootstrap
 result['ScorerRequest']=obj({'request_id':{'type':'string','minLength':1,'maxLength':128,'pattern':'^[A-Za-z0-9_.:-]+$'},'manifest_id':{'type':'string','minLength':1,'maxLength':128,'pattern':'^[A-Za-z0-9_.:-]+$'},'texts':arr({'type':'string','minLength':1,'maxLength':256},minItems=1,maxItems=32),'deadline_ms':{'type':'integer','minimum':1,'maximum':10000}})
 result['ScorerResponse']=obj({'request_id':S,'manifest_id':S,'dimension':{'type':'integer','minimum':1},'vectors':arr(arr({'type':'number','minimum':-1,'maximum':1},minItems=1),minItems=1,maxItems=32)})
 result['ScorerError']=ERROR
 return result


def raw_operations():
 lease={'lease_id':S};epoch={**lease,'expected_version':{'type':'integer','minimum':1},'window_epoch':SEQ}
 pair=either(obj({'action':const('issue'),'device_public_key':S,'launch_token':S},['device_public_key','launch_token']),obj({'action':const('complete'),'pairing_id':S,'signature':S,'launch_token':S}),obj({'action':enum('query','revoke'),'pairing_id':S}))
 callback=arr(obj({'msg_id':S,'sec_openid':S,'nickname':{'type':'string'},'content':{'type':'string'},'timestamp':either({'type':'integer','minimum':0,'maximum':9999999999999999999},{'type':'string','pattern':'^[0-9]{1,19}$'})}),minItems=1,maxItems=1000);callback['items']['additionalProperties']=ref('JsonValue')
 # Public raw callback field names are fixed by the published provider adapter.
 result={
 '/internal/v1/embeddings':('ScorerRequest','ScorerResponse'),
 '/relay/v1/leases':(None,'RelayLease'),
 '/relay/v1/windows/{operation}':(either(obj({**lease,'round_id':S,'deadline_at':TIME}),obj(epoch),obj({**epoch,'reason':S,'winner_seq':nullable(SEQ)},list(epoch)+['reason'])),'RelayWindowResult'),
 '/relay/v1/leases/{operation}':(obj(lease),None),
 '/relay/v1/device-pairings':(pair,'RelayPairing'),
 '/relay/v1/freshness':(obj({'authorization_id':S,'candidate_digest':S,'installation_id':S,'boot_id':S,'connection_nonce':S,'request_nonce':S,'revocation_revision':I}),'RelayFreshness'),
 '/relay/v1/device-tokens/{operation}':(either(obj({'pairing_id':S}),obj({'challenge_id':S,'signature':S})) ,None),
 '/relay/v1/revocations':(EMPTY,'RelayRevocations'),
 '/relay/v1/streams/bootstrap':(obj({**lease,'expected_stream_id':S,'reason':S}),'RelayBootstrap'),
 '/relay/v1/delivery-stages':(obj({'stream_id':S,'stream_seq':SEQ,'stage':enum('transport.persisted','display.received','display.processed'),'receipt_id':S,'retry_failed':B,'client_time':TIME},['stream_id','stream_seq','stage','receipt_id']),'RelayDeliveryStage'),
 '/relay/v1/state':(obj({**lease,'command_id':S,'operation':enum('open','pause','resume','close')},['lease_id']),'RelayState'),
 '/relay/v1/tasks/{operation}':(obj({**lease,'retry_failed':B},['lease_id']),'RelayTask'),
 '/relay/v1/douyin/callback':(callback,None),
 }
 return result

DYNAMIC={'/relay/v1/windows/{operation}':['open','pause','resume','close'],'/relay/v1/leases/{operation}':['renew','revoke'],'/relay/v1/device-tokens/{operation}':['challenges','rotate'],'/relay/v1/tasks/{operation}':['start','stop','query']}
