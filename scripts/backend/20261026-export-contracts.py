"""Export three isolated app factories without lifespan/env/database/model IO.

Each application is imported in its own interpreter. Raw Request/Response wire
adapters live in contracts/export_models.py, separately reviewed and source-bound.
"""
import argparse,copy,hashlib,importlib.util,json,os,subprocess,sys
from pathlib import Path

SPEC='6c582420ea7d80d66cea1178e8629e7358a6a23a88c1d5ea65843a47f821f63b'
C1='d69ae165a6d1169e00f20bb03b4cd66bdc9ed2fe7ef2a730c2a130934a88b4e8'
METHODS={'get','post','patch','put','delete','head','options','trace'}
def read(path):return json.loads(path.read_text())
def write(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def child(appname,source):
    sys.path.insert(0,str(source/f'apps/{appname}'))
    # Construction must never connect; enforce the boundary even on future edits.
    def forbidden(*a,**kw):raise RuntimeError('CONTRACT_EXPORT_EXTERNAL_IO_FORBIDDEN')
    try:
        import sqlalchemy.engine
        sqlalchemy.engine.Engine.connect=forbidden
    except ModuleNotFoundError:pass
    try:
        import psycopg
        psycopg.connect=forbidden
    except ModuleNotFoundError:pass
    from types import SimpleNamespace
    from unittest.mock import patch
    if appname=='api':
        from app.application import create_app
        from app.config import Settings
        app=create_app(Settings('postgresql://schema:unused@127.0.0.1/wordheat_test',analytics_cursor_key=b'c'*32))
    elif appname=='scorer':
        import app.main as module
        with patch.object(module,'load_manifest',return_value=SimpleNamespace(manifest_id='schema-export-only',sha256='0'*64,dimension=1,data={})):
            app=module.create_app(b'c'*32,Path('schema-only'),Path('schema-only'))
    else:
        import app.main as module
        from app.config import RelaySettings
        settings=RelaySettings('postgresql://schema:unused@127.0.0.1/wordheat_relay_test','wordheat_relay_test','wordheat-live:test:relay','test')
        with patch.object(module,'RelayService',return_value=SimpleNamespace()):
            app=module.build_app(settings,control_plane_factory=lambda service:SimpleNamespace(owner_failure=None))
    value=app.openapi()
    def routes(router):
        for route in router.routes:
            original=getattr(route,'original_router',None)
            if original is not None:yield from routes(original)
            else:yield route
    value['x-export-websockets']=[r.path for r in routes(app) if r.__class__.__name__ in {'APIWebSocketRoute','WebSocketRoute'}]
    print(json.dumps(value,ensure_ascii=False))

def load_models(package):
    spec=importlib.util.spec_from_file_location('wire_export_models',package/'export_models.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def merge_schemas(into,incoming):
    for name,schema in incoming.items():
        if name in into and into[name]!=schema:raise ValueError('Cross-application schema collision: '+name)
        into[name]=copy.deepcopy(schema)

def generate(args):
    root=args.root.resolve();package=root/'packages/contracts';models=load_models(package);compat=read(package/'compatibility.json')
    if sha(package/'c1-baseline.json')!=C1:raise ValueError('C1 historical bytes changed')
    documents={};inventory={'version':1,'source_status':args.source_status,'applications':{}}
    for name in ('api','scorer','relay'):
        source=getattr(args,name+'_root') or root;source=source.resolve();python=getattr(args,name+'_python')
        if python is None:raise ValueError('Each app interpreter must be explicit')
        def source_hashes():return [(p.relative_to(source).as_posix(),sha(p)) for p in sorted([*source.glob(f'apps/{name}/app/**/*.py'),source/f'apps/{name}/pyproject.toml',source/f'apps/{name}/uv.lock']) if p.is_file()]
        before=source_hashes()
        command=[str(python),'-B',str(Path(__file__).resolve()),'--child',name,'--source',str(source)]
        run=subprocess.run(command,cwd=source,env={'PATH':os.environ.get('PATH',''),'PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True,timeout=30)
        if run.returncode:raise ValueError(name+' schema-only child failed: '+run.stderr[-2000:])
        document=json.loads(run.stdout);documents[name]=document
        if before!=source_hashes():raise ValueError(name+' source changed during export')
        sources=sorted([*source.glob(f'apps/{name}/app/**/*.py'),source/f'apps/{name}/pyproject.toml',source/f'apps/{name}/uv.lock'])
        inventory['applications'][name]={'http_operations':[{'method':method,'path':path} for path,ops in sorted(document['paths'].items()) for method in sorted(ops) if method in METHODS],'websocket_paths':document['x-export-websockets'],'source_files':[{'path':p.relative_to(source).as_posix(),'sha256':sha(p)} for p in sources if p.is_file()],'dependency_substitutions':{'api':[],'scorer':['manifest loading only; route code unchanged; model not loaded'],'relay':['RelayService construction only; full build_app/control routers unchanged; DB not connected']}[name]}
    inventory['generator_inputs']=[{'path':p.relative_to(root).as_posix(),'sha256':sha(p)} for p in [Path(__file__).resolve(),package/'export_models.py',package/'compatibility.json',package/'c1-baseline.json',package/'relay.schema.json',package/'runtime-manifest.schema.json',package/'native-control.schema.json']]
    api=documents['api'];api.pop('x-export-websockets',None);api['openapi']='3.1.0';api['info']={'title':'WordHeat backend wire contracts','version':'LW-C1/1.0.0+LW-BE/1.1.0'}
    components=api.setdefault('components',{}).setdefault('schemas',{})
    for name in ('scorer','relay'):merge_schemas(components,documents[name].get('components',{}).get('schemas',{}))
    components.update(models.schemas())
    if 'SessionRules' not in components:components['SessionRules']=copy.deepcopy(components['SessionRules-Input'])
    components['SessionRules']['required']=list(components['SessionRules']['properties'])
    components.update(copy.deepcopy(compat['schemas']))
    # Keep previous readiness negotiation naming and examples, while exporting
    # all current typed DTOs. C1 error arbitrary JSON is explicitly recursive.
    for path,ops in api['paths'].items():
        for method,op in ops.items():
            if method not in METHODS:continue
            old=compat['paths'].get(path,{}).get(method,{})
            for key in ('parameters','security','x-role','x-required-permission','x-be-negotiation','x-request-schema','x-invalid-request-example','x-invalid-response-example'):
                if key in old:op[key]=copy.deepcopy(old[key])
            if 'requestBody' in op and 'requestBody' in old:
                for media,value in op['requestBody']['content'].items():
                    previous=old['requestBody']['content'].get(media,{})
                    if 'example' in previous:value['example']=copy.deepcopy(previous['example'])
            for status,response in op['responses'].items():
                for media,value in response.get('content',{}).items():
                    previous=old.get('responses',{}).get(status,{}).get('content',{}).get(media,{})
                    if 'example' in previous:value['example']=copy.deepcopy(previous['example'])
            if method in {'post','patch','put','delete'}:
                for status in ('200','202'):
                    if status not in op['responses'] and any(s in op['responses'] for s in ('200','202')):op['responses'][status]=copy.deepcopy(op['responses'].get('200') or op['responses']['202'])
            if path=='/api/v1/live-sessions/{session_id}/commands':op['requestBody']['content']['application/json']['schema']=models.ref('SessionCommand')
            if path=='/api/v1/rounds/{round_id}/commands':op['requestBody']['content']['application/json']['schema']=models.ref('RoundCommand')
            if path=='/api/v1/health/ready':op['responses']['200']['content']['application/json']['schema']=models.ref('ReadinessResponse')
            if method=='post' and path in {'/api/v1/runtime/commands','/api/v1/connectors/{connector_id}/commands'}:
                headers=op.setdefault('parameters',[])
                for header in ('X-CSRF-Token','Idempotency-Key'):
                    if not any(p.get('name')==header for p in headers):headers.append({'in':'header','name':header,'required':True,'schema':models.S})
            if path in {'/api/v1/live-sessions','/api/v1/rounds'}:
                parameters=op.setdefault('parameters',[]);parameters[:]=[p for p in parameters if p.get('name')!='X-BE-Extension-Version'];parameters.append({'in':'header','name':'X-BE-Extension-Version','required':True,'schema':{'const':'LW-BE/1.1.0'}})
            if path in {'/api/v1/runtime/commands','/api/v1/connectors/{connector_id}/commands'}:
                op['security']=[{'OperatorCookie':[]}];op['x-role']='operator';op['x-required-permission']='maintenance.request' if path=='/api/v1/runtime/commands' else 'session.control'
            if path=='/api/v1/connectors/{connector_id}/commands':op['x-be-negotiation']={'required_when':'args.fresh_start=true or args.retry_failed_deliveries=true','otherwise':'historical C1 compatibility; current actor/session ownership still required'}
    raw=models.raw_operations()
    for name in ('scorer','relay'):
        for path,ops in documents[name]['paths'].items():
            if path in api['paths']:raise ValueError('Duplicate HTTP path across app processes: '+path)
            if path not in raw:raise ValueError('Unreviewed raw Request route: '+path)
            req,res=raw[path];new=copy.deepcopy(ops)
            for method,op in new.items():
                if method not in METHODS:continue
                old=compat['paths'].get(path,{}).get(method,{})
                for key in ('security','parameters','x-role','x-required-permission','x-be-negotiation'):
                    if key in old:op[key]=copy.deepcopy(old[key])
                op['x-application']=name;op['x-wire-source']='source-inventory.json';op['x-role']='native_internal' if name=='scorer' else 'trusted_native_device'
                if name=='relay' and path!='/relay/v1/douyin/callback':
                    op['security']=([{}, {'RelayBearer':[]}] if path.startswith('/relay/v1/device-tokens/') else [{'RelayBearer':[]}])
                if path=='/relay/v1/streams/bootstrap':op.setdefault('parameters',[]).append({'in':'header','name':'Idempotency-Key','required':True,'schema':{'type':'string','minLength':1,'maxLength':128}})
                if req is not None:op['requestBody']={'required':True,'content':{'application/json':{'schema':models.ref(req) if isinstance(req,str) else req}}}
                response_schema=models.ref(res) if res else (models.either(models.ref('RelayLease'),models.ref('RelayRevoke')) if path.endswith('leases/{operation}') else models.either(models.ref('RelayToken'),models.ref('RelayTokenChallenge')))
                op['responses']={'200':{'description':'Source-bound typed wire response','content':{'application/json':{'schema':response_schema}}}}
                if path.endswith('/douyin/callback'):
                    op['responses']={'204':{'description':'Published Douyin callback success; empty response body'}}
                    op['x-role']='official_provider_callback';op['parameters']=[{'in':'header','name':h,'required':True,'schema':models.S} for h in ('x-msg-type','x-nonce-str','x-roomid','x-timestamp','x-signature')]
                    op['x-signature-profile']='data_open_md5: base64(MD5(sorted named headers bytes + unchanged UTF-8 body bytes + secret)); separate OpenAPI RSA profile, never fallback'
                    op['x-max-body-bytes']=1048576
                for status in ('400','401','403','404','409','413','422','429','500','503','504'):op['responses'][status]={'description':'Typed service error','content':{'application/json':{'schema':models.ref('ScorerError' if name=='scorer' else 'RelayError')}}}
                if path in models.DYNAMIC:
                    for param in op.get('parameters',[]):
                        if param.get('in')=='path' and param.get('name')=='operation':param['schema']=models.enum(*models.DYNAMIC[path])
                if name=='relay' and path not in {'/relay/v1/leases','/relay/v1/windows/{operation}','/relay/v1/leases/{operation}','/relay/v1/douyin/callback'}:op.setdefault('parameters',[]).append({'in':'header','name':'X-BE-Extension-Version','required':True,'schema':{'const':'LW-BE/1.1.0'}})
                for media,value in op.get('requestBody',{}).get('content',{}).items():
                    prev=old.get('requestBody',{}).get('content',{}).get(media,{})
                    if 'example' in prev:value['example']=prev['example']
                for key in ('x-invalid-request-example','x-invalid-response-example'):
                    if key in old:op[key]=old[key]
                for status,r in old.get('responses',{}).items():
                    for media,value in r.get('content',{}).items():
                        if 'example' in value and status in op['responses'] and media in op['responses'][status].get('content',{}):op['responses'][status]['content'][media]['example']=value['example']
            api['paths'][path]=new
            if path in models.DYNAMIC:
                for operation in models.DYNAMIC[path]:
                    concrete=path.replace('{operation}',operation);alias=copy.deepcopy(new)
                    for method,op in alias.items():
                        if path=='/relay/v1/windows/{operation}':
                            branch=0 if operation=='open' else 2 if operation=='close' else 1
                            op['requestBody']['content']['application/json']['schema']=copy.deepcopy(req['anyOf'][branch])
                        if path=='/relay/v1/leases/{operation}':op['responses']['200']['content']['application/json']['schema']=models.ref('RelayLease' if operation=='renew' else 'RelayRevoke')
                        if path=='/relay/v1/device-tokens/{operation}':
                            op['requestBody']['content']['application/json']['schema']=copy.deepcopy(req['anyOf'][0 if operation=='challenges' else 1])
                            op['responses']['200']['content']['application/json']['schema']=models.ref('RelayTokenChallenge' if operation=='challenges' else 'RelayToken')
                        op['operationId']+='_'+operation;op['parameters']=[p for p in op.get('parameters',[]) if not(p.get('in')=='path' and p.get('name')=='operation')];op['x-router-template']=path
                    api['paths'][concrete]=alias
    components.pop('HTTPValidationError',None);components.pop('ValidationError',None)
    # Framework validation-error entries are not sent by the C1 handler.
    def fix(value,key=None):
        if isinstance(value,dict):
            if value.get('$ref')=='#/components/schemas/HTTPValidationError':return models.ref('C1ErrorEnvelope')
            if value.get('type')=='object' and 'additionalProperties' not in value:value['additionalProperties']=False
            if value.get('type')=='object' and value.get('additionalProperties') is False and not value.get('properties'):value['maxProperties']=0
            for k,v in list(value.items()):value[k]=fix(v,k)
        elif isinstance(value,list):value[:]=[fix(v) for v in value]
        return value
    fix(api)
    api.setdefault('components',{})['securitySchemes']=compat['securitySchemes']
    ws=api['x-websocket-protocols'];local={'schema_version':1,'contract_version':'LW-C1/1.0.0','x-contract-definition-status':'complete','protocols':copy.deepcopy(ws)}
    for path,wire in ws.items():wire.clear();wire.update({'schema_file':'local-websocket.schema.json','http_operation':False,'transport':'websocket','audience':'DISPLAY' if '/display/' in path else 'STUDIO'})
    ws['/relay/v1/streams']={'schema_file':'relay.schema.json','http_operation':False,'transport':'websocket','audience':'trusted_native_device',
        'client_control_exchange_schema':'#/$defs/ClientControlExchange',
        'x-be-negotiation':{'header':'X-BE-Extension-Version','version':'LW-BE/1.1.0','required_for':['display.received','display.processed'],'unchanged_for':['resume','transport.persisted']},
        'x-rejected-display-receipt':{'missing_or_unsupported_be':'BE_EXTENSION_VERSION_UNSUPPORTED','missing_or_invalid_client_time':'INPUT_INVALID','receipt_time_conflict':'RECEIPT_CONFLICT','frame_kind':'relay.error','close_code':1008,'durable_stage_or_job_write':False}}
    api.update({'x-spec-sha256':SPEC,'x-contract-version':'LW-C1/1.0.0+LW-BE/1.1.0','x-contract-definition-status':'complete','x-undefined-operations':[],'x-source-inventory':'source-inventory.json','x-definition-scope':'Current backend HTTP/WS wire shapes only; not platform authorization, G0/G1/G2, Windows/OBS or release implementation acceptance.'})
    api.pop('x-implementation-status',None)
    def standalone(value):
        if isinstance(value,dict):return {k:(v.replace('#/components/schemas/','#/$defs/') if k=='$ref' and isinstance(v,str) else standalone(v)) for k,v in value.items()}
        if isinstance(value,list):return [standalone(v) for v in value]
        return value
    extension={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:wordheat:backend-extension:1.1.0','x-contract-version':'LW-C1/1.0.0+LW-BE/1.1.0','x-spec-sha256':SPEC,'x-contract-definition-status':'complete','$defs':standalone(components),'$ref':'#/$defs/Readiness'}
    write(package/'backend-extension.schema.json',extension)
    write(package/'openapi.yaml',api);write(package/'local-websocket.schema.json',local);write(package/'source-inventory.json',inventory)
    return api,inventory

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);parser.add_argument('--source-status',choices=['DRAFT_INPUT','FROZEN_INPUT'],default='DRAFT_INPUT')
    for name in ('api','scorer','relay'):parser.add_argument('--'+name+'-python',type=Path);parser.add_argument('--'+name+'-root',type=Path)
    parser.add_argument('--child',choices=['api','scorer','relay']);parser.add_argument('--source',type=Path)
    args=parser.parse_args()
    if args.child:child(args.child,args.source);return
    api,inventory=generate(args);print(json.dumps({'http_operations':sum(len(ops) for ops in api['paths'].values()),'websocket_protocols':len(api['x-websocket-protocols']),'source_status':inventory['source_status']}))
if __name__=='__main__':main()
